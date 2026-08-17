# AutoRegister 本机控制台

Vue 3 + Element Plus 前端、FastAPI 中间层和 MongoDB 本机数据层：

```text
Vue → FastAPI → Service / Repository → MongoDB
```

- 项目：`<安装目录>\app`
- MongoDB：`mongodb://127.0.0.1:27017`，数据库 `autoregister`
- FastAPI：`http://127.0.0.1:8000`
- Vite：`http://127.0.0.1:5173`
- 执行设置：`<安装目录>\data\settings.json`
- 任务日志：`<安装目录>\data\logs\run-{runId}.jsonl`
- 浏览器探测产物：`<安装目录>\data\browser-probe\latest.json` 与 `latest.png`

当前正式版本为 `v1.0.0`。项目仅面向 Windows 本机运行，所有服务默认绑定到 `127.0.0.1`。

## 发布包快速开始

### 环境要求

- Windows 10/11 x64 与 PowerShell 5.1 或更高版本；
- Python 3.13，且 `python` 命令可用；
- Node.js `22.18+` 或 `24.12+`，且 `npm` 命令可用；
- 首次注册 MongoDB Windows 服务时需要管理员权限；
- 可访问 npm、PyPI 和 MongoDB 下载站点。

### 安装与启动

1. 解压 `AutoRegister-v1.0.0.zip`，保留包内目录结构。
2. 打开 PowerShell，进入解压后的 `app` 目录。
3. 复制环境变量模板并按需填写：

```powershell
Copy-Item .env.example .env
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

4. 分别启动 MongoDB、FastAPI 和 Vite：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-mongodb.ps1
& ..\register_env\Scripts\python.exe -m backend
# 新开一个 PowerShell 窗口，在 app 目录运行：
npm.cmd run dev
```

5. 浏览器打开 <http://127.0.0.1:5173/launch>。健康检查地址为
   <http://127.0.0.1:8000/api/health>。

停止 MongoDB：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-mongodb.ps1
```

### 发布包内容

```text
AutoRegister-v1.0.0/
├─ README.md              使用说明
├─ RELEASE_SHA256SUMS.txt 包内文件校验清单
└─ app/
   ├─ backend/            FastAPI 后端
   ├─ src/                Vue 前端源码
   ├─ dist/               已构建前端产物
   ├─ mongodb/            MongoDB 本机配置
   ├─ scripts/            服务与打包脚本
   ├─ tests/              后端测试
   ├─ .env.example        环境变量模板
   └─ setup.ps1           首次安装脚本
```

发布包不包含 `.env`、运行数据、数据库、日志、代理/账号信息、Python
虚拟环境、`node_modules` 或缓存。重新打包可在 `app` 目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-release.ps1
```

脚本会先执行前端类型检查、测试和构建，再生成 ZIP 与同名 `.sha256` 文件。

## 首次安装

在 PowerShell 中运行：

```powershell
cd <安装目录>\app
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

安装脚本会：

1. 创建或复用 `<安装目录>\register_env`；
2. 安装锁定的 Python 与前端依赖；
3. 校验 MongoDB 8.0.28 安装包的签名和 SHA-256；
4. 请求管理员权限并注册 `AutoRegisterMongoDB` Windows 服务；
5. 启动服务并验证 `127.0.0.1:27017`。

MongoDB 使用 `mongodb\mongod.yml`，服务数据位于 `<安装目录>\data\mongodb\db`。安装流程幂等，服务已经存在时只检查并启动。

## 调试启动

用 VS Code 打开 `<安装目录>\app`，在“运行和调试”中选择：

```text
AutoRegister: Full stack
```

该组合依次启动 MongoDB 服务、FastAPI、Vite 和本地浏览器。也可以分别运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-mongodb.ps1
& ..\register_env\Scripts\python.exe -m backend
npm.cmd run dev
```

常用地址：

- 控制台：<http://127.0.0.1:5173/launch>
- 健康检查：<http://127.0.0.1:8000/api/health>
- OpenAPI：<http://127.0.0.1:8000/api/docs>

