# Payment Link Extractor 集成指南

这是一个可独立部署、也可作为 Python 包调用的支付授权链接提取组件。它接收账号 Access Token、Checkout/Update 代理和账单国家，创建并推进结账流程，最终返回第三方支付授权链接。

当前默认配置：

- 账单国家：`DE`
- 币种：`EUR`
- 支付方式：`paypal`
- Web 服务：`http://127.0.0.1:18794`
- 代理桥：`http://127.0.0.1:18796`
- 支持 `oaics_*` 与 `cs_*` 两种 Checkout
- PayPal 只有解析到严格的 BA 授权链接才判定成功

> Access Token、代理账号和代理密码都是敏感凭据。示例中的 `TOKEN`、`PROXY`、`PASSWORD` 均为占位符。

## 1. 推荐集成方式

推荐让本项目作为独立本地服务运行，另一个项目通过 HTTP API 调用：

```text
你的项目
   │ HTTP / WebSocket
   ▼
Payment Link Web Service :18794
   │
   ├── Checkout / Stripe / Provider
   │
   └── Proxy Bridge :18796 ── SOCKS5/HTTP 上游代理
```

这种方式具有以下优点：

- Token 与代理只进入本地服务进程；
- 任务并发、重试、取消和事件推送由本项目统一管理；
- 调用方不需要依赖内部 Checkout/Stripe 实现；
- 上游协议变化时只升级本项目。

如果调用方本身就是 Python 服务，也可以使用第 8 节的进程内 API。

## 2. 安装与启动

### Windows

首次运行：

```text
INSTALL_AND_START.bat
```

以后启动和停止：

```text
START.bat
STOP.bat
```

### 命令行

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

启动代理桥和 Web 服务：

```powershell
.venv\Scripts\python.exe -u iprocket_chain_bridge.py
.venv\Scripts\python.exe -m payment_link_extractor.web --env-file .env
```

两个命令应运行在独立进程中。启动后检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18794/api/health
```

预期响应：

```json
{"ok": true, "service": "payment-link-extractor"}
```

## 3. 环境配置

复制 `.env.example` 为 `.env`。常用字段如下：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPLL_WEB_HOST` | `127.0.0.1` | Web 监听地址 |
| `OPLL_WEB_PORT` | `18794` | Web 监听端口 |
| `OPLL_WEB_PASSWORD` | 空 | API/WebSocket 工作台密码 |
| `OPLL_AT` | 空 | 可选默认 Access Token；集成时推荐按请求传入 |
| `OPLL_CHECKOUT_PROXY` | 空 | 可选默认 Checkout 代理 |
| `OPLL_UPDATE_PROXY` | 空 | 可选默认优惠检查/更新代理 |
| `OPLL_COUNTRY` | `DE` | 默认账单国家 |
| `OPLL_FORCE_COUNTRY` | `DE` | 非空时覆盖请求中的国家 |
| `OPLL_PAYMENT_METHOD` | `paypal` | 默认支付方式 |
| `OPLL_UPDATE_CHECKOUT` | `true` | 是否执行优惠资格检查和 Checkout 更新 |
| `OPLL_STICKY_TASK_PROXY` | `false` | 同一任务让 Checkout Update 与 Stripe 使用同一代理会话 |
| `OPLL_TASK_WORKERS` | `4` | 后台任务线程数 |
| `OPLL_TASK_TTL_SECONDS` | `3600` | 终态任务保留时间 |
| `OPLL_TASK_EVENT_HISTORY_SIZE` | `500` | WebSocket 历史事件数 |
| `IPROCKET_CHAIN_PROXY` | `http://127.0.0.1:18796` | 本地代理桥地址 |
| `IPROCKET_BRIDGE_PORT` | `18796` | 本地代理桥监听端口 |
| `OPLL_PROXY_SOURCE_URL` | 空 | 可选订阅地址；粘贴式代理池可保持为空 |
| `OPLL_PROXY_POOL_FILE` | 空 | 可选服务端代理池文本文件 |
| `OPLL_LOG_FILE` | `./data/payment-link.log` | 日志文件 |

德国账单配巴西出口示例：

```dotenv
OPLL_COUNTRY=DE
OPLL_FORCE_COUNTRY=DE
OPLL_PAYMENT_METHOD=paypal
OPLL_STICKY_TASK_PROXY=true
```

代理用户名中的 `region-BR` 不会覆盖 `OPLL_FORCE_COUNTRY=DE`。

## 4. HTTP API

所有 `/api/*` 请求在配置了 `OPLL_WEB_PASSWORD` 时都需要请求头：

```http
X-Workbench-Password: PASSWORD
```

密码为空时服务端接受本地请求。跨机器部署时应配置密码和 TLS，并限制防火墙来源。

