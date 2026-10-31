#!/usr/bin/env python3
"""天翼云盘自动签到 + 抽奖。

用法：
    python checkin.py              # 签到 + 抽奖（GitHub Actions 默认行为）
    python checkin.py --check      # 只验证登录、不签到，用来排查凭证
    python checkin.py --no-notify  # 签到但不推送通知
    python checkin.py --json       # 输出机器可读结果

凭证来自环境变量，多个账号用换行或 # 分隔：
    COOKIE_189   浏览器里 cloud.189.cn 的 Cookie（推荐）
    SSON         open.e.189.cn 的 SSON Cookie（扫码登录可得）
    USER / PWD   账号 + 密码（云端要验证码时会失败，仅作兼容）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field

import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - 非标准输出（如被重定向）时忽略
        pass

from cloud189 import (
    TASK_SIGNIN,
    TASK_SIGNIN_PHOTOS,
    AuthError,
    Cloud189Client,
    Cloud189Error,
    DrawResult,
    SessionExpired,
    SignResult,
)

BEIJING_OFFSET = 8 * 3600
DIVIDER = "=" * 56


@dataclass
class AccountResult:
    index: int
    label: str
    ok: bool = False
    login_mode: str = ""
    sign: SignResult | None = None
    draws: list[DrawResult] = field(default_factory=list)
    error: str = ""

    @property
    def sign_text(self) -> str:
        return self.sign.text if self.sign else (self.error or "未签到")

    @property
    def draw_text(self) -> str:
        if not self.draws:
            return "未抽奖"
        return " / ".join(d.text for d in self.draws)


# --------------------------------------------------------------------- 配置


def split_accounts(value: str) -> list[str]:
    """按换行或 # 切分账号列表；空值返回空列表。"""
    value = (value or "").strip()
    if not value:
        return []
    if "\n" in value or "\r" in value:
        parts = value.replace("\r", "\n").split("\n")
    else:
        parts = value.split("#")
    return [p.strip() for p in parts if p.strip()]


def read_config() -> dict:
    users = split_accounts(os.environ.get("USER", ""))
    pwds = split_accounts(os.environ.get("PWD", ""))
    cookies = split_accounts(os.environ.get("COOKIE_189", "") or os.environ.get("COOKIE", ""))
    ssons = split_accounts(os.environ.get("SSON", ""))
    return {
        "users": users,
        "passwords": pwds,
        "cookies": cookies,
        "ssons": ssons,
        "serverchan": os.environ.get("SERVERCHAN_SENDKEY", "").strip(),
        "webhook": os.environ.get("NOTIFY_WEBHOOK", "").strip(),
        "debug": bool(os.environ.get("DEBUG")),
    }


def build_clients(config: dict) -> list[dict]:
    """按凭证种类展开成待处理账号列表。"""
    accounts: list[dict] = []
    for cookie in config["cookies"]:
        accounts.append({"label": _cookie_label(cookie), "cookie": cookie, "mode": "cookie"})
    for i, sson in enumerate(config["ssons"]):
        accounts.append({"label": f"SSON账号{i + 1}", "sson": sson, "mode": "sson"})
    for i, user in enumerate(config["users"]):
        password = config["passwords"][i] if i < len(config["passwords"]) else ""
        accounts.append(
            {"label": _mask(user), "username": user, "password": password, "mode": "password"}
        )
    return accounts


def _cookie_label(cookie: str) -> str:
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() in ("COOKIE_LOGIN_USER", "COOKIE_LOGIN_USER_SSO"):
            return f"Cookie账号{_mask(value)[:8]}"
    return "Cookie账号"


def _mask(text: str) -> str:
    text = str(text)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:3]}***{text[-2:]}"


# --------------------------------------------------------------------- 执行


def run_account(index: int, account: dict, *, check_only: bool, debug: bool) -> AccountResult:
    result = AccountResult(
        index=index, label=account["label"], login_mode=account.get("mode", "")
    )
    client = Cloud189Client(
        cookie=account.get("cookie", ""),
        sson=account.get("sson", ""),
        username=account.get("username", ""),
        password=account.get("password", ""),
        debug=debug,
    )
    try:
        result.login_mode = client.login()
        _attach_identity(client, result)
        if check_only:
            result.ok = True
            return result

        result.sign = client.sign_in()
        result.draws.append(client.draw_prize(TASK_SIGNIN))
        result.draws.append(client.draw_prize(TASK_SIGNIN_PHOTOS))
        result.ok = True
        return result
    except AuthError as exc:
        result.error = f"登录失败：{exc}"
    except SessionExpired as exc:
        result.error = f"会话失效：{exc}（请重新获取 Cookie）"
    except Cloud189Error as exc:
        result.error = f"接口异常：{exc}"
    except requests.RequestException as exc:
        result.error = f"网络异常：{exc.__class__.__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - 单账号异常不能拖垮整批
        result.error = f"未预期异常：{exc.__class__.__name__}: {exc}"
        if debug:
            traceback.print_exc()
    return result