## 数据与任务

- 账号、邮箱、代理和任务状态存入 MongoDB。
- 邮箱导入：`邮箱----接码地址`。
- 接码地址支持每个邮箱独立的 HTTPS 不透明令牌 URL，不要求 URL 路径包含邮箱。
- 代理导入：`host:port:username:password`。
- 账号导出：`邮箱----密码----TOTP` 或 `邮箱----接码地址`。
- 启动台默认运行真实 Roxy/Playwright 多进程探测；`/api/runs/mock` 仍保留用于回归测试。
- 最大并发任务在启动时形成快照；真实任务只要求至少存在 1 个 Roxy workspace，并固定复用 API 返回的第一个 workspace。完全没有 workspace 时不会预留邮箱或创建进程。
- 父任务持有共享 workspace 租约；每个 worker 仍只处理一个邮箱，并使用独立进程、临时 `dirId`、代理、Cookie/存储和产物目录。worker 退出并清理窗口后才复用并发槽位启动下一进程。
- 父调度器按 worker 完成事件的到达顺序监控 Roxy 基础设施：`roxy_api_failed`、`roxy_api_unavailable`、`roxy_workspace_not_ready`、`roxy_browser_not_ready`、`browser_cleanup_failed` 或 `cdp_connection_failed` 连续出现 5 次时立即熔断，`roxy_auth_failed` 则立即熔断。成功、部分成功或页面流程类失败会清零连续计数。熔断后不再启动新 worker，也不会自动重启 Roxy；在途 worker 会进入安全取消和清理，未处理邮箱释放回池，已成功账号保留，整批任务以 `failed / roxy_circuit_open` 结束。
- 前端真实任务与手动 `browser_probe` CLI 通过 MongoDB `browser_probe_controller` 租约互斥。
- 成功邮箱从邮箱池物理删除；失败、取消和中断任务释放未完成邮箱。
- FastAPI 重启后，遗留活动任务标记为 `interrupted`，不会自动续跑。
- 注册 worker 成功提取并保存 AT 后，会由同一 Playwright 页面跳转 `accounts/check/v4-2023-04-27` JSON 接口，按 JWT 内 `chatgpt_account_id` 选择账号并解析套餐、订阅与 Plus 试用资格，随后恢复 ChatGPT 主页。资格查询失败不会回滚已注册账号或 AT，只在账号记录中保存脱敏错误码。
- 账号池支持单账号和选中账号优惠资格查询，统一调用 `POST /api/accounts/check-promotion`。后端使用 `curl_cffi`，每个查询从代理池独占一个 HTTP 代理租约，请求完成或异常后必定释放；单次最多 100 个账号，服务端并发最多 3 个且受可用代理数限制。
- `free` 只代表免费套餐；只有接口同时返回 `eligible_promo_campaigns.plus` 时，`promotionEligible` 才会写为 `true`。列表不会返回 AT、代理凭据、响应正文或优惠接口原始 JSON。

## 日本注册到支付流水线

流水线页面位于 <http://127.0.0.1:5173/pipeline>，复用现有注册、资格查询、
Access Token 提链和协议支付实现，不改变各独立工具的入口与数据。

当前顺序固定为：

```text
注册并保存账号/AT → Plus 试用资格通过 → JP + PayPal 提链 → HeroSMS 日本 PP 接码 → 日本协议支付
```

