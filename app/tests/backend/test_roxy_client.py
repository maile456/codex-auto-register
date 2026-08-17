from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from backend.probe_store import ProxyLease
from backend.roxy_client import (
    RoxyApiError,
    RoxyBrowserRecord,
    RoxyClient,
    RoxyConnectionInfo,
    RoxyWorkspace,
)
from backend.run_manager import RunManager


TEST_KEY = "TEST_ROXY_KEY_DO_NOT_LOG"
TEST_PROXY_PASSWORD = "TEST_PROXY_PASSWORD_DO_NOT_LOG"


def test_stale_cleanup_deletes_only_autoregister_managed_browsers() -> None:
    class FakeCleanupRoxy:
        def __init__(self) -> None:
            self.closed: list[str] = []
            self.deleted: list[tuple[int, str]] = []
            self.connections = [
                RoxyConnectionInfo(
                    "owned-ghost",
                    "ws://127.0.0.1:50001/devtools/browser/ghost",
                    "127.0.0.1:50001",
                    123,
                    "AutoRegister Probe ghost",
                    "AutoRegister single-thread probe",
                ),
                RoxyConnectionInfo(
                    "foreign-open",
                    "ws://127.0.0.1:50002/devtools/browser/foreign",
                    "127.0.0.1:50002",
                    124,
                    "another-project-worker",
                    "another project",
                ),
            ]

        async def connection_info(self, **_kwargs: object):
            return list(self.connections)

        async def browsers(self, workspace_id: int, **_kwargs: object):
            assert workspace_id == 7
            return [
                RoxyBrowserRecord(
                    "owned-stale",
                    "AutoRegister Probe stale",
                    "AutoRegister single-thread probe",
                ),
                RoxyBrowserRecord(
                    "foreign",
                    "another-project-worker",
                    "another project",
                ),
            ]

        async def close_browser(self, dir_id: str) -> None:
            self.closed.append(dir_id)
            self.connections = [
                connection
                for connection in self.connections
                if connection.dir_id != dir_id
            ]

        async def delete_browser(self, workspace_id: int, dir_id: str) -> None:
            self.deleted.append((workspace_id, dir_id))

    fake = FakeCleanupRoxy()
    asyncio.run(
        RunManager._cleanup_stale_managed_browsers(
            fake,  # type: ignore[arg-type]
            [RoxyWorkspace(7, "Local")],
        )
    )
    assert fake.closed == ["owned-ghost", "owned-stale"]
    assert fake.deleted == [(7, "owned-stale")]