### 4.1 创建任务

```http
POST /api/tasks
Content-Type: application/json
X-Workbench-Password: PASSWORD
```

请求体：

```json
{
  "access_token": "TOKEN",
  "checkout_proxy": "socks5://USER:PASS@HOST:PORT",
  "update_proxy": "socks5://USER:PASS@HOST:PORT",
  "country": "DE",
  "payment_method": "paypal",
  "apply_checkout_update": true
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `access_token` | 是 | Access Token；也接受 `accessToken` 或 `token` |
| `checkout_proxy` | 是 | Checkout、Stripe 和支付渠道代理 |
| `update_proxy` | 条件必填 | `apply_checkout_update=true` 时必填 |
| `country` | 否 | 账单国家；可能被 `OPLL_FORCE_COUNTRY` 覆盖 |
| `payment_method` | 否 | `paypal`、`gopay` 或 `gcash` |
| `apply_checkout_update` | 否 | 是否检查并应用优惠更新 |
| `stripe_hcaptcha_token` | 否 | 可选 Stripe Elements captcha token |

成功创建返回 HTTP `202`：

```json
{
  "ok": true,
  "task_id": "TASK_ID",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "status_url": "/api/tasks/TASK_ID",
  "websocket_url": "/ws/tasks"
}
```

### 4.2 查询任务

```http
GET /api/tasks/TASK_ID
```

成功结果示例：

```json
{
  "ok": true,
  "task_id": "TASK_ID",
  "status": "succeeded",
  "stage": "completed",
  "progress": 100,
  "account_email": "account@example.com",
  "payment_method": "paypal",
  "billing_country": "DE",
  "session_kind": "openai_custom_checkout",
  "result": {
    "ok": true,
    "checkout_session_id": "SESSION_ID",
    "session_kind": "openai_custom_checkout",
    "payment_method": "paypal",
    "billing_country": "DE",
    "currency": "EUR",
    "amount_due": 0.0,
    "amount_due_minor": 0,
    "provider_url": "PROVIDER_AUTHORIZATION_URL",
    "paypal_url": "PROVIDER_AUTHORIZATION_URL"
  }
}
```

终态为：

- `succeeded`
- `failed`
- `cancelled`

### 4.3 列出任务

```http
GET /api/tasks
```

返回当前进程中尚未过期的任务。任务存储在内存中，服务重启后不会保留。

### 4.4 取消任务

```http
POST /api/tasks/TASK_ID/cancel
```

取消是协作式的：正在执行的 HTTP 请求结束并到达下一个 checkpoint 后生效。

### 4.5 重试任务

```http
POST /api/tasks/TASK_ID/retry
Content-Type: application/json
```

```json
{
  "checkout_proxy": "PROXY",
  "update_proxy": "PROXY"
}
```

允许重试 `failed`、`cancelled`，以及金额非零的成功任务。重试会删除旧任务并创建新任务，返回新的 `task_id`。

### 4.6 删除任务

```http
DELETE /api/tasks/TASK_ID
```

只允许删除终态任务。

批量删除：

```http
POST /api/tasks/bulk-delete
Content-Type: application/json
```

```json
{"target": "failed"}
```

`target` 可取 `failed` 或 `succeeded`。

### 4.7 测试代理

```http
POST /api/proxy/test
Content-Type: application/json
```

```json
{"checkout_proxy": "PROXY"}
```

成功响应：

```json
{
  "ok": true,
  "ip": "EXIT_IP",
  "country": "Brazil",
  "country_code": "BR",
  "region": "REGION",
  "region_code": "REGION_CODE"
}
```

### 4.8 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/defaults` | 前端默认配置 |
| `GET` | `/api/proxy/source` | 读取受支持的 HTTPS 代理订阅 |
| `POST` | `/api/tasks/TASK_ID/resolve-paypal` | 对成功任务再次解析严格 BA 链接 |

## 5. Python HTTP 客户端示例

下面的客户端可以直接复制到另一个 Python 项目：