- 后端只纳入 `promotionEligible=true`、已保存 AT 且 AT 尚未过期的账号；支付前会再次核验。
- 国家固定为 `JP`，提链支付方式固定为 `PayPal`，不会被支付工具页面的国家选择覆盖。
- “自动提链”默认关闭。保存日本 Checkout/Update 代理后可启用，后台一次只启动一个新提链任务。
- 协议支付使用独立的日本代理池。启用 HeroSMS 后，提链成功的记录会自动购买日本 PayPal 号码。
- HeroSMS 固定使用 PayPal 服务 `ts` 和日本国家 `182`；API Key 只从后端 `.env` 的 `HEROSMS_API_KEY` 读取，不返回浏览器。
- 可配置单号最高价格、最多换号次数和单号等待秒数。等待超时或验证码未通过时，旧激活会先取消，再换新号。
- 收到短信后会自动把验证码提交给协议支付任务；HeroSMS 关闭时仍可手工输入 `+81` 手机号和验证码。
- 页面分别保留待提链、提链中、待支付、待验证码、失败和支付成功状态，并实时显示账号密码、TOTP 与邮箱入口。
- 删除流水线记录不会删除账号池账号；下一次同步时，仍满足资格的账号会重新进入待提链队列。

首次使用：打开“日本流水线” → “流水线配置”，填写日本 Checkout Proxy、
Update Proxy 和协议支付代理，保存后点击“同步合资格账号”。可以手工勾选待提链账号，
也可以开启自动提链。启用 HeroSMS 和“提链成功后自动支付”后，流水线会继续完成购号、轮询短信和提交验证码。

## Access Token 提链控制台

提链页面位于 <http://127.0.0.1:5173/payment-tools>。当前实现来自
`backend/oai_payment_extractor/` 中记录的迁移源，已替换此前的简化提链实现；
它与注册、RoxyBrowser 和账号池任务相互独立。

使用顺序：

1. 在“Session / Access Token”中粘贴裸 Access Token、`Bearer` 文本或包含
   `accessToken` / `access_token` / `token` 的 Session JSON，然后点击“提炼 Access Token”。
   这一步只在本机解析输入，不请求网络，也不使用代理。
2. 在 Checkout Proxy 池中每行填写一条代理。启用“执行 Checkout Update”时，
   Update Proxy 池也必须填写；两个池分别轮询，且可分别轮换代理用户名中的
   `sid-*` 或末尾数字会话标记。
3. 选择国家和支付方式，按需填写 Stripe hCaptcha Token，然后提交当前 AT 或全部 AT。
4. 页面每 1.2 秒刷新任务阶段，可取消、换代理重试、重新解析 PayPal BA 链、
   删除任务、批量重试网络失败、导出成功 CSV，以及复制或打开最终 HTTPS 链接。

支持的 Checkout 分支为 OAICS 和 CS Checkout；默认自动识别。支付方式为 PayPal、
GoPay 和 GCash。国家/币种为：GB/GBP、US/USD、BR/USD、DE/EUR、TH/USD、BA/USD、
PH/PHP、ID/IDR、NL/EUR、AE/AED、DK/DKK、JP/JPY、ES/EUR、FI/EUR、FR/EUR。

代理阶段固定如下：

| 阶段 | 使用的代理 |
| --- | --- |
| 优惠资格检查、`checkout/update` | Update Proxy |
| Checkout 创建、OAICS/CS、Stripe、税费、支付确认、最终跳转解析 | Checkout Proxy |

代理输入支持 HTTP、HTTPS、SOCKS5/SOCKS5H URL、常见四字段文本，以及
IPRocket、IPRoyal、1024Proxy 的参考项目格式。可在页面粘贴
`https://app.iprocket.io/...` 订阅地址并导入到两个代理池。普通 HTTP/SOCKS 代理无需
额外配置；上述三类特殊网关会自动使用本机桥 `127.0.0.1:18796`。桥会优先尝试可选的
SOCKS5 前置代理（默认 `127.0.0.1:3251`）；该端口未运行时会按参考实现直接连接上游
代理，所以不是必填配置。如需固定使用前置代理，启动 FastAPI 前可覆盖：

```powershell
$env:IPROCKET_PRE_PROXY_HOST = '127.0.0.1'
$env:IPROCKET_PRE_PROXY_PORT = '3251'
$env:IPROCKET_BRIDGE_PORT = '18796'
$env:IPROCKET_CHAIN_PROXY = 'http://127.0.0.1:18796'
& ..\register_env\Scripts\python.exe -m backend
```

