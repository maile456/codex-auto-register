# Codex Auto Register

面向 Windows 本机运行的账号工作流控制台。项目使用 Vue 3、FastAPI 和 MongoDB，
将邮箱、代理、指纹浏览器任务、账号状态、支付工具和成品管理放在一个可视化界面中。

> 本项目默认只监听 `127.0.0.1`。运行数据、邮箱凭据、代理凭据、Access Token、
> TOTP Secret 和第三方 API Key 均不应提交到 Git。

## 功能

- 邮箱池、代理池与账号池管理
- 代理按国家和分组管理，可供不同工作流选择
- RoxyBrowser 临时指纹窗口调度、并发控制、清理和故障熔断
- 多种邮箱验证码接口与 MailCom Hub 对接
- 注册后套餐和 Plus 试用资格查询
- Access Token 解析、支付链接任务与分阶段代理
- 可配置的提炼、支付重试和自动流水线
- HeroSMS 国家配置、价格限制、换号次数和等待时间
- 成品管理、导出标记、邮箱到账确认和统计
- JSONL 任务日志、敏感字段脱敏和运行恢复

## 界面

| 页面 | 用途 |
| --- | --- |
| 启动界面 | 选择邮箱来源、注册国家、代理分组、数量和并发 |
| 账号池 | 查询、筛选、套餐检查、复制和导出账号 |
| 邮箱池 | 导入接码地址、同步 MailCom 别名、管理来源 |
| 配置栏 | RoxyBrowser、任务并发和代理分组 |
| 支付工具 | Access Token 解析与支付链接任务 |
| Plus 流水线 | 资格筛选、提炼、接码和支付编排 |
| 成品管理 | 支付结果、到账状态、邮箱入口和导出状态 |
| HeroSMS | 独立管理短信国家与订单参数 |
| 协议授权 | 隔离运行的协议授权工作台 |

## 架构

```mermaid
flowchart LR
    UI["Vue 3 / Vite"] --> API["FastAPI"]
    API --> DB["MongoDB"]
    API --> ROXY["RoxyBrowser Local API"]
    API --> MAIL["Mailbox providers"]
    API --> PAY["Payment workers"]
    API --> SMS["SMS providers"]
    HUB["MailCom Hub"] --> IMAP["mail.com IMAP"]
    API --> HUB
```

所有服务默认绑定回环地址：

- 前端：<http://127.0.0.1:5173>
- 后端：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/api/docs>
- MailCom Hub：<http://127.0.0.1:3211>
- RoxyBrowser OpenAPI：`127.0.0.1:50000`

## 环境要求

- Windows 10/11 x64
- PowerShell 5.1 或 PowerShell 7
- Python 3.13
- Node.js `22.18+` 或 `24.12+`
- MongoDB 8.0
- RoxyBrowser（仅真实浏览器任务需要）
- Chrome/Chromium（MailCom 别名自动创建需要）

## 快速开始

```powershell
git clone git@github.com:maile456/codex-auto-register.git
cd codex-auto-register\app

Copy-Item .env.example .env
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

分别启动后端和前端：

```powershell
# 终端 1
cd app
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-mongodb.ps1
& ..\register_env\Scripts\python.exe -m backend

# 终端 2
cd app
npm.cmd run dev
```

打开 <http://127.0.0.1:5173/launch>。也可以在仓库根目录双击
`start-autoregister.cmd` 启动本机组件。

### 首次配置 RoxyBrowser

1. 启动 RoxyBrowser并登录。
2. 在 RoxyBrowser 的 API 设置中启用本地 OpenAPI。
3. 在本项目“配置栏”填写 RoxyBrowser 路径、端口和 API Key。
4. 至少创建一个 workspace；任务会在所选 workspace 中创建临时窗口。
5. 先使用一个邮箱、并发 `1` 做连通性验证。

RoxyBrowser 的套餐、API 请求频率和窗口创建配额由 RoxyBrowser 服务端控制。
`browser_create` 返回 `416` 时，应先在其客户端检查当前套餐、可用窗口及当天创建额度。

## 数据导入

邮箱格式：

```text
user@example.com----https://mail.example.test/latest?email=user%40example.com
```

代理支持 URL 和常见四字段形式：

```text
socks5://username:password@proxy.example.test:1080
proxy.example.test:1080:username:password
```

敏感数据保存在本机 MongoDB 或 `data/` 下的本地文件中。API 响应和任务日志会尽量
隐藏 Access Token、代理密码、短信 Key、TOTP Secret 和浏览器连接地址。

## MailCom Hub

`mailcom-manager/` 是独立的本机邮箱管理服务，提供：

- `邮箱----密码` 批量导入
- Windows DPAPI 加密存储
- INBOX、Spam、Junk 读取和验证码提取
- 主邮箱与别名独立接码 URL
- 别名创建和批量补足
- 可选的服务器快照同步

启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\mailcom-manager\start.ps1
```

