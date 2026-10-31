#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""天翼云盘 Cookie 一键提取工具（给不想研究 F12 的人用）。

普通用户只需要三步：
1. 双击 ``get_cookie.exe``（或运行 ``python get_cookie.py``）；
2. 在弹出的浏览器窗口里登录天翼云盘（扫码、短信、账号密码都行）；
3. 脚本检测到登录后自动抓取 Cookie 并复制到剪贴板，去 GitHub 新建
   Secret ``COOKIE_189``，直接粘贴保存即可。

其他用法：

    python get_cookie.py --paste      # 已经手动复制了 Cookie，只做清洗/校验/复制
    python get_cookie.py --show       # 同时把完整 Cookie 打印到屏幕（远程终端用）
    python get_cookie.py --json       # 机器可读输出（含完整 Cookie，别贴到公开地方）
    python get_cookie.py --no-check   # 跳过登录校验（离线或临时排错时用）
    python get_cookie.py --browser chrome --timeout 600
    python get_cookie.py --profile D:\\cookie-profile --keep-browser

设计说明：
- 用 Chromium 的 DevTools 协议读取“浏览器自己会发给 cloud.189.cn 的
  Cookie”，因此不需要解密浏览器数据库，也不受 Chrome/Edge 新版
  加密方式（v20 app-bound）影响；
- 使用独立的浏览器配置目录（默认 ``~/.cloud189-cookie/profile``），
  不影响你日常用的浏览器；登录一次可复用，过期后重跑即可；
- 全程不抓账号密码、不读取浏览历史，只取 ``*.189.cn`` 的 Cookie。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - 输出被重定向/包裹时忽略
        pass

__version__ = "1.1.1"

WEB_MAIN = "https://cloud.189.cn/web/main"
LOGIN_COOKIE_NAMES = ("COOKIE_LOGIN_USER", "COOKIE_LOGIN_USER_SSO")
COOKIE_HINT = "COOKIE_LOGIN_USER"

DEFAULT_TIMEOUT = 300.0
POLL_INTERVAL = 1.2

DIVIDER = "=" * 62


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def mask(text: str) -> str:
    """日志里只露出头尾，避免把凭证写进截图/日志。"""
    text = str(text)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:3]}***{text[-2:]}"


def normalize_cookie(raw: str) -> str:
    """把用户粘贴/浏览器返回的 Cookie 清洗成 ``a=1; b=2`` 形式。

    容错处理：BOM、首尾引号、``Cookie:`` 前缀、换行、多余分号/空格、
    重复的 Cookie 名（保留第一次出现的值）。
    """
    raw = (raw or "").replace("\ufeff", "").strip()
    if not raw:
        return ""
    if raw[:1] in ("'", '"') and raw[-1:] == raw[:1]:
        raw = raw[1:-1].strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:") :].strip()

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in raw.replace("\r", "\n").split("\n"):
        line = line.strip()
        if line.lower().startswith("cookie:"):
            line = line[len("cookie:") :].strip()
        for part in line.split(";"):
            part = part.strip().strip("'\"")
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if not name or any(ch in name for ch in " \t"):
                continue
            if name in seen:  # 同名 Cookie 保留第一个，避免请求头里出现重复项
                continue
            seen.add(name)
            pairs.append((name, value))
    return "; ".join(f"{name}={value}" for name, value in pairs)


def cookie_names(cookie: str) -> set[str]:
    names: set[str] = set()
    for part in cookie.split(";"):
        name = part.strip().partition("=")[0].strip()
        if name:
            names.add(name)
    return names


def looks_logged_in(cookie: str) -> bool:
    names = cookie_names(cookie)
    return any(name in names for name in LOGIN_COOKIE_NAMES)


def cookie_value(cookie: str, name: str) -> str:
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def cookie_preview(cookie: str) -> str:
    parts = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        parts.append(f"{name.strip()}={mask(value.strip())}")
    return "; ".join(parts)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} 字节"
    return f"{n / 1024:.1f} KB"


# ---------------------------------------------------------------------------
# 剪贴板（Windows 原生，macOS/Linux 走系统命令）
# ---------------------------------------------------------------------------