参考项目在迁移后新增的完整集成指南已原样保存在
`backend/oai_payment_extractor/SOURCE_README.md`。本项目继续使用 FastAPI 的
`127.0.0.1:8000`，同时提供两套等价接口：

- Vue 使用的 camelCase 接口：`/api/payment-extractor/*`；
- 参考指南的 snake_case 兼容接口：`/api/defaults`、`/api/tasks*`、
  `/api/proxy/test`、GET `/api/proxy/source` 和 `/ws/tasks`。

`/ws/tasks` 要求第一条消息为 `{"type":"auth","password":"PASSWORD"}`，并推送
任务历史、阶段和终态事件；最终状态仍以 GET 任务接口为准。设置
`OPLL_WEB_PASSWORD` 后，只有上述提链接口需要 `X-Workbench-Password`，注册、账号、
代理池和运行任务 API 不受影响。Vue 页面中的“工作台密码”会使用参考项目相同的
localStorage 键为提链请求附加该请求头。

可以复制示例配置后重启 FastAPI：

```powershell
Copy-Item .\.env.example .\.env
```

已接入 `OPLL_AT`、国家/强制国家、支付方式、双代理、代理池文件、订阅地址、
Checkout Update、worker 数、TTL、事件历史、密码和提链日志配置。集成版的监听地址仍由
AutoRegister 启动命令控制，因此示例中的 `OPLL_WEB_HOST/PORT` 只作为源项目兼容说明。

提链任务保存在 FastAPI 进程内存中，默认 4 个 worker、终态保留 3600 秒；重启后任务
列表会清空。Access Token 与 hCaptcha Token 不写入浏览器存储；代理池、订阅地址和可选
工作台密码保存在本机浏览器 `localStorage`。API 和 WebSocket 会隐藏 Access Token、
代理密码、hCaptcha Token 与授权 URL 事件字段。

## 协议授权工作台

侧栏的“协议授权”页面集成了独立的 Agreement Protocol sidecar，源码固定到：

```text
https://github.com/1537271403/paypal-agreement-protocol
commit 4719066ec6fd56b57a5bd9599758366836c9dc0a
```

使用流程：

1. 打开 `/agreement-tools`，后端会按需启动本机 sidecar；
2. 输入 BA 链接或 Token、手机号、国家和任务代理；
3. 按页面提示提交短信验证码或操作临时浏览器；
4. 在任务结果区查看授权状态和脱敏日志。

sidecar 默认监听 `127.0.0.1:18098`，通过 `/paypal-pay/*` 同源代理嵌入当前页面。它使用独立进程、任务队列、Cookie、指标和日志目录，不覆盖现有 `/api/tasks`、`/api/payment-extractor/*`、注册运行或资源池接口。常用配置见 `.env.example` 中的 `PAP_*` 和 `PAYPAL_*`。

Windows 集成版会使用 Playwright 自带 Chromium 进行临时验证；也可以通过 `PAP_BROWSER_EXECUTABLE` 指定浏览器。完整协议日志默认关闭，启用 `PAP_FULL_LOGS=1` 后写入 `data/paypal-agreement/`。

## RoxyBrowser 单线程探测

设置页中的浏览器字段用于 RoxyBrowser：

- 指纹浏览器地址默认 `D:\RoxyBrowser\RoxyBrowser.exe`；
- API 主机固定为 `127.0.0.1`，端口默认 `50000`；
- API Key 明文保存在本机 `settings.json`，并按个人本地使用设置在设置页和设置 GET 接口中明文回显；仍禁止进入任务日志、异常和探测产物；
- “启用无头模式”会直接传给 Roxy `/browser/open`；
- 代理额外重试范围为 0–5，默认 1。

先在 Roxy 客户端中开启 API并在本地设置页填写重置后的 API Key。首次可视探测：

```powershell
cd <安装目录>\app
& ..\register_env\Scripts\python.exe -m backend.browser_probe --hold-seconds 30
```

存在多个 Roxy workspace 时显式指定：

