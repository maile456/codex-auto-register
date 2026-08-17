from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.main import create_app
from backend.mongo_manager import MongoManager
from backend.paypal_agreement_service import (
    PaypalAgreementService,
    PaypalAgreementServiceError,
    SOURCE_COMMIT,
)


class FakeAgreementService:
    source_commit = SOURCE_COMMIT
    host = "127.0.0.1"
    port = 18098
    base_url = "http://127.0.0.1:18098"
    ui_path = "/paypal-pay/"

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.closed = 0

    def snapshot(self, status: str = "online") -> dict:
        return {
            "ok": status == "online",
            "status": status,
            "service": "paypal-agreement-protocol",
            "sourceCommit": SOURCE_COMMIT,
            "host": self.host,
            "port": self.port,
            "uiPath": self.ui_path,
            "uiUrl": self.ui_path,
            "managed": True,
            "pid": 1234,
        }

    def status(self) -> dict:
        return self.snapshot("online" if self.started else "stopped")

    def start(self) -> dict:
        self.started += 1
        return self.snapshot()

    def stop(self) -> dict:
        self.stopped += 1
        return self.snapshot("stopped")

    def close(self) -> None:
        self.closed += 1


def build_test_app(tmp_path: Path, service: FakeAgreementService):
    return create_app(
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        mongo_manager=MongoManager(uri="mongodb://127.0.0.1:1", database_name="agreement_test"),
        paypal_agreement_service=service,  # type: ignore[arg-type]
    )


def test_agreement_control_api_is_namespaced_and_preserves_health(tmp_path: Path) -> None:
    service = FakeAgreementService()
    client = TestClient(build_test_app(tmp_path, service))

    status_response = client.get("/api/paypal-agreement/status")
    start_response = client.post("/api/paypal-agreement/start")
    stop_response = client.post("/api/paypal-agreement/stop")
    source_response = client.get("/api/paypal-agreement/source")

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "stopped"
    assert start_response.status_code == 200
    assert start_response.json()["uiPath"] == "/paypal-pay/"
    assert stop_response.status_code == 200
    assert source_response.json() == {
        "service": "paypal-agreement-protocol",
        "sourceCommit": SOURCE_COMMIT,
        "uiPath": "/paypal-pay/",
        "isolated": True,
    }
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/payment-extractor/defaults").status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_agreement_reverse_proxy_rewrites_frame_and_cookie_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    service = FakeAgreementService()
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, request_method, url, *, headers, content):
            captured.update(method=request_method, url=url, headers=headers, content=content)
            return httpx.Response(
                200,
                content=b"<html>fixture</html>",
                headers=[
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("X-Frame-Options", "DENY"),
                    ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'"),
                    ("Set-Cookie", "paypal_web_device_id=fixture; Path=/; HttpOnly; SameSite=Strict"),
                ],
            )

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(build_test_app(tmp_path, service))
    headers = {
        "Origin": "http://outer.fixture",
        "Referer": "http://outer.fixture/agreement-tools",
        "Content-Type": "application/json",
    }
    response = client.request(method, "/paypal-pay/api/health?fixture=1", headers=headers, content=b"{}")

    assert response.status_code == 200
    assert captured["url"] == "http://127.0.0.1:18098/api/health?fixture=1"
    assert "origin" not in captured["headers"]
    assert "referer" not in captured["headers"]
    assert captured["headers"]["host"] == "127.0.0.1:18098"
    assert "x-frame-options" not in response.headers
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "Path=/paypal-pay/" in response.headers["set-cookie"]


def test_sidecar_manager_starts_with_isolated_defaults(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "web.py").write_text("# fixture", encoding="utf-8")
    probe_calls = 0
    created: dict = {}

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def probe():
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls >= 2:
            return True, {"service": "paypal-agreement-protocol"}, ""
        return False, None, "not ready"

    def popen(*args, **kwargs):
        created["args"] = args
        created["kwargs"] = kwargs
        return FakeProcess()

    env_path = tmp_path / ".env"
    env_path.write_text(
        "IPROCKET_PRE_PROXY_HOST=127.0.0.1\nIPROCKET_PRE_PROXY_PORT=7897\n",
        encoding="utf-8",
    )
    service = PaypalAgreementService(
        vendor_dir=vendor,
        port=18099,
        startup_timeout=1,
        log_path=tmp_path / "sidecar.log",
        env_path=env_path,
        probe=probe,
        popen_factory=popen,
    )
    status = service.start()

    assert status["status"] == "online"
    assert status["managed"] is True
    assert status["sourceCommit"] == SOURCE_COMMIT
    env = created["kwargs"]["env"]
    assert env["PAYPAL_WEB_ENABLE_PAY153_BRIDGE"] == "0"
    assert env["PAYPAL_WEB_FULL_LOGS"] == "0"
    assert env["IPROCKET_PRE_PROXY_HOST"] == "127.0.0.1"
    assert env["IPROCKET_PRE_PROXY_PORT"] == "7897"
    assert created["kwargs"]["cwd"] == str(vendor)
    service.close()


def test_sidecar_manager_rejects_an_unrelated_listener(tmp_path: Path) -> None:
    service = PaypalAgreementService(
        vendor_dir=tmp_path,
        port=18100,
        probe=lambda: (False, {"service": "fixture-other"}, "occupied"),
    )

    with pytest.raises(PaypalAgreementServiceError) as exc_info:
        service.start()

    assert exc_info.value.code == "port_in_use"
    assert exc_info.value.status_code == 409


def test_sidecar_manager_keeps_the_listener_on_loopback(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        PaypalAgreementService(vendor_dir=tmp_path, host="0.0.0.0", port=18101)
