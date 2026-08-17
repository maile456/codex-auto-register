# PayPal Agreement Protocol

<p align="center">
  <strong>从 BA 链接开始，完成 PayPal 协议授权。</strong><br>
  独立任务、代理池、短信验证、临时浏览器验证与响应式管理界面。
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Playwright" src="https://img.shields.io/badge/Browser-Playwright-2EAD33?logo=playwright&logoColor=white">
  <img alt="UI" src="https://img.shields.io/badge/UI-Responsive-7C3AED">
  <img alt="Repository" src="https://img.shields.io/badge/Repository-Private-111827">
</p>

## 页面预览

### 桌面端

![桌面端页面](docs/screenshots/desktop.png)

### 手机端

<p align="center">
  <img src="docs/screenshots/mobile.png" alt="手机端页面" width="390">
</p>

## 功能概览

- 输入 PayPal BA 链接或 `BA Token` 创建独立协议任务。
- 每个任务可选择 `身份提升流程` 或 `原版流程`；默认使用身份提升流程。两者均为纯协议，原版保持现有行为，身份提升版强制校验 EC Checkout、Signup Context，并在最终授权前重建 Guest → Member 上下文。
- 支持 BR、GB、US、JP、TH、ID、PH、TW、MX 国家资料与地址结构。
- 支持 HTTP、HTTPS、SOCKS5 代理池；带认证的 SOCKS5 可通过任务级代理桥交给 Chromium 使用。
- 自动执行协议页面初始化、风险信号、账号流程和最终授权阶段。
- 短信验证码错误后可继续提交，也可更换手机号重新发送。
- 出现浏览器验证时启动临时 Chromium，并将任务 Cookie 同步回协议会话。
- 自动维护 EUAT Cookie、Buyer Context 与 Hermes `billingLite` 会话。
- 所有国家优先使用在线地图规范地址，失败或并发繁忙时回退本地地址池。
- 提供任务队列、并发限制、停止任务、实时日志及成功时间观测图。
- 支持深色/浅色模式，以及桌面端和手机端响应式布局。

## 项目结构

```text
paypal-agreement-protocol/
├─ web.py                     # Web 服务、任务队列、接口与状态管理
├─ main.py                    # 命令行入口
├─ config.py                  # UA、视口与基础配置
├─ paypal/
│  ├─ flow.py                 # 协议主流程
│  ├─ elevation_flow.py       # 可选的 Buyer 身份提升纯协议流程
│  ├─ graphql.py              # GraphQL 查询与 Mutation
│  ├─ session.py              # HTTP 会话、Cookie 与请求头同步
│  ├─ manual_browser.py       # 临时 Chromium 与远程交互
│  ├─ models.py               # 用户、地址、卡片及在线地图解析
│  ├─ proxy.py                # 代理格式解析与代理池
│  ├─ fingerprint.py          # 设备与浏览器信号
│  ├─ analytics.py            # 页面事件与分析信号
│  └─ tealeaf.py              # Tealeaf 会话数据
├─ web_static/                # 前端页面、样式与脚本
├─ tools/                     # 调试与采集工具
└─ docs/screenshots/          # README 页面截图
```

## 运行环境

推荐使用 Linux：

- Python 3.11+
- Chromium
- Xvfb
- Nginx（生产环境反向代理时使用）

Debian/Ubuntu：

```bash
sudo apt update
sudo apt install -y chromium xvfb python3-venv
```

## 快速启动

```bash
git clone https://github.com/1537271403/paypal-agreement-protocol.git
cd paypal-agreement-protocol

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python web.py --host 0.0.0.0 --port 8080
```

打开：

```text
http://SERVER_IP:8080
```

本机调试：

```bash
python web.py --host 127.0.0.1 --port 8080
```

## 代理格式

代理池每行一条，支持：

```text
host:port:username:password
http://username:password@host:port
https://username:password@host:port
socks5://username:password@host:port
socks5h://username:password@host:port
```

代理账号不要写进源码或提交到仓库，通过网页任务参数或环境变量注入。

## 常用环境变量

| 变量 | 作用 | 示例 |
|---|---|---|
| `PAYPAL_WEB_MAX_ACTIVE_JOBS` | 全局并发任务数 | `5` |
| `PAYPAL_WEB_MAX_QUEUED_JOBS` | 最大排队任务数 | `50` |
| `PAYPAL_WEB_MAX_ACTIVE_JOBS_PER_DEVICE` | 单设备并发数 | `2` |
| `PAYPAL_WEB_MAX_TOTAL_JOBS` | 内存保留任务上限 | `200` |
| `PAYPAL_WEB_OTP_TIMEOUT_SECONDS` | 短信验证码等待时间 | `1800` |
| `PAYPAL_MANUAL_BROWSER_LIMIT` | 临时浏览器并发数 | `2` |
| `PAYPAL_WEB_COOKIE_SECURE` | 为会话 Cookie 添加 Secure | `1` |
| `PAYPAL_WEB_PRODUCTION` | 开启生产模式 | `1` |
| `PAYPAL_WEB_ALLOW_DEBUG_LOGS` | 网页显示 DEBUG 日志 | `0` |
| `PAYPAL_HTTP_ENGINE` | HTTP 引擎 | `curl_cffi` |
| `PAYPAL_PROXY_URL` | 单条默认代理 | `http://user:pass@host:port` |
| `PAYPAL_PROXY_POOL` | 默认代理池 | 多条代理，逗号分隔 |

## 生产部署

仓库中的 `start.sh` 可用于基础启动。生产环境建议使用 systemd 管理进程，再由 Nginx 反向代理到本地端口：

```ini
[Service]
WorkingDirectory=/opt/paypal-pay
Environment=PAYPAL_WEB_PRODUCTION=1
Environment=PAYPAL_WEB_COOKIE_SECURE=1
ExecStart=/opt/paypal-pay/.venv/bin/python /opt/paypal-pay/web.py --host 127.0.0.1 --port 18097
Restart=always
```

## 数据与安全

以下内容已经由 `.gitignore` 排除：

- `.env`
- Python 缓存
- 浏览器采集数据
- ZIP 和临时归档

任务日志与接口输出会对密码、卡片、验证码、访问令牌和 BA Token 等字段进行脱敏。生产环境仍应限制服务器、GitHub 私有仓库和环境变量的访问权限。

## 更新流程

```bash
git pull
python -m pip install -r requirements.txt
sudo systemctl restart paypal-pay
```

更新后检查：

```bash
systemctl status paypal-pay
curl http://127.0.0.1:18097/api/health
```
