from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

try:
    import requests
except ImportError:  # pragma: no cover - runtime dependency is declared in requirements.txt
    requests = None  # type: ignore[assignment]

from ..transport import normalize_proxy_url


IP_LOOKUP_URL = "https://ipwho.is/"
IP_LOOKUP_FIELDS = "ip,country,country_code,region,region_code"


class ProxyProbeError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProxyLocation:
    ip: str
    country: str
    country_code: str
    region: str
    region_code: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def probe_proxy(
    checkout_proxy: str,
    *,
    request_get: Callable[..., Any] | None = None,
    timeout: float = 10.0,
) -> ProxyLocation:
    proxy = _normalize_probe_proxy(checkout_proxy)
    if request_get is None:
        if requests is None:
            raise ProxyProbeError("requests is required for proxy testing", 500)
        request_get = requests.get

    try:
        response = request_get(
            IP_LOOKUP_URL,
            params={"fields": IP_LOOKUP_FIELDS},
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
        )
    except Exception as exc:
        if requests is not None and isinstance(exc, requests.exceptions.Timeout):
            raise ProxyProbeError("proxy request timed out", 504) from exc
        if requests is not None and isinstance(exc, requests.exceptions.RequestException):
            raise ProxyProbeError("proxy connection failed", 502) from exc
        raise ProxyProbeError("proxy connection failed", 502) from exc

    if getattr(response, "status_code", 0) != 200:
        raise ProxyProbeError("IP lookup service unavailable", 502)
    try:
        payload = response.json()
    except Exception as exc:
        raise ProxyProbeError("IP lookup returned invalid JSON", 502) from exc
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise ProxyProbeError("IP lookup failed", 502)

    ip = _field(payload, "ip")
    if not ip:
        raise ProxyProbeError("IP lookup returned no address", 502)
    return ProxyLocation(
        ip=ip,
        country=_field(payload, "country"),
        country_code=_field(payload, "country_code"),
        region=_field(payload, "region"),
        region_code=_field(payload, "region_code"),
    )


def _normalize_probe_proxy(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProxyProbeError("checkout_proxy is required", 400)
    try:
        normalized = normalize_proxy_url(text)
        parsed = urlsplit(normalized)
    except Exception as exc:
        raise ProxyProbeError("invalid checkout_proxy", 400) from exc
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ProxyProbeError("checkout_proxy must be an HTTP/HTTPS/SOCKS5 proxy", 400)
    return normalized


def _field(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    return str(value).strip() if value is not None else ""
