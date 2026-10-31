# 天翼云盘自动签到 + 抽奖

天翼云盘（cloud.189.cn）每日签到 + 抽奖的 GitHub Actions 脚本。
原始代码来自 <https://51.ruyo.net/16050.html>（2020 年），本仓库在 2026-10 做了一次维护性重写。

> **不会取 Cookie？** 不用研究 F12。仓库自带 `get_cookie` 小工具：
> 双击 `bin/get_cookie.exe`（或 `python get_cookie.py`）→ 在弹出的窗口里登录 →
> 脚本自动抓取、校验并复制 Cookie → 去 GitHub 粘贴成 `COOKIE_189` 即可。
> 详细说明见 [取 Cookie 的四种方式](#取-cookie-的四种方式)。

## 这次维护改了什么

| 问题 | 2020 版 | 现在 |
| --- | --- | --- |
| CI 直接红叉 | `setup-python@v2` + Python 3.7，在 `ubuntu-latest`(24.04) 上装不上 | `setup-python@v7` + Python 3.13，带 pip 缓存 |
| 登录链路 | `udb_login.jsp` + 手搓 RSA，页面已 301 跳走 | 现行链路：`unifyLoginForPC` → `encryptConf` → `loginSubmit` → `getSessionForPC` |
| 接口鉴权 | 只靠 Cookie，缺 `sessionKey`，签到接口返回 `date/signature is null` | 两条路：能换到 `sessionKey` 就带上；换不到就让浏览器 Cookie 直接当 web 会话（`cookieUserSession`），签到接口本身也认它 |
| 失效后被当成成功 | 302/HTML 只打印一句就继续 | 识别 302/HTML/`InvalidSessionKey`，自动重新登录重试一次，仍失败则明确报错 |
| 账号密码 | 用 heredoc 从 stdin 喂，多个账号循环同一脚本 | 环境变量直读，多账号互不干扰，单账号异常不影响其他账号 |
| 通知 | 写死了一个 Server 酱 `SCU...` key（已泄露） | 改成可选 `SERVERCHAN_SENDKEY` 密钥，密钥再没进过代码 |
| 结果展示 | 只有 stdout | GitHub Step Summary 表格 + `--json` |
| 定时任务 | 仓库 60 天无提交后 GitHub 会静默停用 schedule | 加了 `保活` 工作流，每月一次空提交 |
| 缺 import | `calculate_md5_sign` 用了 `hashlib` 但没导入 | 删掉了死代码，只留真正用到的 |
| **凭证获取** | 只有一句“去浏览器复制 Cookie”，普通人根本不知道点哪里 | 自带 `get_cookie.exe` / `get_cookie.py`：自动开浏览器、登录、抓 Cookie、校验、复制到剪贴板；手动步骤也补全了 |

> 那个写死的 Server 酱 key 建议在 <https://sct.ftqq.com/> 后台作废重发，它已经在公网上放了 6 年。

## 快速开始

1. **拿一份 Cookie**：下载 [`bin/get_cookie.exe`](bin/get_cookie.exe) 双击运行，或者 `python get_cookie.py`。
   在弹出的浏览器里登录天翼云盘，脚本会自动把 Cookie 复制到剪贴板。
2. **配置 Secret**：Fork 或用自己的仓库 → *Settings → Secrets and variables → Actions →
   New repository secret*。本地跑填 `COOKIE_189` 最省事；**如果要在 GitHub Actions 上跑，
   请先看下面的「GitHub Actions 的两个硬限制」——Cookie 绑定 IP，CI 里通常要用 `SSON`
   或账号口令**。
3. **先体检再正式跑**：*Actions → 签到 → Run workflow*，勾上 `check` 跑一次登录体检，
   绿了再取消勾选跑正式签到。

定时任务每天北京时间 **06:00 / 22:00** 各跑一次。

### 多账号

`COOKIE_189`、`SSON` 用 **换行** 或 **`#`** 分隔（注意：**一条 Cookie 必须写在同一行**，
不能把浏览器里带换行的请求头整段粘进去，否则会被当成两个账号）：

```text
COOKIE_189 = COOKIE_LOGIN_USER=xxx; COOKIE_LOGIN_USER_SSO=yyy
             COOKIE_LOGIN_USER=zzz; COOKIE_LOGIN_USER_SSO=www
```

用 `get_cookie` 抓第二个账号时，浏览器会记住上一个账号的登录态，所以要换一个配置目录：

```bash
python get_cookie.py --profile "%USERPROFILE%\.cloud189-cookie\account2"
```

## 重要：GitHub Actions 的两个硬限制

> **Cookie 会绑定 IP。** 在家/公司浏览器里拿到的 Cookie，云端会记下当时的出口 IP；
> GitHub Actions 每次的出口 IP 都不一样，签到会直接报
> `check ip error - curIp=..., cookiesIp=...`。这不是脚本 bug，**换一份 Cookie 也绕不过去**。
>
> 想在 GitHub Actions 上跑，按这个优先级选：
>
> 1. **`SSON`**：SSO 票据，不绑定浏览器会话 IP。扫码登录 `open.e.189.cn` 后从 Cookie 里取
>    `SSON` 的值，存成 Secret `SSON`（脚本会用它完成 SSO 握手）；
> 2. **`USER` + `PWD`**：先在 <https://e.dlife.cn> 的账号安全里**关闭「设备锁」**，
>    再填账号密码；CI 环境仍可能被图形验证码拦住；
> 3. **自建 runner / 国内固定 IP 的机器**：在同一网络里取 Cookie 并执行签到，
>    Cookie 的 IP 绑定就不会冲突。
>
> 本地跑（自己的电脑）用 `COOKIE_189` 没问题，绑定的就是本机 IP。
>
> **只保留一种凭证。** 如果同时配了 `COOKIE_189`、`SSON`、`USER/PWD`，脚本会把它们当成
> 三个不同的账号逐个跑，失败的那几个会一直让 CI 变红；用 GitHub Actions 时建议只留 `SSON`。

## 取 Cookie 的四种方式

浏览器里的 Cookie 等于“已登录的会话”，拿到它就等于拿到了账号。四种方式任选一种：
**能用工具就别手动**，手动最容易复制错。

### 方式 1：`get_cookie.exe`（最省事，推荐）

适用于 Windows，无需安装 Python。

1. 从仓库下载 [`bin/get_cookie.exe`](bin/get_cookie.exe)（约 15 MB），放到任意可写目录；
2. 双击运行。第一次 Windows 可能弹 SmartScreen 提示“已保护你的电脑”，
   点「更多信息」→「仍要运行」（exe 没有代码签名，属正常现象）；
3. 脚本会自动打开一个**独立的** Edge（没有 Edge 就用 Chrome）窗口，地址是
   `https://cloud.189.cn/web/main`。这个窗口和你平时用的浏览器互不影响；
4. 在窗口里正常登录：手机号+密码、短信、扫码都行；
5. 登录成功后脚本会自动检测到，验证 Cookie 是否真的能登录，然后把完整 Cookie
   复制到剪贴板，并在命令行里打印（打码后的）预览；
6. 命令行最后会停在 **`按回车键退出`**，窗口不会一闪而过；先把 Cookie 复制好，
   再按回车关闭；
7. 去 GitHub：*Settings → Secrets and variables → Actions → New repository secret*，
   Name 填 `COOKIE_189`，Secret 框里 `Ctrl+V`，保存。

> 如果剪贴板被远程桌面/安全软件占用，脚本会打印完整 Cookie，并自动保存一份
> `cookie_COOKIE_189.txt` 到 exe 所在目录；从文件里复制即可，**复制完删掉这个文件**。

脚本会使用 `%LOCALAPPDATA%\cloud189-cookie\profile` 作为独立浏览器配置目录，
所以下次再运行通常还是登录状态，直接秒出 Cookie；过期后重跑并重新登录即可。

> 如果公司电脑禁用了浏览器的调试端口，exe 会提示改用 `--paste`（方式 2）。
> 如果杀毒软件误报，也可以直接用方式 2 跑 Python 脚本，源码就是 [`get_cookie.py`](get_cookie.py)。

### 方式 2：`python get_cookie.py`（同功能，可调参数）

需要 Python 3.8+：

```bash
python -m pip install -r requirements.txt   # 只有做登录校验时才需要这些依赖

python get_cookie.py                  # 自动模式：开浏览器 → 登录 → 抓取 → 复制
python get_cookie.py --paste          # 已经手动复制了 Cookie 到剪贴板，只做清洗+校验+复制
python get_cookie.py --show           # 同时把完整 Cookie 打印出来（远程终端/没有剪贴板时用）
python get_cookie.py --json           # 机器可读输出（含完整 Cookie，注意别贴到公开地方）
python get_cookie.py --no-check       # 跳过联网校验（离线或依赖装不上时）
python get_cookie.py --browser "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
python get_cookie.py --profile "%USERPROFILE%\.cloud189-cookie\account2"   # 多账号分开存
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--paste` | 不启动浏览器，从剪贴板（或管道输入）读 Cookie 后校验、复制 |
| `--show` | 打印完整 Cookie（默认只打印打码预览） |
| `--no-check` | 不调用签到接口校验，只负责取 Cookie |
| `--timeout 600` | 等待登录的秒数，默认 300 |
| `--browser` | `auto` / `edge` / `chrome` / `brave` / `chromium`，或浏览器 exe 的完整路径 |
| `--profile` | 自定义浏览器配置目录（多账号就靠它区分） |
| `--keep-browser` | 提取完成后不关闭浏览器窗口 |
| `--no-pause` | 结束后不停留（脚本/CI 用；默认在控制台里等你按回车） |

脚本原理：用 Chromium 的 DevTools 协议读取“浏览器自己会发给 `cloud.189.cn` 的 Cookie”，
不导出密码、不读浏览历史，也不需要破解 Chrome/Edge 新版 Cookie 加密。

### 方式 3：手动 F12（最通用，但最容易错）

1. 打开 <https://cloud.189.cn>，登录，进到网盘主页（地址栏是 `cloud.189.cn/web/main`）；
2. 按 `F12`（或 `Ctrl+Shift+I`）打开开发者工具；
3. 切到 **网络 / Network** 面板。如果列表是空的，按 `F5` 刷新一次页面；
4. 在过滤框里输入 `web`，点列表里任意一个 `cloud.189.cn` 的请求（类型是 `document`
   或 `xhr` 都行）；
5. 右侧选 **标头 / Headers**，往下找 **请求标头 / Request Headers**；
6. 找到以 `Cookie:` 开头的那一行，它通常很长，双击这一行的值全选，`Ctrl+C` 复制；
   如果界面里看不到，点面板里的 **原始 / Raw** 再找；
7. 复制到的内容应该长这样（只复制 `Cookie:` **后面** 的那一串，不要带 `Cookie:` 前缀）：

   ```text
   COOKIE_LOGIN_USER=8a1f...; COOKIE_LOGIN_USER_SSO=...; guid=...; ...
   ```

8. 如果只想少复制，至少要有 `COOKIE_LOGIN_USER` 和 `COOKIE_LOGIN_USER_SSO` 两项；
9. 把这一行粘到 GitHub Secret `COOKIE_189` 里（**保持一行**）。

手动模式的 4 个高频错误：

| 错误做法 | 后果 |
| --- | --- |
| 把整段 Request Headers（`GET /web/main HTTP/1.1`、`User-Agent` 等）一起复制 | 客户端认不出，校验失败 |
| 只复制了 `COOKIE_LOGIN_USER=...` 一项 | 缺 SSO，换不到 `sessionKey` |
| 从 `open.e.189.cn`（登录页）复制 | 那里没有 `COOKIE_LOGIN_USER`，应改用 SSON 或按上面的步骤从网盘页复制 |
| 复制后带换行/多行 | 会被当成多个账号；必须压成一行 |

复制完可以用 `python get_cookie.py --paste` 让它清洗并校验，比肉眼检查靠谱。

### 方式 4：`SSON`（GitHub Actions 上最稳的凭证）

`SSON` 是 SSO 票据，不绑定浏览器会话 IP，所以 CI 里也能用。取法：

1. 浏览器打开 <https://open.e.189.cn>，用天翼账号登录（扫码或密码都行）；
2. `F12` → **Application / 应用** → **Cookies** → 选 `https://open.e.189.cn`；
3. 找到名为 `SSON` 的那一项，复制它的 **Value**（通常是一长串字符）；
4. GitHub → *Settings → Secrets → Actions* → New repository secret，Name 填 `SSON`，粘贴保存。

脚本会用 `SSON` 走 `unifyLoginForPC → SSO 握手 → getSessionForPC` 换 `sessionKey`。
在 `get_cookie` 的输出里如果看到「检测到 SSON」，也可以直接用那一项。

### 方式 5：`USER` + `PWD`（仅兼容老配置）

保留只是为了兼容。天翼云盘现在经常要求图形/短信验证码，CI 环境无法通过，
脚本会直接告诉你「请改用 COOKIE_189」，不会假装成功。

### 三种凭证怎么选

| 凭证 | 适合场景 | 说明 |
| --- | --- | --- |
| `COOKIE_189` | 本地 / 固定 IP 机器 | 最省事，但**绑定获取时的 IP**；GitHub Actions 上会 `check ip error` |
| `SSON` | GitHub Actions | SSO 票据，不绑定浏览器会话 IP；扫码登录后取 |
| `USER` + `PWD` | GitHub Actions（关设备锁） | 可能被图形验证码/设备锁拦截，失败时提示怎么处理 |

## 本地运行

```bash
python -m pip install -r requirements.txt

# 只验证登录
COOKIE_189='COOKIE_LOGIN_USER=xxx; COOKIE_LOGIN_USER_SSO=yyy' python checkin.py --check

# 正式签到 + 抽奖，并打印 JSON 结果
COOKIE_189='...' python checkin.py --json

# 离线单测（不需要网络和账号）
python -m unittest discover -s tests -v
```

`checkin.py` 参数：

| 参数 | 作用 |
| --- | --- |
| `--check` | 只登录、不签到，用来排查凭证 |
| `--no-notify` | 签到但不推送 |
| `--json` | 追加机器可读结果 |
| `--debug` | 打印请求细节（不会打印 Cookie 明文） |

环境变量：`COOKIE_189` / `SSON` / `USER` / `PWD` / `SERVERCHAN_SENDKEY` / `NOTIFY_WEBHOOK` / `DEBUG`。

通知都是可选的：`SERVERCHAN_SENDKEY` 走 Server 酱（v3 的 `sctapi` 与老 `SCU` key 都兼容），
`NOTIFY_WEBHOOK` 会 POST 一份 JSON。

## 重新打包 exe

`bin/get_cookie.exe` 由 `get_cookie.py` 打包而来，改完源码后运行：

```bat
build_exe.bat
```

（等价命令：`python -m PyInstaller --onefile --console --name get_cookie --runtime-tmpdir . get_cookie.py`）

## 排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `没有配置任何账号` | 三个 secret 都没设 | 至少配 `COOKIE_189` |
| `登录失败：凭证已失效` / `InvalidSessionKey` | Cookie 过期 | 重新跑 `get_cookie` 取一份 |
| `Cookie 没有有效登录态…cookieUserSession is null or invalid` | Cookie 不是登录后的完整会话（过期、只复制了一部分、或填了带 `***` 的预览） | 重新跑 `get_cookie`（它会先校验再复制），确认整行粘贴 |
| `Network is unreachable`（`open.e.189.cn`） | GitHub Actions 出口到登录域不通（常见于 IPv6 无路由或被拦截） | 脚本已默认强制 IPv4；仍不通就换 `SSON` |
| `check ip error - curIp=..., cookiesIp=...` | **Cookie 绑定 IP**：GitHub Actions 出口 IP 与取 Cookie 时的 IP 不同 | 无法用代码绕过。改用 `SSON`、账号口令（关设备锁），或自建 runner/固定 IP 机器 |
| `接口返回登录页（HTML）` | 同上，服务端直接跳登录页 | 同上 |
| `登录失败：请输入验证码…` | 口令登录被验证码拦截 | 改用 `COOKIE_189` |
| `登录失败：账户名或密码错误` | 密码确实不对，或天翼云盘已不再接受该账号的网页口令登录 | 先去官网确认密码能不能登网页；能登也报错就直接上 `COOKIE_189` |
| 双击 exe 没反应 / 被 SmartScreen 拦 | exe 无代码签名 | 点「更多信息 → 仍要运行」；或改用 `python get_cookie.py` |
| 窗口一闪而过，来不及复制 | 极少数环境 `isatty` 判定异常 | 用 `cmd` 运行 `get_cookie.exe`；或加 `--out cookie.txt` 保存到文件 |
| 剪贴板不可用 / 复制不出来 | 远程桌面、剪贴板被占用 | 从脚本打印的完整 Cookie 或 `cookie_COOKIE_189.txt` 里复制 |
| exe 报 `Could not create temporary directory` | 所在目录不可写 / 临时目录被策略限制 | 把 exe 放到 `下载`、桌面等可写目录再运行；或改用 Python 脚本 |
| `没找到可用的 Chromium 内核浏览器` | 没装 Edge/Chrome，或装在非标准路径 | 装一个 Edge；或 `--browser` 指定 exe 完整路径 |
| `DevTools 端口没有打开` | 公司策略禁用调试端口 | 改用 `python get_cookie.py --paste` 手动模式 |
| 等了很久没反应 | 浏览器里还没登录，或扫码没在手机上确认 | 在窗口里完成登录；等待时间可加 `--timeout 600` |
| `Cookie 里没有 COOKIE_LOGIN_USER` | 复制错了页面/请求 | 从 `cloud.189.cn` 网盘页复制，不要用登录页 |
| `登录校验：未通过` | Cookie 不完整或已过期 | 确认整条 Cookie 是**一行**；过期就重新取 |
| `剪贴板不可用` | 远程终端/无桌面会话 | 用 `--show` 把完整 Cookie 打印出来再手动复制 |
| workflow 一直不触发 | 仓库 60 天无提交，GitHub 停用了 schedule | Actions 页点 `Enable`，或手动 `Run workflow`；`保活` 工作流会预防这个问题 |
| `Actions` 里没有「签到」 | fork 后默认没有启用 | 点 *I understand my workflows, go ahead and enable them* |

## 项目结构

```text
checkin.py                    # CLI：账号编排、签到/抽奖、汇总、通知
cloud189.py                   # 客户端：登录、sessionKey、签到、抽奖接口
get_cookie.py                 # Cookie 提取工具：开浏览器登录 → 抓取 → 校验 → 复制
bin/get_cookie.exe            # 上面这个脚本的 Windows 免安装版（约 15 MB）
build_exe.bat                 # 重新打包 exe 的一行命令
tests/test_checkin.py         # 离线单测（mock 网络）
.github/workflows/run.yml         # 签到主流程
.github/workflows/keepalive.yml   # 每月空提交，防止 schedule 被停用
requirements.txt
```

## 安全提醒

- Cookie 就等于账号密码：**不要发群、不要截图、不要提交到仓库**，只放在 GitHub Secret 里；
- `get_cookie` 只在本机运行，不上传任何数据：Cookie 只写到剪贴板（或 `--out` 指定的文件）；
- 它使用独立的浏览器配置目录，不会动你平时的浏览器资料；
- 万一 Cookie 泄露：去天翼云盘官网「退出所有设备」并改密码，然后重新取一份。

## 免责声明

仅供个人账号自动化日常签到使用。脚本完全依赖天翼云盘非公开接口，接口变更后可能失效；
请自行评估账号风险，不要用于批量、商业或任何违反服务条款的场景。