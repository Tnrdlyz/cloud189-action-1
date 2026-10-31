"""离线单测：不发真实请求，只验证解析、容错与账号切分。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import requests

import checkin
from cloud189 import (
    AuthError,
    Cloud189Client,
    Cloud189Error,
    SessionExpired,
    SignResult,
    _as_bool,
    _as_int,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, url="https://cloud.189.cn/x"):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ("" if payload is None else json.dumps(payload))
        self.url = url
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """按顺序返回预置响应，并记录请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.cookies = requests.Session().cookies

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._next()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next()

    def _next(self):
        if not self.responses:
            raise AssertionError("没有预置响应了")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(*responses, **kwargs):
    client = Cloud189Client(cookie="COOKIE_LOGIN_USER=abc; SSON=def", **kwargs)
    client.session = FakeSession(responses)
    # 换掉 session 会丢掉 Cookie，这里按原样重新灌一次
    client._load_cookie_string(client.cookie)
    return client


class HelperTests(unittest.TestCase):
    def test_bool_and_int(self):
        self.assertTrue(_as_bool("false") is False)
        self.assertTrue(_as_bool(True))
        self.assertEqual(_as_int("12"), 12)
        self.assertEqual(_as_int(None), 0)
        self.assertEqual(_as_int("abc"), 0)

    def test_sign_result_text(self):
        self.assertIn("已签到", SignResult(is_sign=True, bonus_mb=0).text)
        self.assertIn("100M", SignResult(is_sign=False, bonus_mb=100).text)


class AccountSplitTests(unittest.TestCase):
    def test_split_by_newline(self):
        self.assertEqual(checkin.split_accounts("a\nb\n\n c "), ["a", "b", "c"])

    def test_split_by_hash(self):
        self.assertEqual(
            checkin.split_accounts("13800000000#13800000001"),
            ["13800000000", "13800000001"],
        )

    def test_empty(self):
        self.assertEqual(checkin.split_accounts("  "), [])

    def test_build_clients_keeps_order_and_labels(self):
        config = {
            "cookies": ["COOKIE_LOGIN_USER=xyz123456; SSON=q"],
            "ssons": ["ss"],
            "users": ["13800000000"],
            "passwords": ["pwd"],
        }
        accounts = checkin.build_clients(config)
        self.assertEqual([a["label"][:4] for a in accounts], ["Cook", "SSON", "138*"])
        self.assertEqual(accounts[0]["cookie"], "COOKIE_LOGIN_USER=xyz123456; SSON=q")
        self.assertEqual(accounts[2]["password"], "pwd")

    def test_mask_never_leaks(self):
        self.assertNotIn("12345678", checkin._mask("12345678"))
        self.assertEqual(checkin._mask("ab"), "**")


class LoginTests(unittest.TestCase):
    def test_cookie_login_gets_session_key(self):
        client = make_client(FakeResponse(payload={"sessionKey": "sk-1", "accessToken": "at"}))
        self.assertEqual(client.login(), "cookie")
        self.assertEqual(client.session_key, "sk-1")

    def test_cookie_login_without_session_key_falls_back_to_sson(self):
        # 1) getSessionForPC 返回空 → 2) unifyLoginForPC 302 → 3) 带 SSON 拿跳转 → 4) 再换 sessionKey
        client = make_client(
            FakeResponse(payload={"errorCode": "InvalidSessionKey", "errorMsg": "null"}),
            FakeResponse(text="<html>", url="https://open.e.189.cn/login"),
            FakeResponse(text="<html>", url="https://cloud.189.cn/web/redirect.html?x=1"),
            FakeResponse(payload={"sessionKey": "sk-2"}),
        )
        self.assertEqual(client.login(), "cookie")
        self.assertEqual(client.session_key, "sk-2")

    def test_password_login_requires_credentials(self):
        client = Cloud189Client()
        with self.assertRaises(AuthError):
            client.login()

    def test_password_login_reports_captcha(self):
        client = make_client(
            FakeResponse(payload={"result": 0, "data": {"pre": "{NRP}", "pubKey": "k"}})  # encryptConf
        )
        client.username, client.password = "u", "p"
        client.login_mode = "password"
        with mock.patch.object(Cloud189Client, "_rsa_hex", return_value="deadbeef"):
            client.session.responses.extend(
                [
                    FakeResponse(text=(
                        "'captchaToken' value='ct'\nlt = \"lt1\"\n"
                        "paramId = \"pid\"\nreqId = \"rid\"\n"
                    )),
                    FakeResponse(payload={"result": -1, "msg": "请输入验证码"}),
                ]
            )
            with self.assertRaises(AuthError) as ctx:
                client._password_login()
        self.assertIn("验证码", str(ctx.exception))