```powershell
& ..\register_env\Scripts\python.exe -m backend.browser_probe --workspace-id 1
```

Roxy 自动启动后，探测器会等待本地 `/health` 与同一 workspace 连续成功 3 次；检查间隔 1 秒、最长 30 秒、单次请求最多 3 秒，任一次失败都会重新累计稳定次数。HTTP 401/403 会立即返回 `roxy_auth_failed`，等待超时返回 `roxy_workspace_not_ready`。等待完成前不会预留邮箱、领取代理或创建窗口。若 `/browser/open` 因 Roxy 启动竞态返回可重试错误，探测器不会再次调用打开接口，而是每 500 毫秒通过 `/browser/connection_info` 对账同一 `dirId`，最长 15 秒；已经异步启动的窗口会被直接接管，结果记录 `roxyOpenRecovered` 与恢复耗时。始终未生成 CDP 连接时返回 `roxy_browser_not_ready`，并观察延迟窗口 5 秒，发现后再次关闭、删除。启动时还会清除已过期的 `probe:` 代理租约字段。失败诊断只记录安全的阶段、错误类型、次数、耗时和数字状态码，不记录响应正文、CDP 地址或凭据。

每次真实任务预检稳定后，会通过 Roxy `/browser/list` 清理名称以 `AutoRegister Probe ` 开头且备注精确等于 `AutoRegister single-thread probe` 的本项目遗留窗口档案，避免已保存档案占满 workspace 窗口额度。其他项目和人工创建的窗口不会匹配，也不会被清理。

探测器通过 MongoDB 控制器租约限制真实任务冲突，并按最久未使用顺序租用代理。Roxy 临时窗口默认只打开代理出口 IP 页面；Playwright 连接 CDP 后优先复用该标签并关闭同一临时上下文中的多余标签，再导航到 ChatGPT 登录/注册页。填写邮箱和点击 Continue 前共用统一稳定门：每个阶段最多等待 60 秒，并要求页面连续稳定 5 秒；稳定条件包括 URL 始终为 `chatgpt.com/auth/login`、`document.readyState` 为 `complete`、不存在 Cloudflare 文本、Turnstile iframe 或结构化挑战、邮箱输入框可见且可编辑/启用、Continue 可见且启用并且文本正确。原有随机 3–5 秒等待已合并进稳定门，实际连续稳定时间取随机值与 5 秒中的较大值，不再额外重复 sleep。任何 CF 重现、DOM 脱离、按钮禁用或字段值变化都会立即清零连续稳定计时；CF 只被动等待自动放行，全程不点击也不绕过，持续到 60 秒时截图并返回 `target_challenge_detected`（`challengeWaitMs: 60000`）。未观察到持续 CF 但首次表单始终不稳定时返回 `email_form_not_stable`，填写后始终不稳定则返回 `email_form_unstable_before_continue`。稳定后会重新定位输入框与按钮、再次确认输入值等于目标邮箱，随后才执行正式点击和密码页、验证码页及可信跳转的并发观察；即使 DOM 替换导致点击调用抛错，只要下一步已经出现仍视为提交成功。若页面水合或刷新持续清空邮箱，会重新进入稳定门并自动重填一次；提交后只有邮箱框连续空白至少 2 秒且登录表单已经恢复才判定刷新，短暂空白或加载状态不会触发重填。总填写和 Continue 点击均最多两次；点击前反复重置返回 `email_form_reset`，提交后再次重置或重填额度耗尽返回 `email_post_submit_reset`。脱敏结果额外记录 `loginChallengeObserved`、`emailFormReadyWaitMs`、`emailPreContinueStableWaitsMs` 和 `emailFormStabilityResetCount`，不会记录邮箱、Cookie 或凭据。`taskTimeoutSeconds` 默认为 `0`，表示整批任务不限时；设置为正整数时才启用对应秒数的全局硬上限。人工取消、服务关闭和各页面阶段自身的 60/90/120 秒保护仍然有效。只有确认进入邮箱验证页后，才会开始请求接码地址，每 2 秒轮询一次，最多等待 120 秒。验证码邮件必须带有明确时区，转换为 UTC 后不得早于最终 Continue 提交边界，也不得超前当前 UTC 超过 120 秒。接码地址继续执行 HTTPS、公网 DNS、重定向和响应大小校验；本机 DNS 使用 `198.18.0.0/15` Fake-IP 时，必须再通过公共 DNS-over-HTTPS 复核。

