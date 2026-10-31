"""天翼云盘（Cloud 189）登录 / 签到 / 抽奖客户端。

只依赖 requests（口令登录另需 cryptography），不碰网盘文件读写。

2026 年时点的登录链路（与官方 Web / App 当前行为一致）：

    getSessionForPC.action  →  sessionKey
                                 │
     cloud.189.cn 接口都带 sessionKey 查询参数 + 用户会话 Cookie

也就是说，2020 年那种「udb_login.jsp + RSA 提交 + 拿 Cookie 直接调签到接口」
的写法已经不可用：/udb/ 路径 301 跳走，且 cloud.189.cn 的接口现在校验
sessionKey（缺失时返回 InvalidSessionKey / 302 跳登录页）。

本文件支持三种凭证，优先级 cookie > sson > 账号口令：

* cookie：浏览器里 cloud.189.cn 的整条 Cookie（含 COOKIE_LOGIN_USER 等），
  最省事、最稳，不会遇到验证码；
* sson：open.e.189.cn 的 SSON Cookie（扫码登录后可得），走 SSO 握手；
* username/password：走 unifyLoginForPC + encryptConf + loginSubmit，
  云端若要求图形/短信验证码则无法在 CI 里完成，会给出明确报错。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

WEB_URL = "https://cloud.189.cn"
AUTH_URL = "https://open.e.189.cn"
API_URL = "https://api.cloud.189.cn"
DRAW_URL = "https://m.cloud.189.cn"

# 官方 Web 端登录用的固定参数
APP_ID = "8025431004"
LOGIN_CLIENT_TYPE = "10020"
RETURN_URL = "https://m.cloud.189.cn/zhuanti/2020/loginErrorPc/index.html"
PC_CLIENT_TYPE = "TELEPC"
PC_VERSION = "6.2"
PC_CHANNEL_ID = "web_cloud.189.cn"

# 签到接口伪装成 Android 客户端
API_CLIENT_TYPE = "TELEANDROID"
API_VERSION = "9.0.6"
API_MODEL = "KB2000"

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; KB2000 Build/RP1A.201005.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/96.0.4664.104 "
    "Mobile Safari/537.36 Ecloud/9.0.6 Android/30 clientId/538135150693412 "
    "clientModel/KB2000 clientChannelId/qq proVersion/1.0.6"
)

# 2020 版脚本里写死的活动参数，至今仍在使用
TASK_SIGNIN = ("TASK_SIGNIN", "ACT_SIGNIN")
TASK_SIGNIN_PHOTOS = ("TASK_SIGNIN_PHOTOS", "ACT_SIGNIN")


class Cloud189Error(Exception):
    """接口层面可解释的错误（errorCode / 非 JSON / 网络）。"""

    def __init__(self, message: str, code: str | None = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class AuthError(Cloud189Error):
    """凭证缺失、口令登录被验证码拦住、SSO 握手失败等。"""


class SessionExpired(Cloud189Error):
    """sessionKey / Cookie 失效，需要重新登录。"""


@dataclass
class SignResult:
    """一次个人签到任务的结果。"""

    is_sign: bool
    bonus_mb: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        if self.is_sign:
            return f"今日已签到（本次未新增空间，签到累计奖励 {self.bonus_mb}M）"
        if self.bonus_mb:
            return f"签到成功，获得 {self.bonus_mb}M 空间"
        return "签到成功"


@dataclass
class DrawResult:
    """一次抽奖任务的结果。"""

    task_id: str
    prize: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"抽奖获得 {self.prize}" if self.prize else (self.error or "抽奖无结果")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class Cloud189Client:
    """一个账号一个实例；同一实例内自动维持 Cookie 与 sessionKey。"""

    def __init__(
        self,
        *,
        cookie: str = "",
        sson: str = "",
        username: str = "",
        password: str = "",
        timeout: float = 20.0,
        debug: bool = False,
    ):
        self.cookie = (cookie or "").strip()
        self.sson = (sson or "").strip()
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = timeout
        self.debug = debug

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": PC_UA,
                "Referer": f"{WEB_URL}/web/main/",
                "Accept": "application/json;charset=UTF-8",
            }
        )
        self.session_key = ""
        self.access_token = ""
        self.account = ""
        self.login_mode = ""

        if self.cookie:
            self._load_cookie_string(self.cookie)

    # ------------------------------------------------------------------ 登录

    def login(self) -> str:
        """完成登录并拿到 sessionKey；返回登录方式（cookie/sson/password）。"""
        if self.cookie:
            self.login_mode = "cookie"
            self._login_with_cookie()
        elif self.sson:
            self.login_mode = "sson"
            self._login_with_sson()
        elif self.username and self.password:
            self.login_mode = "password"
            self._login_with_password()
        else:
            raise AuthError("没有可用的凭证：请提供 COOKIE_189 / SSON / USER+PWD 之一")
        return self.login_mode

    def _login_with_cookie(self) -> None:
        # 浏览器 Cookie 里通常已经带有用户会话，直接换 sessionKey 即可。
        try:
            self._get_session_for_pc()
            return
        except Cloud189Error as exc:
            # 浏览器 Cookie 可能只带了 SSON，或会话已过期，走一次 SSO 握手。
            self._debug(f"直接用 Cookie 换 sessionKey 失败：{exc}，尝试 SSO 握手")
        sson = self._cookie_value("SSON")
        if not sson:
            raise AuthError(
                "Cookie 无法换取 sessionKey（多半是会话已过期）。"
                "请重新从浏览器复制 cloud.189.cn 的 Cookie，确认包含 COOKIE_LOGIN_USER。"
            )
        self._login_with_sson()

    def _login_with_sson(self, sson: str = "") -> None:
        sson = sson or self.sson or self._cookie_value("SSON")
        if not sson:
            raise AuthError("SSON 为空，无法完成 SSO 登录")
        redirect_url = self._sso_handshake(sson)
        self._get_session_for_pc(redirect_url=redirect_url)

    def _login_with_password(self) -> None:
        login_url = self._password_login()
        self._get_session_for_pc(redirect_url=login_url)

    def _sso_handshake(self, sson: str) -> str:
        """用 SSON 换到带用户会话的跳转地址。"""
        first = self.session.get(
            f"{WEB_URL}/api/portal/unifyLoginForPC.action",
            params=self._login_params(),
            timeout=self.timeout,
        )
        self._raise_for_status(first)
        second = self.session.get(
            first.url,
            headers={"Cookie": f"SSON={sson}"},
            timeout=self.timeout,
        )
        self._raise_for_status(second)
        return second.url

    def _password_login(self) -> str:
        """口令登录，返回可交给 getSessionForPC 的跳转地址。

        现代链路：encryptConf 取公钥与前缀 → unifyLoginForPC 取 lt/paramId/reqId
        → loginSubmit.do 提交 → toUrl。需要验证码时这里会明确报错。
        """
        conf = self.session.post(
            f"{AUTH_URL}/api/logbox/config/encryptConf.do", timeout=self.timeout
        )
        self._raise_for_status(conf)
        try:
            data = conf.json()["data"]
        except (ValueError, KeyError) as exc:
            raise AuthError(f"encryptConf 返回异常：{conf.text[:200]}") from exc

        page = self.session.get(
            f"{WEB_URL}/api/portal/unifyLoginForPC.action",
            params=self._login_params(),
            timeout=self.timeout,
        )
        self._raise_for_status(page)
        try:
            captcha_token = re.search(r"'captchaToken' value='(.+?)'", page.text).group(1)  # type: ignore[union-attr]
            lt = re.search(r'lt = "(.+?)"', page.text).group(1)  # type: ignore[union-attr]
            param_id = re.search(r'paramId = "(.+?)"', page.text).group(1)  # type: ignore[union-attr]
            req_id = re.search(r'reqId = "(.+?)"', page.text).group(1)  # type: ignore[union-attr]
        except AttributeError as exc:
            raise AuthError(
                "登录页结构已变化，解析不到 captchaToken/lt/paramId/reqId。"
                "请优先使用 COOKIE_189 方式登录。"
            ) from exc

        prefix = data.get("pre", "")
        payload = {
            "appKey": APP_ID,
            "accountType": "02",
            "validateCode": "",
            "captchaToken": captcha_token,
            "dynamicCheck": "FALSE",
            "clientType": "1",
            "cb_SaveName": "3",
            "isOauth2": "false",
            "returnUrl": RETURN_URL,
            "paramId": param_id,
            "userName": f"{prefix}{self._rsa_hex(data['pubKey'], self.username)}",
            "password": f"{prefix}{self._rsa_hex(data['pubKey'], self.password)}",
        }
        resp = self.session.post(
            f"{AUTH_URL}/api/logbox/oauth2/loginSubmit.do",
            data=payload,
            headers={"Referer": f"{AUTH_URL}/", "lt": lt, "REQID": req_id},
            timeout=self.timeout,
        )
        self._raise_for_status(resp)
        try:
            result = resp.json()
        except ValueError as exc:
            raise AuthError(f"登录提交返回非 JSON：{resp.text[:200]}") from exc

        if str(result.get("result")) not in ("0", "None"):
            msg = result.get("msg") or "登录失败"
            hint = ""
            text = str(msg)
            if "验证码" in text or "captcha" in text.lower():
                hint = "（云端要求图形验证码，CI 无法完成，请改用 COOKIE_189）"
            elif "账户名或密码" in text or "密码错误" in text:
                hint = (
                    "（若确认密码无误：天翼云盘可能已不再接受网页口令登录，"
                    "或该账号只开通了短信登录；请改用 COOKIE_189）"
                )
            raise AuthError(f"{msg}{hint}", code=str(result.get("result")))
        to_url = result.get("toUrl") or ""
        if not to_url:
            raise AuthError("登录成功但没有拿到跳转地址 toUrl")
        return to_url

    # ------------------------------------------------------- sessionKey/Cookie

    def _login_params(self) -> dict:
        return {
            "appId": APP_ID,
            "clientType": LOGIN_CLIENT_TYPE,
            "returnURL": RETURN_URL,
            "timeStamp": int(time.time() * 1000),
        }

    def _get_session_for_pc(self, redirect_url: str = "", access_token: str = "") -> None:
        params = {
            "appId": APP_ID,
            "clientType": PC_CLIENT_TYPE,
            "version": PC_VERSION,
            "channelId": PC_CHANNEL_ID,
            "rand": int(time.time() * 1000),
        }
        if redirect_url:
            params["redirectURL"] = redirect_url
        if access_token:
            params["accessToken"] = access_token

        resp = self.session.post(
            f"{API_URL}/getSessionForPC.action", params=params, timeout=self.timeout
        )
        self._raise_for_status(resp)
        payload = self._json_or_raise(resp, "getSessionForPC")

        session_key = payload.get("sessionKey") or ""
        if not session_key:
            code = payload.get("errorCode") or payload.get("code") or ""
            message = payload.get("errorMsg") or payload.get("message") or str(payload)[:200]
            if code in ("UserInvalidOpenToken", "UserInvalidSessionkey"):
                raise AuthError(f"凭证已失效：{message}", code=code)
            raise AuthError(f"未拿到 sessionKey：{message}", code=code)

        self.session_key = session_key
        self.access_token = payload.get("accessToken") or self.access_token
        # 服务端可能在这次响应里补种 Cookie，requests 已自动收下。

    def _relogin(self) -> None:
        self.session_key = ""
        if self.login_mode == "cookie" and self.cookie:
            self._login_with_cookie()
        elif self.login_mode == "sson" or self._cookie_value("SSON"):
            self._login_with_sson()
        elif self.username and self.password:
            self._login_with_password()
        else:
            raise SessionExpired("sessionKey 失效且没有可用的重新登录方式")

    # --------------------------------------------------------------- 业务接口

    def account_info(self) -> dict:
        """取当前登录账号信息（失败不影响签到）。"""
        return self._request_json(
            f"{WEB_URL}/api/open/user/getUserInfo.action",
            headers={"Referer": f"{WEB_URL}/web/main/"},
        )

    def sign_in(self) -> SignResult:
        payload = self._request_json(
            f"{WEB_URL}/mkt/userSign.action",
            params={
                "rand": int(time.time() * 1000),
                "clientType": API_CLIENT_TYPE,
                "version": API_VERSION,
                "model": API_MODEL,
            },
            mobile=True,
        )
        return SignResult(
            is_sign=_as_bool(payload.get("isSign")),
            bonus_mb=_as_int(payload.get("netdiskBonus")),
            raw=payload,
        )

    def draw_prize(self, task: tuple[str, str] = TASK_SIGNIN) -> DrawResult:
        task_id, activity_id = task
        try:
            payload = self._request_json(
                f"{DRAW_URL}/v2/drawPrizeMarketDetails.action",
                params={"taskId": task_id, "activityId": activity_id},
                mobile=True,
                host=DRAW_URL.split("://", 1)[-1],
                is_draw=True,
                # 抽奖是尽力而为：失败（含会话超时）只回报，不再重新登录重试，
                # 免得一次「今天已经抽过」被当成会话失效
                _retry=False,
            )
        except Cloud189Error as exc:
            return DrawResult(task_id=task_id, error=str(exc))
        prize = payload.get("description") or payload.get("prizeName") or ""
        error = ""
        if not prize:
            error = payload.get("errorMsg") or payload.get("message") or "抽奖无结果"
        return DrawResult(task_id=task_id, prize=prize, error=error, raw=payload)

    # ------------------------------------------------------------- 请求工具箱

    def _request_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        mobile: bool = False,
        host: str = "",
        is_draw: bool = False,
        _retry: bool = True,
    ) -> dict:
        if not self.session_key:
            self._relogin()

        query = dict(params or {})
        query["sessionKey"] = self.session_key

        request_headers = {
            "User-Agent": MOBILE_UA if mobile else PC_UA,
            "Accept": "application/json;charset=UTF-8",
        }
        if mobile:
            request_headers["Referer"] = f"{WEB_URL}/web/main/"
        if host:
            request_headers["Host"] = host
        request_headers.update(headers or {})

        self._debug(f"GET {url} params={ {k: v for k, v in query.items() if k != 'sessionKey'} }")
        resp = self.session.get(
            url, params=query, headers=request_headers, timeout=self.timeout
        )

        # 302 / HTML 登录页 = 会话失效
        if resp.status_code in (301, 302, 303, 307):
            if _retry:
                self._debug("会话失效（重定向到登录），重新登录后重试")
                self._relogin()
                return self._request_json(
                    url,
                    params=params,
                    headers=headers,
                    mobile=mobile,
                    host=host,
                    is_draw=is_draw,
                    _retry=False,
                )
            raise SessionExpired("接口重定向到登录页，sessionKey 已失效")
        self._raise_for_status(resp)

        try:
            payload = resp.json()
        except ValueError:
            body = resp.text[:300].replace("\n", " ")
            if "<html" in body.lower():
                raise SessionExpired("接口返回登录页（HTML），Cookie 已失效，请更新凭证")
            raise Cloud189Error(f"接口返回非 JSON：{body}")

        if isinstance(payload, dict) and payload.get("errorCode"):
            code = str(payload.get("errorCode"))
            message = str(payload.get("errorMsg") or payload.get("message") or code)
            transient = code in (
                "InvalidSessionKey",
                "SessionTimeOut",
                "TimeOut",
                "InvalidAccessToken",
            )
            if transient and _retry:
                self._debug(f"{code}（{message}），重新登录后重试")
                self._relogin()
                return self._request_json(
                    url,
                    params=params,
                    headers=headers,
                    mobile=mobile,
                    host=host,
                    is_draw=is_draw,
                    _retry=False,
                )
            if transient:
                raise SessionExpired(message, code=code, payload=payload)
            if is_draw:
                # 抽奖失败是常态（已抽过 / 任务未完成），交给上层展示
                raise Cloud189Error(message, code=code, payload=payload)
            raise Cloud189Error(message, code=code, payload=payload)

        return payload if isinstance(payload, dict) else {"data": payload}

    def _raise_for_status(self, resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        code = ""
        message = resp.text[:300]
        try:
            payload = resp.json()
            code = str(payload.get("errorCode") or payload.get("code") or "")
            message = str(payload.get("errorMsg") or payload.get("message") or message)
        except ValueError:
            pass
        if code in ("InvalidSessionKey", "SessionTimeOut", "TimeOut"):
            raise SessionExpired(message, code=code)
        raise Cloud189Error(f"HTTP {resp.status_code}：{message}", code=code)

    def _json_or_raise(self, resp: requests.Response, where: str) -> dict:
        try:
            payload = resp.json()
        except ValueError as exc:
            body = resp.text[:300].replace("\n", " ")
            if "<error" in body:
                raise Cloud189Error(f"{where} 返回错误：{body}") from exc
            raise Cloud189Error(f"{where} 返回非 JSON：{body}") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    # ------------------------------------------------------------- Cookie 助手

    def _load_cookie_string(self, raw: str) -> None:
        for part in raw.replace("\n", ";").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if not name:
                continue
            for domain in (".189.cn", ".cloud.189.cn"):
                try:
                    self.session.cookies.set(name, value, domain=domain, path="/")
                except (ValueError, AttributeError):
                    pass

    def _cookie_value(self, name: str) -> str:
        for cookie in self.session.cookies:
            if cookie.name == name:
                return cookie.value
        return ""

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _rsa_hex(public_key_pem_body: str, text: str) -> str:
        """RSA/PKCS1-v1_5 加密并转成十六进制（官方前端的做法）。"""
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可读提示
            raise AuthError(
                "口令登录需要 cryptography，请先 pip install -r requirements.txt"
            ) from exc

        pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            f"{public_key_pem_body}\n"
            "-----END PUBLIC KEY-----"
        )
        key = serialization.load_pem_public_key(pem.encode())
        encrypted = key.encrypt(text.encode(), padding.PKCS1v15())
        return encrypted.hex()

    def _debug(self, message: str) -> None:
        if self.debug:
            print(f"[debug] {message}", flush=True)