接口文档见 <http://127.0.0.1:3211/docs>，详细说明见
[`mailcom-manager/README.md`](mailcom-manager/README.md)。

## 环境变量

复制 `app/.env.example` 后按需配置。常用变量：

| 变量 | 说明 |
| --- | --- |
| `HEROSMS_API_KEY` | HeroSMS API Key |
| `OPLL_WEB_PASSWORD` | 支付工作台请求密码 |
| `OPLL_CHECKOUT_PROXY` | 默认 Checkout 代理 |
| `OPLL_UPDATE_PROXY` | 默认资格查询/Update 代理 |
| `IPROCKET_BRIDGE_PORT` | 特殊代理本机桥端口 |
| `PAP_PORT` | 协议授权 sidecar 端口 |
| `PAYPAL_PROXY_POOL` | 协议授权代理池 |
| `AUTOREGISTER_MONGO_URI` | 覆盖 MongoDB URI |

不要把 `.env`、`data/settings.json` 或浏览器导出的凭据文件提交到仓库。

## 开发

安装依赖：

```powershell
cd app
& ..\register_env\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
npm.cmd ci
```

验证：

```powershell
cd app
npm.cmd run type-check
npm.cmd test -- --run
npm.cmd run build
& ..\register_env\Scripts\python.exe -m pytest tests\backend -q

cd ..\mailcom-manager
& ..\register_env\Scripts\python.exe -m pytest tests -q
```

MongoDB 集成测试默认跳过；需要时设置 `AUTOREGISTER_RUN_MONGO_TESTS=1`。

## 项目结构

```text
.
├─ app/
│  ├─ backend/              FastAPI、任务调度和服务集成
│  ├─ src/                  Vue 前端
│  ├─ tests/backend/        后端测试
│  ├─ scripts/              MongoDB 与发布脚本
│  └─ .env.example          环境变量模板
├─ mailcom-manager/         MailCom Hub
├─ start-autoregister.ps1   Windows 一键启动
└─ README.md
```

运行数据、虚拟环境、依赖目录、备用项目、构建产物和本机日志均由根目录
`.gitignore` 排除。

## 常见问题

### 端口 8000 已占用

说明已有后端实例正在运行。打开 <http://127.0.0.1:8000/api/health> 检查状态，
不要重复启动第二个实例。

### RoxyBrowser 创建窗口失败

先确认 RoxyBrowser 正在运行、OpenAPI 已开启、API Key 与端口正确、workspace 存在。
错误码 `416` 通常表示服务端窗口创建额度问题，不是代理连通性错误。

### 代理 CONNECT 中止

先直接测试本地网络能否连接代理入口，再确认代理协议。部分动态 SOCKS5 网关会通过
本机桥转换后交给 RoxyBrowser；桥端口和代理本身是两层独立状态。

### 邮箱 URL 很慢

IMAP 查询可能依次检查多个文件夹并下载邮件正文。批量别名任务、注册取码和到账检查
同时运行时会争用 IMAP 并发槽位，建议错峰执行。

## 安全

公开部署前阅读 [`SECURITY.md`](SECURITY.md)。管理 API 默认面向本机使用；若通过反向
代理暴露，必须增加身份验证、TLS、访问控制和请求速率限制。

## 贡献

提交方式、测试要求和敏感信息检查见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 许可证与第三方代码

项目原创代码采用 [MIT License](LICENSE)。内置或迁移的第三方代码不自动适用 MIT，
其来源、固定版本和许可边界见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
