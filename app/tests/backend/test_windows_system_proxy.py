from __future__ import annotations

from backend.windows_system_proxy import roxy_system_proxy_chain_available


def test_system_proxy_chain_is_disabled_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("backend.windows_system_proxy.os.name", "posix")
    assert roxy_system_proxy_chain_available() is False