当前实现会在提交邮箱前读取一次接码页作为旧码基线。邮件带明确时区时继续按提交边界过滤；供应商不返回邮件时间时，只接受与基线不同且连续稳定的新验证码。邮箱页和验证码页优先按同一表单中的 `type="submit"` 结构识别提交按钮，不依赖 `Continue`、`続行` 等界面语言。

日本区域流程额外识别日文邮箱验证标题与正文（包括“一時的な認証コード”和“一時検証コード”），并继续使用旧码基线避免误用历史验证码。登录、验证码和资料页优先依赖表单结构与稳定属性；资料页兼容 `/about-you`、`/create-account/profile`、`/u/signup/profile`、`/signup/profile` 以及验证码与资料合并页。资料表单只有一个可见提交控件时允许结构化提交，日文 `続行` 与账号创建文案也会被识别。

邮箱提交后若 45 秒内始终停留在禁用加载状态，恢复流程会重新导航登录页以取消挂起请求，再用同一邮箱提交一次。资料提交阶段参考注册状态机：识别认证重试页并最多恢复两次；仍停留在资料表单时，最多提交三次，每次间隔至少 3.5 秒。

收到新鲜的独立 6 位验证码后，探测器会在同一页面填入并立即校验，随机等待 1–3 秒后再次校验并只点击一次验证码页 Continue。点击和下一页观察并发执行；即使标准点击抛错，只要页面已经进入下一步仍视为成功。验证码值被清空、被拒绝或过期、人机挑战和未知下一页都会立即停止；点击完成后表单、按钮与 URL 持续 45 秒不变时返回 `verification_form_unchanged_after_click`。全过程不会点击 Resend、重新填写验证码或进行第二次提交，并以 `verificationClickCompleted`、`verificationPostClickState`、等待耗时及最终可见状态记录脱敏诊断。验证码提交后最多等待 45 秒解析 Profile 分支：进入 `/about-you` 时，从本地英文姓名列表随机组合一个仅含字母和单个空格的全名；表单同时兼容数字 `Age` 与日期 `Birthday`/`Date of birth` 两种变体。数字年龄沿用 25–35 岁随机整数；生日变体先随机选择 25–35 岁，再按执行时 UTC 日期生成能准确满足该年龄的有效 `YYYY-MM-DD` 日期。姓名与第二字段之间、第二字段与提交之间各独立等待 1–3 秒并重新定位、复核值；提交按钮只在同一可见表单内匹配 `Finish creating account` 或 `Continue`，且最多点击一次。若点击调用抛错但可信 ChatGPT 主界面已出现，仍按成功处理。若直接进入可信的 `chatgpt.com` 主界面且可见 `accounts-profile-button`，无论入口是 `button` 还是实际页面使用的 `div[role="button"]`，都判定资料已经设置；共享定位器按 `data-testid` 和 `role` 识别入口，排除位于 `inert` 或 `aria-hidden` 容器的同名副本，不依赖用户名、套餐或中英文 `aria-label`。跳过分支返回 `profileSkipped: true` 和 `profileSkipReason: already_configured`。成功与 Profile 失败结果仅追加枚举诊断 `profileFormVariant`、`profileLocatorStrategy` 和 `profileSubmitVariant`；随机姓名、年龄和生日只存在于浏览器内存，不进入 MongoDB、终端、`latest.json` 或 JSONL。挑战页、未知页面超时、表单重置或明确拒绝仍立即停止，不重试、不轮换代理。

