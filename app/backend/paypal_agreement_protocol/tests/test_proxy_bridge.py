import base64
import sys
from types import SimpleNamespace

from paypal.proxy import ProxyConfig, ProxyEntry


def test_1024proxy_uses_dynamic_bridge_url(monkeypatch):
    monkeypatch.setenv("PAYPAL_PROXY_USE_BRIDGE", "1")
    entry = ProxyEntry.parse("gw.1024proxy.io:9595:USER:PASS")

    url = entry.url
    assert entry.uses_bridge is True
    assert url.startswith("http://iprb_")
    assert "gw.1024proxy.io:9595" not in url
    assert "USER" not in url
    encoded = url.split("iprb_", 1)[1].split(":", 1)[0]
    decoded = base64.urlsafe_b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4)).decode()
    assert decoded == "socks5|gw.1024proxy.io|9595|USER"


def test_non_vendor_proxy_keeps_direct_url(monkeypatch):
    monkeypatch.setenv("PAYPAL_PROXY_USE_BRIDGE", "1")
    entry = ProxyEntry.parse("http://USER:PASS@proxy.example.test:8080")
    assert entry.uses_bridge is False
    assert entry.url == "http://USER:PASS@proxy.example.test:8080"


def test_prepare_starts_bridge_only_for_vendor(monkeypatch):
    calls = []
    monkeypatch.setenv("PAYPAL_PROXY_USE_BRIDGE", "1")
    bridge = SimpleNamespace(ensure_background_server=lambda: calls.append(True) or True)
    monkeypatch.setitem(sys.modules, "oai_iprocket_chain_bridge", bridge)
    assert ProxyConfig(True, ProxyEntry.parse("gw.1024proxy.io:9595:USER:PASS")).prepare() is True
    assert ProxyConfig(True, ProxyEntry.parse("proxy.example.test:8080:USER:PASS")).prepare() is False
    assert calls == [True]