def clipboard_write(text: str) -> bool:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
            user32.SetClipboardData.restype = ctypes.c_void_p
            user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

            data = (text + "\x00").encode("utf-16-le")
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)

            if not user32.OpenClipboard(None):
                kernel32.GlobalFree(handle)
                return False
            try:
                user32.EmptyClipboard()
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    kernel32.GlobalFree(handle)
                    return False
                handle = None  # 所有权已交给系统
            finally:
                user32.CloseClipboard()
            return True
        except Exception:
            return False

    commands = [["pbcopy"]] if sys.platform == "darwin" else [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for command in commands:
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text.encode("utf-8"), check=True)
                return True
            except Exception:
                continue
    return False


def clipboard_read() -> str:
    if os.name == "nt":
        try:
            import ctypes

            CF_UNICODETEXT = 13
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            user32.GetClipboardData.restype = ctypes.c_void_p
            user32.GetClipboardData.argtypes = [ctypes.c_uint]

            if not user32.OpenClipboard(None):
                return ""
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    return ""
                try:
                    return ctypes.wstring_at(pointer)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        except Exception:
            return ""

    commands = [["pbpaste"]] if sys.platform == "darwin" else [["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]
    for command in commands:
        if shutil.which(command[0]):
            try:
                out = subprocess.run(command, capture_output=True, check=True)
                return out.stdout.decode("utf-8", "replace")
            except Exception:
                continue
    return ""


# ---------------------------------------------------------------------------
# 找浏览器
# ---------------------------------------------------------------------------


def _wheel_paths(kind: str) -> list[str]:
    home = Path.home()
    if os.name == "nt":
        roots = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")),
        ]
        rel = {
            "edge": [r"Microsoft\Edge\Application\msedge.exe"],
            "chrome": [r"Google\Chrome\Application\chrome.exe", r"Chromium\Application\chrome.exe"],
            "brave": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
            "chromium": [r"Chromium\Application\chrome.exe"],
        }
        return [os.path.join(root, sub) for root in roots for sub in rel.get(kind, [])]

    if sys.platform == "darwin":
        rel = {
            "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
            "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
            "brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
            "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
        }
        return list(rel.get(kind, []))
    return []


BROWSER_NAMES = {
    "edge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
    "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
    "brave": ["brave-browser", "brave"],
    "chromium": ["chromium", "chromium-browser"],
}


def find_browser(preference: str = "auto") -> tuple[str, str, str]:
    """返回 (可执行文件, 浏览器类型, 人类可读名称)。"""
    order = ["edge", "chrome", "brave", "chromium"] if preference == "auto" else [preference]
    for kind in order:
        for candidate in _wheel_paths(kind):
            if os.path.isfile(candidate):
                return candidate, kind, browser_label(kind)
        if os.name != "nt":
            for name in BROWSER_NAMES.get(kind, []):
                found = shutil.which(name)
                if found:
                    return found, kind, browser_label(kind)
    # 用户直接给了一个路径
    if preference and os.path.isfile(preference):
        return preference, "custom", os.path.basename(preference)
    raise FileNotFoundError(preference)


def browser_label(kind: str) -> str:
    return {"edge": "Microsoft Edge", "chrome": "Google Chrome", "brave": "Brave", "chromium": "Chromium"}.get(kind, kind)


def default_profile_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "cloud189-cookie" / "profile"


def launch_browser(exe: str, profile_dir: Path, url: str) -> subprocess.Popen:
    """用独立配置目录启动浏览器并开启 DevTools 端口。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        url,
    ]
    windows = os.name == "nt"
    creationflags = 0
    if windows:
        # 不弹黑色控制台窗口（浏览器自己就是 GUI）
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


# ---------------------------------------------------------------------------
# 极简 WebSocket（只为读 DevTools 协议，不引入第三方依赖）
# ---------------------------------------------------------------------------


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        if not url.startswith("ws://"):
            raise ValueError(f"只支持 ws:// 地址：{url}")
        hostport, _, path = url[5:].partition("/")
        self.path = "/" + path
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = b""
        self._next_id = 0

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("utf-8"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools 握手失败：连接被关闭")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        self._buffer = rest
        if b" 101 " not in header.split(b"\r\n")[0]:
            raise ConnectionError(f"DevTools 握手失败：{header.decode('utf-8', 'replace')[:200]}")

    # frame ----------------------------------------------------------------
    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DevTools 连接已断开")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _send(self, payload: str, opcode: int = 0x1) -> None:
        data = payload.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv(self) -> str:
        chunks = b""
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else None
            data = self._read_exact(length)
            if mask:
                data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
            if opcode == 0x8:  # close
                raise ConnectionError("DevTools 连接已关闭")
            if opcode == 0x9:  # ping
                self._send(data.decode("utf-8", "replace"), opcode=0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            chunks += data
            if first & 0x80:  # FIN
                return chunks.decode("utf-8", "replace")
            # 否则继续收后续 continuation 帧

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self._next_id += 1
        message: dict = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._send(json.dumps(message))
        wanted = self._next_id
        while True:
            response = json.loads(self.recv())
            if response.get("id") == wanted:
                if "error" in response:
                    error = response["error"]
                    raise RuntimeError(f"{method} 失败：{error.get('message') or error}")
                return response.get("result", {})
            # 其它事件（Page.* / Network.*）直接忽略

    def close(self) -> None:
        try:
            self._send("", opcode=0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 抓 Cookie
# ---------------------------------------------------------------------------


def read_devtools_port(profile_dir: Path, proc: subprocess.Popen, timeout: float = 40.0) -> tuple[int, str] | None:
    """等待浏览器写出 DevToolsActivePort（端口 + browser 级 ws 路径）。"""
    port_file = profile_dir / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None and not port_file.exists():
            return None
        if port_file.exists():
            try:
                raw = port_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                raw = []
            if len(raw) >= 2 and raw[0].strip().isdigit():
                port = int(raw[0].strip())
                # 端口文件也可能是上一次运行遗留的，确认端口确实活着
                if _devtools_alive(port):
                    return port, raw[1].strip()
        time.sleep(0.3)
    return None


def _devtools_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            return b"webSocketDebuggerUrl" in response.read()
    except Exception:
        return False


def _attach_page(ws: WebSocket) -> str:
    """找到（或新建）一个页面并 attach，返回 sessionId。

    优先复用已经打开 189 域名的标签页，避免把用户正在操作的页面导航走。
    """
    targets = ws.call("Target.getTargets").get("targetInfos", [])
    pages = [t for t in targets if t.get("type") == "page"]
    page = next((t for t in pages if "189.cn" in str(t.get("url", ""))), None)
    if page is None and pages:
        page = pages[0]
    if page is None:
        target_id = ws.call("Target.createTarget", {"url": WEB_MAIN})["targetId"]
    else:
        target_id = page["targetId"]

    session = ws.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
    ws.call("Network.enable", {}, session=session)
    ws.call("Page.enable", {}, session=session)
    current_url = str(page.get("url", "")) if page else ""
    if "189.cn" not in current_url:
        try:
            ws.call("Page.navigate", {"url": WEB_MAIN}, session=session)
        except Exception:
            pass
    return session


def cookies_for_url(ws: WebSocket, session: str, url: str) -> list[dict]:
    result = ws.call("Network.getCookies", {"urls": [url]}, session=session)
    return result.get("cookies", [])


def cookies_to_header(cookies: list[dict]) -> str:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    # path=/ 的 Cookie 优先，其次域名长的优先，最后按名字排序，保证输出稳定
    def sort_key(item: dict) -> tuple:
        domain = str(item.get("domain", ""))
        path = str(item.get("path", "/"))
        return (0 if path == "/" else 1, -len(domain), str(item.get("name", "")))

    for cookie in sorted(cookies, key=sort_key):
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        if not name or not value or name in seen:
            continue
        seen.add(name)
        pairs.append((name, value))
    return "; ".join(f"{name}={value}" for name, value in pairs)


def wait_for_login(
    ws: WebSocket,
    session: str,
    timeout: float,
    verbose: bool = True,
    validator=None,
) -> tuple[str, str]:
    """轮询 cloud.189.cn 的 Cookie，直到出现登录态。

    validator 用来在“抓到 Cookie”后立刻验证一次：如果浏览器里残留的是
    过期 Cookie，就提示用户重新登录并继续等待，而不是直接失败退出。
    """
    start = time.time()
    last_tip = start
    tried: set[str] = set()
    failures = 0
    while True:
        if time.time() - start > timeout:
            raise TimeoutError(f"等待登录超时（{int(timeout)} 秒）")
        try:
            cookies = cookies_for_url(ws, session, WEB_MAIN)
        except Exception:
            # 页面可能被关掉/跳转，重新 attach 一个页面继续
            session = _attach_page(ws)
            cookies = cookies_for_url(ws, session, WEB_MAIN)

        cookie = cookies_to_header(cookies)
        if cookie and looks_logged_in(cookie):
            if validator is None:
                return cookie, ""
            if cookie not in tried:
                tried.add(cookie)
                if verbose:
                    print(" 检测到登录态，正在验证这份 Cookie…")
                ok, message = validator(cookie)
                if message.startswith("跳过校验"):
                    if verbose:
                        print(f" {message}")
                    return cookie, ""
                if ok:
                    if verbose:
                        print_check_result(True, message)
                    return cookie, (mask(message) if message else "")
                failures += 1
                if verbose:
                    print(f" ⚠️  这份 Cookie 还不能用：{message}")
                    print("    如果刚登录，请稍等几秒；如果是旧的登录态，请在浏览器里重新登录…")
                if failures == 2 and verbose:
                    print("    多次校验失败？若浏览器能正常上网，可能是脚本走了另一个网络出口，")
                    print("    可以加 --no-check 跳过校验，直接使用抓到的 Cookie。")
            # 继续等待，直到 Cookie 变化或被校验通过

        if verbose and time.time() - last_tip > 20:
            waited = int(time.time() - start)
            print(f"    …已等待 {waited} 秒，还在等你登录（浏览器窗口别关）")
            last_tip = time.time()
        time.sleep(POLL_INTERVAL)


def fetch_cookie_in_browser(args: argparse.Namespace) -> tuple[str, str]:
    profile_dir = Path(args.profile).expanduser() if args.profile else default_profile_dir()
    try:
        exe, kind, name = find_browser(args.browser)
    except FileNotFoundError:
        raise RuntimeError(
            "没找到可用的 Chromium 内核浏览器（Chrome / Edge / Brave）。\n"
            "请先安装 Edge 或 Chrome，再用 --browser 指定路径；\n"
            "也可以改用 --paste 手动粘贴 Cookie。"
        )

    print(DIVIDER)
    print(" 天翼云盘 Cookie 提取（自动模式）")
    print(DIVIDER)
    print(f" 浏览器：{name}（{exe}）")
    print(f" 配置目录：{profile_dir}")
    print(f" 登录页：{WEB_MAIN}")
    print(" 说明：这里用的是独立配置目录，不影响你平时用的浏览器。")
    print()

    proc = launch_browser(exe, profile_dir, WEB_MAIN)
    try:
        port_info = read_devtools_port(profile_dir, proc)
        if port_info is None:
            raise RuntimeError(
                "浏览器已启动，但 DevTools 端口没有打开（可能被公司策略禁用，或浏览器启动失败）。\n"
                "请改用 --paste：按 README 的手动步骤复制 Cookie 后重跑；\n"
                "也可以加 --browser 指定浏览器完整路径再试。"
            )
        port, ws_path = port_info
        ws = WebSocket(f"ws://127.0.0.1:{port}{ws_path}")
        try:
            ws.call("Browser.getVersion")
            session = _attach_page(ws)
            print(" 已打开浏览器。请在弹出的窗口里登录天翼云盘（扫码/短信/密码均可）。")
            print(" 登录成功后脚本会自动继续，不用手动复制任何东西…")
            print()
            validator = None
            if not args.no_check:
                validator = lambda value: validate_cookie(value, args.debug)  # noqa: E731
            cookie, account = wait_for_login(ws, session, args.timeout, validator=validator)
        finally:
            # 先让浏览器自己正常退出（会落盘 Cookie/缓存），再兜底强杀
            if not args.keep_browser:
                try:
                    ws.call("Browser.close")
                except Exception:
                    pass
            ws.close()
        return cookie, account
    finally:
        if args.keep_browser:
            print(" （--keep-browser：浏览器窗口保留，用完后请自行关闭）")
        else:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_cookie(cookie: str, debug: bool = False) -> tuple[bool, str]:
    """用仓库里的 cloud189 客户端真正登录一次，确认 Cookie 可用。"""
    try:
        from cloud189 import AuthError, Cloud189Client, Cloud189Error, SessionExpired
    except Exception as exc:  # noqa: BLE001 - 缺依赖时不影响主流程
        return False, f"跳过校验（无法导入 cloud189：{exc}）"

    client = Cloud189Client(cookie=cookie, debug=debug)
    try:
        client.login()
        info = client.account_info() or {}
        nick = ""
        if isinstance(info, dict):
            nick = str(info.get("nickName") or info.get("loginName") or info.get("userName") or "")
        return True, nick
    except AuthError as exc:
        return False, f"Cookie 已失效或不是登录态：{exc}"
    except SessionExpired as exc:
        return False, f"会话失效：{exc}"
    except Cloud189Error as exc:
        return False, f"接口返回异常：{exc}"
    except Exception as exc:  # noqa: BLE001 - 网络波动等
        return False, f"校验失败（{exc.__class__.__name__}）：{exc}"


# ---------------------------------------------------------------------------
# 输出与保存
# ---------------------------------------------------------------------------


def emit_cookie(cookie: str, args: argparse.Namespace, account: str = "") -> int:
    copied = False if args.no_clipboard else clipboard_write(cookie)

    if args.json:
        payload = {
            "ok": True,
            "cookie": cookie,
            "cookie_count": len(cookie_names(cookie)),
            "length": len(cookie),
            "account": account,
            "copied": copied,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.out:
            _write_out(args.out, cookie)
        return 0

    print(DIVIDER)
    print(f" 完成！共 {len(cookie_names(cookie))} 项 Cookie，{human_size(len(cookie))}")
    if account:
        print(f" 登录账号：{account}")
    print(f" 预览：{cookie_preview(cookie)}")
    sson = cookie_value(cookie, "SSON")
    if sson:
        print(f" 另外检测到 SSON（{mask(sson)}），CI 里也可以改用 Secret SSON")
    if copied:
        print(" 完整 Cookie 已复制到剪贴板 ✅（直接去 GitHub 粘贴即可）")
    else:
        print(" ⚠️ 剪贴板不可用，完整 Cookie 已在下方打印；")
        print("    可以右键控制台 →「标记」，选中 Cookie 后按回车复制。")
        if not args.out and not args.no_clipboard:
            fallback = Path.cwd() / "cookie_COOKIE_189.txt"
            if _write_out(str(fallback), cookie):
                print(f"    另外已保存到文件：{fallback}")
                print("    （复制完请删除该文件，里面就是你的账号凭证）")
    if args.show or not copied:
        print()
        print(cookie)
    print(DIVIDER)
    if args.out and _write_out(args.out, cookie):
        print(f" 已写入文件：{args.out}")
    print(" 下一步：")
    print("   1. 打开 GitHub 仓库 → Settings → Secrets and variables → Actions")
    print("   2. 点 New repository secret，Name 填 COOKIE_189")
    print("   3. Secret 输入框里 Ctrl+V 粘贴，保存")
    print(" 多账号：每个账号跑一次本脚本，把得到的 Cookie 用换行或 # 连起来，")
    print("        一起放进同一个 COOKIE_189（详见 README「多账号」）。")
    print(" CI 注意：Cookie 绑定获取时的 IP，GitHub Actions 出口 IP 不同会报 check ip error；")
    print("          CI 请优先用 SSON 或账号口令（先关闭设备锁），详见 README「两个硬限制」。")
    print(" 安全提醒：Cookie 等于账号密码，别发群、别截图、别提交到仓库。")
    return 0


def _write_out(path: str, cookie: str) -> bool:
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cookie + "\n", encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return True
    except OSError as exc:
        print(f" 写入文件失败：{exc}")
        return False


def print_check_result(ok: bool, message: str) -> None:
    if ok:
        print(f" 登录校验：通过 ✅ {('（账号：' + mask(message) + '）') if message else ''}")
    else:
        print(f" 登录校验：未通过 ❌ {message}")


# ---------------------------------------------------------------------------
# 三种入口
# ---------------------------------------------------------------------------


def run_paste(args: argparse.Namespace) -> int:
    print(DIVIDER)
    print(" 天翼云盘 Cookie 提取（粘贴模式）")
    print(DIVIDER)
    if not sys.stdin.isatty():
        # 管道/重定向进来的输入优先，方便脚本化
        source = "标准输入"
        raw = sys.stdin.read()
    else:
        source = "剪贴板"
        raw = clipboard_read()
        if not raw.strip():
            source = "手动输入"
            print(" 剪贴板里没有内容。请把浏览器里复制的 Cookie 粘贴到这里，然后回车：")
            raw = input(" Cookie> ")
        else:
            print(" 已从剪贴板读取到内容。")
    cookie = normalize_cookie(raw)
    if not cookie:
        print(" ❌ 没有读到有效的 Cookie。请在浏览器 F12 → 网络 → 请求头里复制整条 Cookie。")
        return 2
    if not looks_logged_in(cookie):
        print(f" ⚠️  这段 Cookie 里没有 {COOKIE_HINT}，大概率不是登录后的 Cookie。")
        print("    确认复制的是 cloud.189.cn 的请求头，而不是 open.e.189.cn 的。")
    print(f" 识别到 {len(cookie_names(cookie))} 项 Cookie（来源：{source}）。")

    account = ""
    if not args.no_check:
        print(" 正在用这份 Cookie 登录校验…")
        ok, message = validate_cookie(cookie, args.debug)
        print_check_result(ok, message)
        if ok:
            account = mask(message) if message else ""
        elif "跳过校验" not in message:
            print(" 校验没通过就不要继续配置了，按 README 重新获取一份。")
            if not args.force:
                return 3
            print(" （--force：仍然输出这份 Cookie）")
    return emit_cookie(cookie, args, account)


def run_auto(args: argparse.Namespace) -> int:
    cookie, account = fetch_cookie_in_browser(args)
    print()
    if args.no_check:
        print(" （--no-check：跳过登录校验，请自行确认 Cookie 是登录后复制的）")
    return emit_cookie(cookie, args, account)


def wait_for_exit(args: argparse.Namespace) -> None:
    """双击运行时停在最后，给用户留出复制 Cookie 的时间。

    只在“控制台交互”时暂停：管道/重定向/CI 不会被卡住；--no-pause 可关闭。
    """
    if getattr(args, "no_pause", False):
        return
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return
        if not (sys.stdout and sys.stdout.isatty()):
            return
    except (ValueError, OSError):
        return
    try:
        input("按回车键退出（先复制上面的 Cookie）… ")
    except (EOFError, KeyboardInterrupt):
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="get_cookie",
        description="天翼云盘 Cookie 一键提取：自动打开浏览器登录，抓取 Cookie 并复制到剪贴板。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  get_cookie.exe                          # 双击/回车即用，自动模式
  python get_cookie.py --paste            # 已经手动复制了 Cookie
  python get_cookie.py --show --no-check  # 打印完整 Cookie（远程终端用）
  python get_cookie.py --browser "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
""",
    )
    parser.add_argument("--paste", action="store_true", help="不启动浏览器，从剪贴板/标准输入读取 Cookie 后校验")
    parser.add_argument("--browser", default="auto", help="auto / edge / chrome / brave / chromium，或浏览器 exe 的完整路径")
    parser.add_argument("--profile", default="", help="自定义浏览器配置目录（默认 ~/.cloud189-cookie/profile）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"等待登录的秒数（默认 {int(DEFAULT_TIMEOUT)}）")
    parser.add_argument("--no-check", action="store_true", help="跳过登录校验（不导入 cloud189、不联网验证）")
    parser.add_argument("--no-clipboard", action="store_true", help="不写剪贴板，只打印")
    parser.add_argument("--show", action="store_true", help="打印完整 Cookie（默认只打印打码预览）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（包含完整 Cookie，注意使用场景）")
    parser.add_argument("--out", default="", help="把 Cookie 写入指定文件（方便本地留存）")
    parser.add_argument("--keep-browser", action="store_true", help="提取完成后保留浏览器窗口")
    parser.add_argument("--force", action="store_true", help="校验失败也照样输出 Cookie")
    parser.add_argument("--no-pause", action="store_true", help="结束后不停留（脚本/自动化用；默认在控制台里等你按回车）")
    parser.add_argument("--debug", action="store_true", help="打印调试信息")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.timeout <= 0:
            print(" --timeout 必须是正数")
            return 2
        if args.paste:
            return run_paste(args)
        return run_auto(args)
    except KeyboardInterrupt:
        print("\n 已取消。")
        return 130
    except TimeoutError as exc:
        print(f"\n ❌ {exc}")
        print(" 浏览器窗口里的登录完成了吗？如果是扫码登录，请确认手机上已确认；")
        print(" 也可以加长等待时间：--timeout 600")
        return 2
    except RuntimeError as exc:
        print(f"\n ❌ {exc}")
        return 2
    except FileNotFoundError as exc:
        print(f"\n ❌ 找不到文件：{exc}")
        return 2
    except ConnectionError as exc:
        print(f"\n ❌ {exc}")
        print(" 浏览器窗口可能被关掉了，重新运行一次即可；也可以改用 --paste 手动模式。")
        return 2
    except Exception as exc:  # noqa: BLE001 - 最后一道防线，给普通人看得懂的提示
        if args.debug:
            import traceback

            traceback.print_exc()
        print(f"\n ❌ 出错了：{exc.__class__.__name__}: {exc}")
        print(" 可以加 --debug 查看详细信息；或改用 --paste 手动模式。")
        return 1
    finally:
        wait_for_exit(args)


if __name__ == "__main__":
    sys.exit(main())