def test_roxy_client_uses_token_and_expected_window_contract() -> None:
    requests: list[tuple[str, str, dict[str, object] | None, str]] = []
    connection_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append(
            (request.method, request.url.path, body, request.headers.get("token", ""))
        )
        if request.url.path == "/health":
            return httpx.Response(200, json={"code": 0, "msg": "成功"})
        if request.url.path == "/browser/workspace":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"rows": [{"id": 7, "workspaceName": "Local"}]},
                },
            )
        if request.url.path == "/browser/create":
            return httpx.Response(200, json={"code": 0, "data": {"dirId": "dir-1"}})
        if request.url.path == "/browser/list":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "rows": [
                            {
                                "dirId": "dir-stale",
                                "windowName": "AutoRegister Probe stale",
                                "windowRemark": "AutoRegister single-thread probe",
                            }
                        ],
                        "total": 1,
                    },
                },
            )
        if request.url.path == "/browser/open":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "ws": "ws://127.0.0.1:52314/devtools/browser/test",
                        "http": "127.0.0.1:52314",
                        "pid": 123,
                    },
                },
            )
        if request.url.path == "/browser/connection_info":
            connection_queries.append(request.url.query.decode("ascii"))
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": [
                        {
                            "dirId": "dir-1",
                            "ws": "ws://127.0.0.1:52314/devtools/browser/test",
                            "http": "127.0.0.1:52314",
                            "pid": 123,
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"code": 0, "msg": "成功"})

    async def scenario() -> None:
        proxy = ProxyLease(
            id="proxy-1",
            host="proxy.example.com",
            port=10000,
            username="proxy-user",
            password=TEST_PROXY_PASSWORD,
        )
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.health()
            assert (await client.workspaces())[0].id == 7
            browsers = await client.browsers(7)
            assert len(browsers) == 1
            assert browsers[0].dir_id == "dir-stale"
            assert browsers[0].window_name == "AutoRegister Probe stale"
            dir_id = await client.create_browser(7, "probe-123", proxy)
            opened = await client.open_browser(7, dir_id, headless=True)
            assert opened.ws.startswith("ws://127.0.0.1:")
            connections = await client.connection_info([dir_id])
            assert len(connections) == 1
            assert connections[0].dir_id == dir_id
            assert connections[0].ws == opened.ws
            await client.close_browser(dir_id)
            await client.delete_browser(7, dir_id)

    asyncio.run(scenario())

    assert requests
    assert all(item[3] == TEST_KEY for item in requests)
    create_body = next(item[2] for item in requests if item[1] == "/browser/create")
    assert create_body is not None
    assert create_body["proxyInfo"] == {
        "moduleId": 0,
        "proxyMethod": "custom",
        "proxyCategory": "HTTP",
        "ipType": "IPV4",
        "protocol": "HTTP",
        "host": "proxy.example.com",
        "port": "10000",
        "proxyUserName": "proxy-user",
        "proxyPassword": TEST_PROXY_PASSWORD,
        "checkChannel": "IPRust.io",
    }
    open_body = next(item[2] for item in requests if item[1] == "/browser/open")
    assert open_body is not None
    assert open_body["headless"] is True
    assert create_body["defaultOpenUrl"] == [
        "https://api64.ipify.org?format=json"
    ]
    assert connection_queries == ["dirIds=dir-1"]
    assert any(item[1] == "/browser/list" for item in requests)


def test_roxy_create_uses_socks5_category_for_socks5_proxy() -> None:
    create_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/browser/create":
            create_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"code": 0, "data": {"dirId": "dir-socks"}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def scenario() -> None:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.create_browser(
                7,
                "probe-socks",
                ProxyLease(
                    id="proxy-socks",
                    host="proxy.example.com",
                    port=3000,
                    username="proxy-user",
                    password=TEST_PROXY_PASSWORD,
                    scheme="socks5",
                ),
            )

    asyncio.run(scenario())
    proxy_info = create_payload["proxyInfo"]
    assert isinstance(proxy_info, dict)
    assert proxy_info["proxyCategory"] == "SOCKS5"
    assert proxy_info["protocol"] == "SOCKS5"


def test_roxy_create_routes_vendor_proxy_through_local_bridge() -> None:
    create_payload: dict[str, object] = {}
    bridge_starts: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/browser/create":
            create_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"code": 0, "data": {"dirId": "dir-bridge"}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def scenario() -> None:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
            bridge_starter=lambda: bridge_starts.append(True) or True,
        ) as client:
            await client.create_browser(
                7,
                "probe-bridge",
                ProxyLease(
                    id="proxy-bridge",
                    host="gateway.ipipbright.net",
                    port=1000,
                    username="vendor-user",
                    password=TEST_PROXY_PASSWORD,
                    scheme="http",
                ),
            )

    asyncio.run(scenario())
    assert bridge_starts == [True]
    proxy_info = create_payload["proxyInfo"]
    assert isinstance(proxy_info, dict)
    assert proxy_info["host"] == "127.0.0.1"
    assert proxy_info["port"] == "18796"
    assert proxy_info["proxyCategory"] == "HTTP"
    assert proxy_info["protocol"] == "HTTP"
    assert proxy_info["proxyPassword"] == TEST_PROXY_PASSWORD
    bridge_user = str(proxy_info["proxyUserName"])
    assert bridge_user.startswith("iprb_")
    encoded = bridge_user.removeprefix("iprb_")
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    assert base64.urlsafe_b64decode(encoded).decode("utf-8") == (
        "http|gateway.ipipbright.net|1000|vendor-user"
    )


