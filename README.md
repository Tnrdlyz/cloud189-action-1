# 天翼云盘自动签到 + 抽奖

天翼云盘（cloud.189.cn）每日签到 + 抽奖的 GitHub Actions 脚本。
原始代码来自 <https://51.ruyo.net/16050.html>（2020 年），本仓库在 2026-10 做了一次维护性重写。

## 这次维护改了什么

| 问题 | 2020 版 | 现在 |
| --- | --- | --- |
| CI 直接红叉 | `setup-python@v2` + Python 3.7，在 `ubuntu-latest`(24.04) 上装不上 | `setup-python@v7` + Python 3.13，带 pip 缓存 |
| 登录链路 | `udb_login.jsp` + 手搓 RSA，页面已 301 跳走 | 现行链路：`unifyLoginForPC` → `encryptConf` → `loginSubmit` → `getSessionForPC` |
| 接口鉴权 | 只靠 Cookie，缺 `sessionKey`，签到接口返回 `date/signature is null` | 登录后取 `sessionKey`，所有 `cloud.189.cn` 请求都带上 |
| 失效后被当成成功 | 302/HTML 只打印一句就继续 | 识别 302/HTML/`InvalidSessionKey`，自动重新登录重试一次，仍失败则明确报错 |
| 账号密码 | 用 heredoc 从 stdin 喂，多个账号循环同一脚本 | 环境变量直读，多账号互不干扰，单账号异常不影响其他账号 |
| 通知 | 写死了一个 Server 酱 `SCU...` key（已泄露） | 改成可选 `SERVERCHAN_SENDKEY` 密钥，密钥再没进过代码 |
| 结果展示 | 只有 stdout | GitHub Step Summary 表格 + `--json` |
| 定时任务 | 仓库 60 天无提交后 GitHub 会静默停用 schedule | 加了 `保活` 工作流，每月一次空提交 |
| 缺 import | `calculate_md5_sign` 用了 `hashlib` 但没导入 | 删掉了死代码，只留真正用到的 |

> 那个写死的 Server 酱 key 建议在 <https://sct.ftqq.com/> 后台作废重发，它已经在公网上放了 6 年。

## 快速开始

1. **Fork 或直接用自己的这个仓库**，进入 *Settings → Secrets and variables → Actions*。
2. **加凭证**（三选一，推荐第一个）：
   - `COOKIE_189`：浏览器里 `cloud.189.cn` 的 Cookie 串，最稳，不会遇到验证码；
   - `SSON`：`open.e.189.cn` 的 SSON Cookie，走扫码登录那条路；
   - `USER` + `PWD`：手机号 + 密码（云端弹验证码时会失败，只作兼容保留）。
3. **运行一次**：*Actions → 签到 → Run workflow*，勾上 `check` 先做登录体检，确认绿的再跑正式签到。
4. 定时：每天北京时间 **06:00 / 22:00** 各跑一次。

### 多账号

`COOKIE_189`、`SSON` 用 **换行** 或 **`#`** 分隔；老的 `USER`/`PWD` 仍按 `#` 分隔、两条列表一一对应：

```text
COOKIE_189 = COOKIE_LOGIN_USER=xxx; COOKIE_LOGIN_USER_SSO=yyy
             COOKIE_LOGIN_USER=zzz; COOKIE_LOGIN_USER_SSO=www
```

## 凭证怎么拿

### 方案 A：`COOKIE_189`（推荐）

1. 浏览器打开 <https://cloud.189.cn> 并登录；
2. F12 → Network（网络）→ 刷新页面 → 点任意一个 `cloud.189.cn` 的请求 → Request Headers 里复制整条 **Cookie**；
3. 整条粘贴成 `COOKIE_189` 的值即可，不要只复制某一项；确认里面至少有 `COOKIE_LOGIN_USER`。

Cookie 会过期（通常是几周～几个月）。哪天 CI 报「会话失效，请重新获取 Cookie」，按上面步骤换一条就行。

### 方案 B：`SSON`

`open.e.189.cn` 扫码登录后，从浏览器 Cookie 里取 `SSON` 的值填进 `SSON`。
脚本会用它完成 SSO 握手再换 `sessionKey`，适合拿不到 `COOKIE_LOGIN_USER` 的情况。

### 方案 C：`USER` + `PWD`

保留只是为了兼容老配置。云端现在经常要求图形/短信验证码，CI 环境无法通过，脚本会直接告诉你「请改用 COOKIE_189」，不会假装成功。

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

参数：

| 参数 | 作用 |
| --- | --- |
| `--check` | 只登录、不签到，用来排查凭证 |
| `--no-notify` | 签到但不推送 |
| `--json` | 追加机器可读结果 |
| `--debug` | 打印请求细节（不会打印 Cookie 明文） |

环境变量：`COOKIE_189` / `SSON` / `USER` / `PWD` / `SERVERCHAN_SENDKEY` / `NOTIFY_WEBHOOK` / `DEBUG`。

通知都是可选的：`SERVERCHAN_SENDKEY` 走 Server 酱（v3 的 `sctapi` 与老 `SCU` key 都兼容），`NOTIFY_WEBHOOK` 会 POST 一份 JSON。

## 排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `没有配置任何账号` | 三个 secret 都没设 | 至少配 `COOKIE_189` |
| `登录失败：凭证已失效` / `InvalidSessionKey` | Cookie 过期 | 重新复制 `COOKIE_189` |
| `接口返回登录页（HTML）` | 同上，服务端直接跳登录页 | 同上 |
| `登录失败：请输入验证码…` | 口令登录被验证码拦截 | 改用 `COOKIE_189` |
| workflow 一直不触发 | 仓库 60 天无提交，GitHub 停用了 schedule | Actions 页点 `Enable`，或手动 `Run workflow`；`保活` 工作流会预防这个问题 |
| `Actions` 里没有「签到」 | fork 后默认没有启用 | 点 *I understand my workflows, go ahead and enable them* |

## 目录

```text
checkin.py                    # CLI：账号编排、签到/抽奖、汇总、通知
cloud189.py                   # 客户端：登录、sessionKey、签到、抽奖接口
tests/test_checkin.py         # 离线单测（mock 网络）
.github/workflows/run.yml         # 签到主流程
.github/workflows/keepalive.yml   # 每月空提交，防止 schedule 被停用
requirements.txt
```

## 免责声明

仅供个人账号自动化日常签到使用。脚本完全依赖天翼云盘非公开接口，接口变更后可能失效；
请自行评估账号风险，不要用于批量、商业或任何违反服务条款的场景。