账号入库并删除对应预留邮箱后，主页之后的 Security / Passkey 导航当前通过内部常量 `SECURITY_NAVIGATION_ENABLED = False` 暂停。默认流程停留在可信的 ChatGPT 主界面，不导航 `#settings/Security/passkeys`、不点击 Add，也不会触发 Windows 安全中心；结果返回 `success / account_profile_completed`，保留 `accountId`、Profile 字段和 `accountSetupPending: true`，不输出 `security*` 字段。`--hold-seconds` 从账号资料完成、账号入库和邮箱删除后开始，结束后仍关闭临时 Roxy 窗口并释放代理与探测锁。既有 Security / Passkey 自动化实现及其单元测试继续保留，后续重新实现时可恢复内部开关；密码和 TOTP 仍待后续设置。

默认模式只在终端和 `latest.json` 记录是否收到验证码、长度、邮件 UTC 时间及等待耗时，不记录验证码正文：

```powershell
& ..\register_env\Scripts\python.exe -m backend.browser_probe --hold-seconds 60
```

需要本机人工调试时，可显式显示本次验证码：

```powershell
& ..\register_env\Scripts\python.exe -m backend.browser_probe --hold-seconds 60 --debug-show-code
```

`--debug-show-code` 只允许验证码出现在当前终端和 `<安装目录>\data\browser-probe\latest.json`；邮箱地址、接码 URL、令牌、Roxy API Key、代理凭据、Cookie 和 CDP 地址仍保持脱敏。探测器自动启动 Roxy 时会丢弃 Roxy 自身的标准输出和错误输出，避免第三方调试请求头进入终端。每次新探测开始时会清除上一轮 `latest.json`，探测结束后自动关闭并删除临时窗口，并释放邮箱、代理租约和控制器锁。

真实任务接口为 `POST /api/runs/browser-probe`，worker 快照接口为 `GET /api/runs/{run_id}/workers`。完整邮箱和 ipify 实测出口 IP 仅在本机 worker 快照响应和启动台显示；JSONL 只记录脱敏 IP，且不会记录 AccessToken、密码、TOTP、代理认证、Cookie 或 CDP 地址。

每个多进程 worker 都在自己的 `data/browser-probe/runs/{runId}/{workerId}/` 目录保存诊断截图和脱敏 `latest.json`。步骤异常也会写入安全错误码、阶段、资源 ID 和有限状态诊断；写入失败不会覆盖原始 worker 错误。失败产物不包含邮箱地址、验证码、AccessToken、完整出口 IP、Cookie、CDP 地址或代理凭据。

Continue 点击阶段会先在邮箱表单内重新定位按钮并复核邮箱值、输入框可编辑状态和按钮启用状态。若点击期间发生 DOM 刷新，探测器会对账下一步页面或稳定表单状态，最多使用第二次提交；`latest.json` 可结合 `emailContinueClickFailures`、`emailContinueRecoveryState`、`emailPreContinueStableWaitsMs` 和 `emailFormStabilityResetCount` 判断是否发生了等待或恢复。

## JSONL 日志

- 每次任务一个 UTF-8 文件：`run-{UUID}.jsonl`。
- 每行一个 `schemaVersion: 1` 事件。
- 日志禁止写入密码、TOTP、接码地址、代理密码、Cookie、Token 或 Secret。
- 活动任务日志始终保留；终态日志只保留最近 10 次。
- 清理只处理能通过文件名与 schema 校验的终态日志；未知文件和损坏文件不会被删除。

## 验证

```powershell
npm.cmd run type-check
npm.cmd test -- --run
npm.cmd run build
& ..\register_env\Scripts\python.exe -m pytest -q tests\backend
```

运行真实 MongoDB 集成测试：

```powershell
$env:AUTOREGISTER_RUN_MONGO_TESTS = '1'
& ..\register_env\Scripts\python.exe -m pytest -q tests\backend
```

测试数据库名固定使用 `autoregister_test_` 前缀，并在测试结束时只删除本次随机测试库。