def test_roxy_errors_are_structured_without_echoing_response_or_credentials() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 500,
                "msg": f"bad {TEST_KEY} {TEST_PROXY_PASSWORD}",
            },
        )

    async def scenario() -> RoxyApiError:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(RoxyApiError) as caught:
                await client.health()
        return caught.value

    error = asyncio.run(scenario())
    message = str(error)
    assert TEST_KEY not in message
    assert TEST_PROXY_PASSWORD not in message
    assert "code=500" in message
    assert error.operation == "health"
    assert error.api_code == 500
    assert error.http_status is None
    assert error.retryable is True
    assert error.error_kind == "api"


@pytest.mark.parametrize("status", [401, 403])
def test_roxy_auth_errors_are_non_retryable_and_redacted(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            text=f"PRIVATE_RESPONSE {TEST_KEY} {TEST_PROXY_PASSWORD}",
        )

    async def scenario() -> RoxyApiError:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(RoxyApiError) as caught:
                await client.workspaces(timeout_seconds=3)
        return caught.value

    error = asyncio.run(scenario())

    assert error.operation == "workspace_list"
    assert error.http_status == status
    assert error.api_code is None
    assert error.retryable is False
    assert error.is_auth_failure is True
    assert error.error_kind == "http"
    assert TEST_KEY not in str(error)
    assert TEST_PROXY_PASSWORD not in str(error)
    assert "PRIVATE_RESPONSE" not in str(error)


def test_workspace_request_uses_independent_three_second_timeout() -> None:
    timeout_extension: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        timeout_extension.update(request.extensions.get("timeout", {}))
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"rows": [{"id": 7, "workspaceName": "Local"}]},
            },
        )

    async def scenario() -> None:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.workspaces(timeout_seconds=3)

    asyncio.run(scenario())

    assert timeout_extension
    assert set(timeout_extension.values()) == {3.0}


@pytest.mark.parametrize(
    ("response", "error_kind"),
    [
        (httpx.Response(200, text="PRIVATE_INVALID_JSON"), "invalid_json"),
        (httpx.Response(200, json=[{"code": 0}]), "invalid_structure"),
    ],
)
def test_roxy_response_shape_errors_are_safely_classified(
    response: httpx.Response,
    error_kind: str,
) -> None:
    async def scenario() -> RoxyApiError:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(lambda _request: response),
        ) as client:
            with pytest.raises(RoxyApiError) as caught:
                await client.health()
        return caught.value

    error = asyncio.run(scenario())

    assert error.error_kind == error_kind
    assert "PRIVATE_INVALID_JSON" not in str(error)
    assert TEST_KEY not in str(error)


def test_roxy_transport_error_is_safely_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("PRIVATE_TRANSPORT_DETAIL", request=request)

    async def scenario() -> RoxyApiError:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(RoxyApiError) as caught:
                await client.health()
        return caught.value

    error = asyncio.run(scenario())

    assert error.error_kind == "transport"
    assert "PRIVATE_TRANSPORT_DETAIL" not in str(error)


def test_roxy_connection_info_rejects_invalid_ws_contract() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [{"dirId": "dir-1", "ws": "PRIVATE_WS"}],
            },
        )

    async def scenario() -> RoxyApiError:
        async with RoxyClient(
            50000,
            SecretStr(TEST_KEY),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(RoxyApiError) as caught:
                await client.connection_info(["dir-1"])
        return caught.value

    error = asyncio.run(scenario())

    assert error.operation == "browser_connection_info"
    assert error.error_kind == "contract"
    assert "PRIVATE_WS" not in str(error)