class SignTests(unittest.TestCase):
    def _ready(self, *responses, session_key="sk"):
        client = make_client(*responses)
        client.session_key = session_key
        client.login_mode = "cookie"
        return client

    def test_sign_ok(self):
        client = self._ready(FakeResponse(payload={"isSign": "false", "netdiskBonus": 88}))
        result = client.sign_in()
        self.assertFalse(result.is_sign)
        self.assertEqual(result.bonus_mb, 88)

    def test_sign_sends_session_key(self):
        client = self._ready(FakeResponse(payload={"isSign": True, "netdiskBonus": 0}))
        client.sign_in()
        _, url, kwargs = client.session.calls[-1]
        self.assertIn("/mkt/userSign.action", url)
        self.assertEqual(kwargs["params"]["sessionKey"], "sk")

    def test_sign_redirect_means_expired_and_relogin_once(self):
        client = self._ready(
            FakeResponse(status_code=302, payload={}),
            FakeResponse(payload={"sessionKey": "sk-new"}),
            FakeResponse(payload={"isSign": True, "netdiskBonus": 0}),
        )
        result = client.sign_in()
        self.assertTrue(result.is_sign)
        self.assertEqual(client.session_key, "sk-new")

    def test_sign_html_means_expired(self):
        client = self._ready(FakeResponse(text="<html><title>login</title></html>"))
        with self.assertRaises(SessionExpired):
            client.sign_in()

    def test_sign_invalid_session_retries_then_fails(self):
        client = self._ready(
            FakeResponse(payload={"errorCode": "InvalidSessionKey", "errorMsg": "expired"}),
            FakeResponse(payload={"sessionKey": "sk-new"}),
            FakeResponse(payload={"errorCode": "InvalidSessionKey", "errorMsg": "expired again"}),
        )
        with self.assertRaises(SessionExpired):
            client.sign_in()

    def test_sign_server_error_is_plain_error(self):
        client = self._ready(FakeResponse(status_code=500, payload={"errorCode": "SystemError"}))
        with self.assertRaises(Cloud189Error) as ctx:
            client.sign_in()
        self.assertNotIsInstance(ctx.exception, SessionExpired)

    def test_draw_success_and_failure(self):
        client = self._ready(
            FakeResponse(payload={"description": "30M空间"}),
            FakeResponse(payload={"errorCode": "TimeOut", "errorMsg": "已经抽过奖"}),
        )
        ok = client.draw_prize(("TASK_SIGNIN", "ACT_SIGNIN"))
        self.assertEqual(ok.prize, "30M空间")
        failed = client.draw_prize(("TASK_SIGNIN", "ACT_SIGNIN"))
        self.assertEqual(failed.prize, "")


class ReportTests(unittest.TestCase):
    def test_report_marks_failure(self):
        results = [
            checkin.AccountResult(index=0, label="A", ok=True, login_mode="cookie",
                                  sign=SignResult(is_sign=False, bonus_mb=30)),
            checkin.AccountResult(index=1, label="B", ok=False, error="登录失败：过期"),
        ]
        report = checkin.render_report(results, check_only=False)
        self.assertIn("成功 1 个", report)
        self.assertIn("登录失败：过期", report)
        self.assertIn("| 2 |", report)


class ExitCodeTests(unittest.TestCase):
    def test_no_accounts_returns_2(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(checkin.main(["--no-notify"]), 2)

    def test_all_failed_returns_1(self):
        env = {"COOKIE_189": "a=b", "PYTHONUNBUFFERED": "1"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            checkin, "run_account",
            return_value=checkin.AccountResult(index=0, label="x", error="boom"),
        ):
            self.assertEqual(checkin.main(["--no-notify"]), 1)

    def test_one_ok_returns_0(self):
        env = {"COOKIE_189": "a=b"}
        ok = checkin.AccountResult(index=0, label="x", ok=True,
                                   sign=SignResult(is_sign=True, bonus_mb=0))
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            checkin, "run_account", return_value=ok
        ):
            self.assertEqual(checkin.main(["--no-notify"]), 0)


if __name__ == "__main__":
    unittest.main()