```python
from __future__ import annotations

import time
import requests


class PaymentLinkClient:
    def __init__(self, base_url: str, password: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-Workbench-Password"] = password

    def create_task(
        self,
        *,
        access_token: str,
        checkout_proxy: str,
        update_proxy: str,
        country: str = "DE",
        apply_checkout_update: bool = True,
    ) -> str:
        response = self.session.post(
            f"{self.base_url}/api/tasks",
            json={
                "access_token": access_token,
                "checkout_proxy": checkout_proxy,
                "update_proxy": update_proxy,
                "country": country,
                "payment_method": "paypal",
                "apply_checkout_update": apply_checkout_update,
            },
            timeout=15,
        )
        response.raise_for_status()
        return str(response.json()["task_id"])

    def get_task(self, task_id: str) -> dict:
        response = self.session.get(
            f"{self.base_url}/api/tasks/{task_id}",
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def wait(self, task_id: str, timeout: float = 180) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.get_task(task_id)
            if task["status"] in {"succeeded", "failed", "cancelled"}:
                return task
            time.sleep(2)
        raise TimeoutError(f"task timed out: {task_id}")


client = PaymentLinkClient("http://127.0.0.1:18794", "PASSWORD")
task_id = client.create_task(
    access_token="TOKEN",
    checkout_proxy="PROXY",
    update_proxy="PROXY",
)
task = client.wait(task_id)

if task["status"] == "succeeded":
    authorization_url = task["result"]["provider_url"]
    print(authorization_url)
else:
    raise RuntimeError(task.get("error") or task["status"])
```

生产集成建议由调用方设置比服务端 HTTP 超时更长的任务总超时，并保留 `task_id` 便于追踪。

## 6. JavaScript/TypeScript 调用示例

```javascript
async function createPaymentTask(input) {
  const response = await fetch("http://127.0.0.1:18794/api/tasks", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Workbench-Password": "PASSWORD",
    },
    body: JSON.stringify({
      access_token: input.accessToken,
      checkout_proxy: input.checkoutProxy,
      update_proxy: input.updateProxy,
      country: "DE",
      payment_method: "paypal",
      apply_checkout_update: true,
    }),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
```

## 7. WebSocket 事件

连接地址：

```text
ws://127.0.0.1:18794/ws/tasks
```

连接后第一条消息必须是认证消息，即使密码为空也要发送：

```json
{"type": "auth", "password": "PASSWORD"}
```

认证成功：

```json
{"type": "auth.ok"}
```

事件格式：

```json
{
  "type": "task.stage",
  "task_id": "TASK_ID",
  "timestamp": "2026-08-14T00:00:00Z",
  "data": {
    "stage": "taxes",
    "status": "running",
    "progress": 65
  }
}
```

常见事件：

- `task.created`
- `task.started`
- `task.checkout_detected`
- `task.stage`
- `task.succeeded`
- `task.failed`
- `task.cancel_requested`
- `task.cancelled`
- `task.deleted`
- `task.ping`

WebSocket 用于实时刷新；任务最终状态仍应通过 `GET /api/tasks/TASK_ID` 确认。

## 8. Python 进程内调用

调用方与本项目处于同一 Python 环境时，可以绕过 Web 层：

```python
from payment_link_extractor import (
    ConfigurationError,
    ExtractionConfig,
    NetworkError,
    ProtocolError,
    extract_payment_link,
)

config = ExtractionConfig(
    access_token="TOKEN",
    checkout_proxy="PROXY",
    update_proxy="PROXY",
    country="DE",
    payment_method="paypal",
    apply_checkout_update=True,
    verbose=False,
)

try:
    result = extract_payment_link(config)
except ConfigurationError as exc:
    raise RuntimeError(f"invalid configuration: {exc}") from exc
except NetworkError as exc:
    raise RuntimeError(f"network stage={exc.stage}: {exc.detail}") from exc
except ProtocolError as exc:
    raise RuntimeError(f"upstream status={exc.status_code}: {exc.detail}") from exc
else:
    print(result.provider_url)
```

`extract_payment_link()` 是同步阻塞调用。需要并发时由调用方使用线程池，或优先使用 Web 任务服务。

## 9. 代理池集成

支持以下常见形式：

```text
http://USER:PASS@HOST:PORT
https://USER:PASS@HOST:PORT
socks5://USER:PASS@HOST:PORT
socks5h://USER:PASS@HOST:PORT
HOST:PORT:USER:PASS
```

对于识别到的特殊供应商域名，项目会把凭据编码进本地 HTTP CONNECT 请求并转发到 `127.0.0.1:18796`：

- 有本地前置 SOCKS 时保留链式路由；
- 本地前置 SOCKS 不存在时直连粘贴的上游代理；
- 没有订阅地址时代理桥仍可启动；
- 每条粘贴式代理都携带自己的动态凭据。

Web 页面支持每行一个代理并轮换。HTTP API 集成时建议调用方自己维护代理池，每个任务只传一条 Checkout Proxy 和一条 Update Proxy。

建议流程：

1. 调用 `/api/proxy/test` 筛选代理；
2. 优先选择目标国家和 IP 族；
3. 同一任务的 Checkout/Update 可使用同一稳定会话以保持出口一致；
4. 网络失败时换代理重试；
5. `not_eligible`、`generic_decline` 等业务错误不要归类为代理故障。

