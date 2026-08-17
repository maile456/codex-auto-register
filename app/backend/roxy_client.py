from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import SecretStr

from .browser_automation import IP_CHECK_URL
from .oai_iprocket_chain_bridge import ensure_background_server
from .oai_payment_extractor.transport import chain_bridge_proxy_url
from .probe_store import ProxyLease
from .windows_system_proxy import roxy_system_proxy_chain_available


MANAGED_BROWSER_PREFIX = "AutoRegister Probe "
MANAGED_BROWSER_REMARK = "AutoRegister single-thread probe"

CHAIN_BRIDGE_PROXY_SUFFIXES = (
    "ipipbright.net",
    "1024proxy.io",
    "iprocket.io",
    "iprocket.pro",
    "iproyal.net",
    "iproyal.com",
)


def _requires_chain_bridge(host: str) -> bool:
    lowered = str(host or "").strip().lower().rstrip(".")
    return any(
        lowered == suffix or lowered.endswith("." + suffix)
        for suffix in CHAIN_BRIDGE_PROXY_SUFFIXES
    )


ROXY_ERROR_KINDS = frozenset(
    {
        "transport",
        "http",
        "api",
        "invalid_json",
        "invalid_structure",
        "contract",
    }
)


class RoxyApiError(RuntimeError):
    """A deliberately redacted Roxy local API failure."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "unknown",
        http_status: int | None = None,
        api_code: int | None = None,
        elapsed_ms: int = 0,
        retryable: bool = True,
        error_kind: str = "contract",
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.http_status = http_status
        self.api_code = api_code
        self.elapsed_ms = max(0, elapsed_ms)
        self.retryable = retryable
        self.error_kind = (
            error_kind if error_kind in ROXY_ERROR_KINDS else "contract"
        )

    @property
    def is_auth_failure(self) -> bool:
        return self.http_status in (401, 403)


@dataclass(frozen=True, slots=True)
class RoxyWorkspace:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class RoxyOpenResult:
    ws: str
    http: str
    pid: int | None
    recovered: bool = False
    recovery_elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class RoxyConnectionInfo:
    dir_id: str
    ws: str
    http: str
    pid: int | None
    window_name: str = ""
    window_remark: str = ""


@dataclass(frozen=True, slots=True)
class RoxyBrowserRecord:
    dir_id: str
    window_name: str
    window_remark: str


class RoxyClient:
    def __init__(
        self,
        port: int,
        token: SecretStr,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
        bridge_starter: Callable[[], bool] | None = None,
        system_proxy_chain_detector: Callable[[], bool] | None = None,
    ) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self._bridge_starter = bridge_starter or ensure_background_server
        self._system_proxy_chain_detector = (
            system_proxy_chain_detector or roxy_system_proxy_chain_available
        )
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "token": token.get_secret_value(),
            },
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> "RoxyClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        started = monotonic()
        request_options: dict[str, Any] = {
            "params": params,
            "json": json_body,
        }
        if timeout_seconds is not None:
            request_options["timeout"] = timeout_seconds
        try:
            response = await self._http.request(
                method,
                path,
                **request_options,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise RoxyApiError(
                "Roxy API 返回了 HTTP 错误",
                operation=operation,
                http_status=status,
                elapsed_ms=int((monotonic() - started) * 1000),
                retryable=status not in (401, 403),
                error_kind="http",
            ) from None
        except httpx.HTTPError:
            raise RoxyApiError(
                "Roxy API 请求失败",
                operation=operation,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_kind="transport",
            ) from None

        try:
            payload = response.json()
        except ValueError:
            raise RoxyApiError(
                "Roxy API 返回了无效 JSON",
                operation=operation,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_kind="invalid_json",
            ) from None
        if not isinstance(payload, dict):
            raise RoxyApiError(
                "Roxy API 返回结构错误",
                operation=operation,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_kind="invalid_structure",
            )
        code = payload.get("code")
        if code != 0:
            safe_code = code if isinstance(code, int) and not isinstance(code, bool) else None
            suffix = f"，code={safe_code}" if safe_code is not None else ""
            raise RoxyApiError(
                f"Roxy API 调用未成功{suffix}",
                operation=operation,
                api_code=safe_code,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_kind="api",
            )
        return payload

    async def health(self) -> None:
        await self._request("GET", "/health", operation="health")

    async def workspaces(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> list[RoxyWorkspace]:
        payload = await self._request(
            "GET",
            "/browser/workspace",
            operation="workspace_list",
            params={"page_index": 1, "page_size": 100},
            timeout_seconds=timeout_seconds,
        )
        data = payload.get("data")
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RoxyApiError(
                "Roxy workspace 响应缺少 rows",
                operation="workspace_list",
            )
        result: list[RoxyWorkspace] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            workspace_id = row.get("id")
            if not isinstance(workspace_id, int):
                continue
            result.append(
                RoxyWorkspace(
                    id=workspace_id,
                    name=str(row.get("workspaceName") or f"workspace-{workspace_id}"),
                )
            )
        return result

    async def browsers(
        self,
        workspace_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[RoxyBrowserRecord]:
        payload = await self._request(
            "GET",
            "/browser/list",
            operation="browser_list",
            params={
                "workspaceId": workspace_id,
                "page_index": 1,
                "page_size": 100,
            },
            timeout_seconds=timeout_seconds,
        )
        data = payload.get("data")
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RoxyApiError(
                "Roxy browser list 响应缺少 rows",
                operation="browser_list",
            )
        result: list[RoxyBrowserRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            dir_id = row.get("dirId")
            if not isinstance(dir_id, str) or not dir_id:
                continue
            result.append(
                RoxyBrowserRecord(
                    dir_id=dir_id,
                    window_name=str(row.get("windowName") or ""),
                    window_remark=str(row.get("windowRemark") or ""),
                )
            )
        return result

    async def create_browser(
        self,
        workspace_id: int,
        probe_id: str,
        proxy: ProxyLease,
    ) -> str:
        if (
            proxy.username
            and _requires_chain_bridge(proxy.host)
            and not self._system_proxy_chain_detector()
        ):
            self._bridge_starter()
            bridge_url = urlsplit(
                chain_bridge_proxy_url(
                    proxy.host,
                    proxy.port,
                    proxy.username,
                    proxy.password,
                    proxy.scheme,
                )
            )
            proxy = ProxyLease(
                id=proxy.id,
                host=str(bridge_url.hostname or "127.0.0.1"),
                port=int(bridge_url.port or 18796),
                username=unquote(bridge_url.username or ""),
                password=unquote(bridge_url.password or ""),
                country=proxy.country,
                group=proxy.group,
                scheme="http",
            )
        category = {
            "http": "HTTP",
            "https": "HTTPS",
            "socks5": "SOCKS5",
            "socks5h": "SOCKS5",
        }.get(str(proxy.scheme or "http").lower(), "HTTP")
        payload = await self._request(
            "POST",
            "/browser/create",
            operation="browser_create",
            json_body={
                "workspaceId": workspace_id,
                "windowName": f"{MANAGED_BROWSER_PREFIX}{probe_id[:8]}",
                "windowRemark": MANAGED_BROWSER_REMARK,
                "coreType": "Chrome",
                "os": "Windows",
                "defaultOpenUrl": [IP_CHECK_URL],
                "proxyInfo": {
                    "moduleId": 0,
                    "proxyMethod": "custom",
                    "proxyCategory": category,
                    "ipType": "IPV4",
                    "protocol": category,
                    "host": proxy.host,
                    "port": str(proxy.port),
                    "proxyUserName": proxy.username,
                    "proxyPassword": proxy.password,
                    "checkChannel": "IPRust.io",
                },
                "fingerInfo": {
                    "isLanguageBaseIp": True,
                    "isDisplayLanguageBaseIp": True,
                    "isTimeZone": True,
                    "isPositionBaseIp": True,
                    "position": 0,
                },
            },
        )
        data = payload.get("data")
        dir_id = data.get("dirId") if isinstance(data, dict) else None
        if not isinstance(dir_id, str) or not dir_id:
            raise RoxyApiError(
                "Roxy 创建窗口成功响应缺少 dirId",
                operation="browser_create",
            )
        return dir_id

    async def open_browser(
        self,
        workspace_id: int,
        dir_id: str,
        *,
        headless: bool,
    ) -> RoxyOpenResult:
        payload = await self._request(
            "POST",
            "/browser/open",
            operation="browser_open",
            json_body={
                "workspaceId": workspace_id,
                "dirId": dir_id,
                "forceOpen": False,
                "headless": headless,
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RoxyApiError(
                "Roxy 打开窗口响应缺少 data",
                operation="browser_open",
            )
        ws = data.get("ws")
        http_endpoint = data.get("http")
        if not isinstance(ws, str) or not ws.startswith("ws://"):
            raise RoxyApiError(
                "Roxy 打开窗口响应缺少有效 CDP ws",
                operation="browser_open",
            )
        return RoxyOpenResult(
            ws=ws,
            http=str(http_endpoint or ""),
            pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        )

    async def connection_info(
        self,
        dir_ids: list[str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> list[RoxyConnectionInfo]:
        params = None
        if dir_ids:
            params = {"dirIds": ",".join(dir_ids)}
        payload = await self._request(
            "GET",
            "/browser/connection_info",
            operation="browser_connection_info",
            params=params,
            timeout_seconds=timeout_seconds,
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise RoxyApiError(
                "Roxy 已打开窗口响应缺少 data",
                operation="browser_connection_info",
                error_kind="contract",
            )
        result: list[RoxyConnectionInfo] = []
        for row in data:
            if not isinstance(row, dict):
                raise RoxyApiError(
                    "Roxy 已打开窗口响应包含无效项目",
                    operation="browser_connection_info",
                    error_kind="contract",
                )
            dir_id = row.get("dirId")
            ws = row.get("ws")
            if (
                not isinstance(dir_id, str)
                or not dir_id
                or not isinstance(ws, str)
                or not ws.startswith("ws://")
            ):
                raise RoxyApiError(
                    "Roxy 已打开窗口响应字段无效",
                    operation="browser_connection_info",
                    error_kind="contract",
                )
            result.append(
                RoxyConnectionInfo(
                    dir_id=dir_id,
                    ws=ws,
                    http=str(row.get("http") or ""),
                    pid=row.get("pid") if isinstance(row.get("pid"), int) else None,
                    window_name=str(row.get("windowName") or ""),
                    window_remark=str(row.get("windowRemark") or ""),
                )
            )
        return result

    async def close_browser(self, dir_id: str) -> None:
        await self._request(
            "POST",
            "/browser/close",
            operation="browser_close",
            json_body={"dirId": dir_id},
        )

    async def delete_browser(self, workspace_id: int, dir_id: str) -> None:
        await self._request(
            "POST",
            "/browser/delete",
            operation="browser_delete",
            json_body={
                "workspaceId": workspace_id,
                "dirIds": [dir_id],
                "isSoftDelete": False,
            },
        )