def _attach_identity(client: Cloud189Client, result: AccountResult) -> None:
    """尽量补上昵称，日志里更容易对应到人。"""
    try:
        info = client.account_info()
    except Exception:  # noqa: BLE001 - 昵称只是锦上添花，任何失败都忽略
        return
    name = ""
    if isinstance(info, dict):
        name = str(info.get("nickName") or info.get("loginName") or info.get("userName") or "")
    if name:
        result.label = f"{result.label}（{_mask(name)}）"


# --------------------------------------------------------------------- 输出


def now_beijing() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + BEIJING_OFFSET))


def render_report(results: list[AccountResult], check_only: bool) -> str:
    title = "天翼云盘登录体检" if check_only else "天翼云盘签到 / 抽奖"
    lines = [
        f"### {title}",
        "",
        f"- 时间：{now_beijing()}（北京时间）",
        f"- 账号：{len(results)} 个，成功 {sum(r.ok for r in results)} 个",
        "",
        "| # | 账号 | 登录方式 | 结果 |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        if check_only:
            detail = "登录正常" if r.ok else r.error
        elif r.ok:
            detail = f"{r.sign_text}<br>{r.draw_text}"
        else:
            detail = r.error
        lines.append(f"| {r.index + 1} | {r.label} | {r.login_mode or '-'} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    except OSError:
        pass


def notify(config: dict, results: list[AccountResult], check_only: bool) -> None:
    title = ("天翼云盘登录体检" if check_only else "天翼云盘签到") + (
        f" · {sum(r.ok for r in results)}/{len(results)} 成功"
    )
    body = render_report(results, check_only)

    if config["serverchan"]:
        _send_serverchan(config["serverchan"], title, body)
    if config["webhook"]:
        _send_webhook(config["webhook"], title, body, results)


def _send_serverchan(sendkey: str, title: str, body: str) -> None:
    """Server 酱（sct.ftqq.com v3 与旧版 SCU key 都兼容）。"""
    key = sendkey.strip()
    urls = [f"https://sctapi.ftqq.com/{key}.send"]
    if key.upper().startswith(("SCU", "SCT")):
        urls.append(f"https://sc.ftqq.com/{key}.send")
    for url in urls:
        try:
            resp = requests.post(url, data={"title": title, "desp": body}, timeout=15)
            if resp.status_code == 200:
                return
            print(f"通知发送失败：HTTP {resp.status_code} {resp.text[:120]}")
        except requests.RequestException as exc:
            print(f"通知发送异常：{exc}")


def _send_webhook(url: str, title: str, body: str, results: list[AccountResult]) -> None:
    payload = {
        "title": title,
        "text": body,
        "accounts": [
            {
                "label": r.label,
                "ok": r.ok,
                "login_mode": r.login_mode,
                "sign": r.sign_text,
                "draws": [d.text for d in r.draws],
                "error": r.error,
            }
            for r in results
        ],
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code >= 400:
            print(f"Webhook 发送失败：HTTP {resp.status_code} {resp.text[:120]}")
    except requests.RequestException as exc:
        print(f"Webhook 发送异常：{exc}")


# --------------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="天翼云盘自动签到 + 抽奖")
    parser.add_argument("--check", action="store_true", help="只验证登录，不签到")
    parser.add_argument("--no-notify", action="store_true", help="不推送通知")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--debug", action="store_true", help="打印请求细节")
    args = parser.parse_args(argv)

    config = read_config()
    debug = args.debug or config["debug"]
    accounts = build_clients(config)

    if not accounts:
        print("没有配置任何账号：请设置 COOKIE_189（推荐）、SSON 或 USER/PWD。")
        print("排查步骤见 README 的「凭证怎么拿」一节。")
        return 2

    print(f"{DIVIDER}\n{now_beijing()} 北京时间，共 {len(accounts)} 个账号\n{DIVIDER}")

    results: list[AccountResult] = []
    for index, account in enumerate(accounts):
        print(f"[{index + 1}/{len(accounts)}] {account['label']} 处理中 ...", flush=True)
        result = run_account(index, account, check_only=args.check, debug=debug)
        results.append(result)
        if result.ok:
            if args.check:
                print(f"  ✅ 登录正常（{result.login_mode}）")
            else:
                print(f"  ✅ {result.sign_text}")
                for draw in result.draws:
                    print(f"  🎁 {draw.text}")
        else:
            print(f"  ❌ {result.error}")

    report = render_report(results, args.check)
    print(f"\n{report}\n")
    write_step_summary(report)

    if not args.no_notify:
        notify(config, results, args.check)

    if args.json:
        print(
            json.dumps(
                {
                    "time": now_beijing(),
                    "ok": sum(r.ok for r in results),
                    "total": len(results),
                    "accounts": [
                        {
                            "label": r.label,
                            "ok": r.ok,
                            "login_mode": r.login_mode,
                            "sign": r.sign_text,
                            "draws": [d.text for d in r.draws],
                            "error": r.error,
                        }
                        for r in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    # 全部失败才让 CI 变红；部分失败（比如某个号今天抽过奖）不算失败
    return 0 if any(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())