## 10. 状态和进度

标准阶段与进度：

| 阶段 | 进度 |
| --- | ---: |
| `queued` | 0 |
| `running` | 5 |
| `eligibility_check` | 10 |
| `checkout` | 15 |
| `checkout_update` | 25 |
| `stripe_init` | 35 |
| `elements_session` | 50 |
| `taxes` | 65 |
| `payment_confirmation` | 80 |
| `redirect_resolution` | 95 |
| `completed` | 100 |

任务管理器是进程内内存实现：

- 服务重启后任务消失；
- 终态任务在 TTL 后清理；
- WebSocket 历史是有限长度队列；
- 生产系统需要长期审计时，由调用方保存必要的非敏感结果。

## 11. 错误分类

| 错误或特征 | 含义 | 建议处理 |
| --- | --- | --- |
| `AT is required` | 缺少 Token | 检查请求字段 |
| `checkout proxy is required` | 缺少 Checkout Proxy | 为任务分配代理 |
| `Could not resolve proxy: eyJ...` | Token 被误填进代理字段 | 清理代理池，增加输入类型校验 |
| `proxy request timed out` | 代理线路超时 | 换代理会话 |
| `TLS connect error` / `SSL_ERROR_SYSCALL` | 代理或目标侧 TLS 中断 | 换已验证代理并限制重试次数 |
| `promo eligibility rejected: state=not_eligible` | 账号不满足优惠资格 | 优惠链场景排除该账号；普通结账可关闭更新 |
| `setup_attempt_failed / generic_decline` | 支付设置被上游拒绝 | 记录 Checkout 类型并按账号/支付路径处理 |
| `PayPal BA 链解析失败` | 中转地址没有解析出严格授权链接 | 换代理或稍后重试解析 |

`network_error=true` 只表示请求在收到 HTTP 响应前发生传输故障。上游返回 4xx/5xx 或业务拒绝不属于网络错误。

## 12. 输入校验建议

调用方提交前应检查：

- Token 是三段 JWT；
- 代理字段包含合法代理 scheme/host/port；
- Token 没有出现在代理池中；
- Checkout Proxy 与 Update Proxy 都是单条代理；
- `apply_checkout_update=true` 时 Update Proxy 非空；
- 记录 Token 对应账号的哈希或内部 ID，不在日志中保存完整 Token。

最小保护示例：

```python
from urllib.parse import urlsplit


def validate_inputs(access_token: str, proxy: str) -> None:
    if len(access_token.split(".")) != 3:
        raise ValueError("invalid access token")
    if len(proxy.split(".")) == 3 and "://" not in proxy:
        raise ValueError("access token was supplied as proxy")
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("unsupported proxy scheme")
    if not parsed.hostname or not parsed.port:
        raise ValueError("proxy host or port is missing")
```

## 13. 安全注意事项

- 不要把 Access Token、代理密码或最终授权链接写入普通日志；
- Web 服务默认仅监听 `127.0.0.1`；
- 改为局域网监听时配置 `OPLL_WEB_PASSWORD`、TLS 和防火墙；
- `/api/defaults` 可能返回服务端代理默认值，不应暴露给不受信任的调用方；
- 浏览器工作台会在 `localStorage` 保存密码和代理偏好；
- 服务端任务对象会在内存中持有 Token，直到任务删除、TTL 清理或进程退出；
- 生产集成应设置调用频率、并发和失败重试上限。

## 14. 集成检查清单

- [ ] `/api/health` 返回 `ok=true`
- [ ] `18796` 代理桥正在监听
- [ ] `/api/proxy/test` 返回预期出口国家
- [ ] `OPLL_FORCE_COUNTRY` 与账单需求一致
- [ ] 调用方不会把 Token 写入代理字段
- [ ] 调用方实现任务超时、轮询或 WebSocket 监听
- [ ] 调用方区分网络错误和业务拒绝
- [ ] 调用方不持久化完整 Token/代理密码
- [ ] 服务重启导致任务丢失的行为已被上层系统处理
- [ ] 成功结果使用 `result.provider_url`，并检查 `status == "succeeded"`

## 15. 项目入口

| 入口 | 用途 |
| --- | --- |
| `python -m payment_link_extractor.web --env-file .env` | Web/API 服务 |
| `python -m payment_link_extractor` | 单次 CLI 提取 |
| `payment_link_extractor.extract_payment_link` | Python 进程内调用 |
| `iprocket_chain_bridge.py` | 本地代理桥 |
| `START.bat` / `STOP.bat` | Windows 启停 |

日志默认位于：

```text
data/payment-link.log
```

排障时优先记录 `task_id`、`stage`、`session_kind`、`network_error` 和脱敏后的错误码。
