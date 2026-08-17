from __future__ import annotations

import os
import base64
import time
import uuid
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:  # pragma: no cover - installation issue handled at runtime
    requests = None  # type: ignore

from .config import DEFAULT_TIMEOUT, DEFAULT_USER_AGENT
from .errors import ConfigurationError, NetworkError, ProtocolError
from .logging_utils import compact_url, emit_log, safe_log_text
from .models import ExtractionConfig

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:  # pragma: no cover
    CurlCffiSession = None  # type: ignore

try:
    from curl_cffi.requests import RequestException as CurlCffiRequestException  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from curl_cffi.requests.errors import RequestException as CurlCffiRequestException  # type: ignore
    except ImportError:  # pragma: no cover
        CurlCffiRequestException = None  # type: ignore

try:
    from curl_cffi.requests import HTTPError as CurlCffiHTTPError  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from curl_cffi.requests.errors import HTTPError as CurlCffiHTTPError  # type: ignore
    except ImportError:  # pragma: no cover
        CurlCffiHTTPError = None  # type: ignore


class TransportFactory(Protocol):
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any: ...

    def stripe(self, config: ExtractionConfig) -> Any: ...


def new_session() -> Any:
    if CurlCffiSession is not None:
        return CurlCffiSession(impersonate="firefox")
    if requests is None:
        raise ConfigurationError("requests is required; install requirements.txt")
    return requests.Session()


def safe_close(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _is_iprocket_host(host: str) -> bool:
    lowered = str(host or "").lower().rstrip(".")
    return (
        lowered.endswith(".iprocket.io")
        or lowered.endswith(".iprocket.pro")
        or lowered == "proxy.iproyal.net"
        or lowered.endswith(".iproyal.net")
        or lowered == "proxy.iproyal.com"
        or lowered.endswith(".iproyal.com")
        or lowered == "1024proxy.io"
        or lowered.endswith(".1024proxy.io")
    )


def _iprocket_protocol(port: int, scheme: str = "") -> str:
    lowered = str(scheme or "").lower()
    if lowered.startswith("socks"):
        return "socks5"
    if lowered in {"http", "https"}:
        return "http"
    if port in {9595, 59999, 619999}:
        return "socks5"
    if port in {5959, 61999}:
        return "http"
    return "auto"


def chain_bridge_proxy_url(
    host: str,
    port: int,
    username: str,
    password: str,
    scheme: str = "",
) -> str:
    bridge = os.getenv("IPROCKET_CHAIN_PROXY", "http://127.0.0.1:18796")
    protocol = (
        "socks5"
        if "1024proxy." in host.lower()
        else "http" if "iproyal." in host.lower() else _iprocket_protocol(port, scheme)
    )
    metadata = base64.urlsafe_b64encode(
        f"{protocol}|{host}|{port}|{username}".encode("utf-8")
    ).decode("ascii").rstrip("=")
    parsed_bridge = urlsplit(bridge)
    bridge_host = parsed_bridge.hostname or "127.0.0.1"
    bridge_port = parsed_bridge.port or 18796
    return (
        f"http://iprb_{metadata}:{quote(password, safe='')}"
        f"@{bridge_host}:{bridge_port}"
    )


# Backward-compatible internal name used by the existing extractor paths.
_iprocket_bridge_proxy = chain_bridge_proxy_url


def normalize_proxy_url(proxy: str) -> str:
    # The web UI accepts proxy pools (one entry per line).  A transport always
    # receives one proxy, so use the first non-empty entry as a safe fallback
    # for API clients that submit the pool without selecting an entry first.
    lines = [line.strip() for line in str(proxy or "").splitlines() if line.strip()]
    text = lines[0] if lines else ""
    if not text:
        return ""
    # IPRocket share/subscription URL: resolve it to the first exported entry.
    try:
        source = urlsplit(text)
        if (
            source.scheme == "https"
            and source.hostname == "app.iprocket.io"
            and source.path.endswith("/clienta/sysnation/getLink")
        ):
            request = Request(text, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=15) as response:
                exported = [
                    line.strip()
                    for line in response.read(1024 * 1024).decode("utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
            return normalize_proxy_url(exported[0] if exported else "")
    except Exception as exc:
        raise ValueError("IPRocket proxy subscription could not be read") from exc
    # IPRocket QR exports use socks://BASE64 or http://BASE64 rather than a
    # conventional URL. Decode that representation before normalizing.
    if text.lower().startswith(("socks://", "http://")) and "@" not in text:
        encoded = text.split("://", 1)[1].strip()
        try:
            padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
            decoded = base64.b64decode(padded).decode("utf-8").strip()
            if "iprocket." in decoded.lower():
                return normalize_proxy_url(decoded)
        except Exception:
            pass
    had_explicit_scheme = "://" in text
    # IPRocket dashboard export formats 1/2/3. Password remains the fourth
    # field so punctuation inside it is preserved.
    if "://" not in text and "@" not in text:
        separator = next((item for item in (":", "|", ",", ";") if text.count(item) >= 3), ":")
        parts = text.split(separator, 3)
        parsed_vendor: tuple[str, str, str, str] | None = None
        if len(parts) == 4 and _is_iprocket_host(parts[0]) and parts[1].isdigit():  # host:port:user:pass
            parsed_vendor = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 4 and parts[0].isdigit() and _is_iprocket_host(parts[1]):  # port:host:user:pass
            parsed_vendor = parts[1], parts[0], parts[2], parts[3]
        elif len(parts) == 4 and parts[1].isdigit() and _is_iprocket_host(parts[2]):  # pass:port:host:user
            parsed_vendor = parts[2], parts[1], parts[3], parts[0]
        elif len(parts) == 4 and parts[3].isdigit() and _is_iprocket_host(parts[2]):  # user:pass:host:port
            parsed_vendor = parts[2], parts[3], parts[0], parts[1]
        if parsed_vendor is not None:
            host, port, username, password = parsed_vendor
            if _is_iprocket_host(host):
                return _iprocket_bridge_proxy(host, int(port), username, password)
            # Vendor port conventions: IPRocket 9595 and Kookeey gateways are
            # SOCKS5; IPRocket 5959 is HTTP. Resolve DNS through SOCKS as well.
            scheme = (
                "socks5h"
                if port == "9595" or "kookeey" in host.lower()
                else "http"
            )
            text = (
                scheme
                + "://"
                + quote(username, safe="")
                + ":"
                + quote(password, safe="")
                + "@"
                + host
                + ":"
                + port
            )
        elif separator == ":" and len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = parts
            scheme = "socks5h" if "kookeey" in host.lower() else "http"
            text = (
                scheme + "://" + quote(username, safe="") + ":"
                + quote(password, safe="") + "@" + host + ":" + port
            )
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlsplit(text)
    except Exception:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    host = parsed.hostname or ""
    if not host:
        return text
    if _is_iprocket_host(host) and parsed.username is not None:
        try:
            parsed_port = parsed.port or (9595 if parsed.scheme.lower().startswith("socks") else 5959)
        except ValueError as exc:
            raise ValueError("proxy contains an invalid port") from exc
        return _iprocket_bridge_proxy(
            host,
            parsed_port,
            unquote(parsed.username),
            unquote(parsed.password or ""),
            parsed.scheme if had_explicit_scheme else "",
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="%")
        if parsed.password is not None:
            auth += ":" + quote(unquote(parsed.password), safe="%")
        auth += "@"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError as exc:
        raise ValueError("proxy contains an invalid port") from exc
    netloc = auth + host + port
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def set_proxy_url(session: Any, proxy: str) -> None:
    normalized = normalize_proxy_url(proxy)
    session.proxies = {"http": normalized, "https": normalized} if normalized else {}


def stage_http_request(
    session: Any,
    stage: str,
    method: str,
    url: str,
    log: Any | None = None,
    **kwargs: Any,
) -> Any:
    started = time.perf_counter()
    emit_log(log, f"{stage}: {method.upper()} {compact_url(url)}")
    try:
        response = session.request(method.upper(), url, **kwargs)
    except Exception as exc:
        detail = safe_log_text(exc)
        emit_log(log, f"{stage}: request error={detail}")
        if is_network_exception(exc):
            raise NetworkError(stage, detail) from exc
        raise
    emit_log(
        log,
        f"{stage}: HTTP {response.status_code} elapsed={time.perf_counter() - started:.2f}s",
    )
    return response


def is_network_exception(exc: BaseException) -> bool:
    """Return whether an exception indicates a transport failure.

    HTTP errors are deliberately excluded: an HTTP response means the transport
    completed, even when the provider returned a 4xx or 5xx status.
    """
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    if requests is not None:
        request_exceptions = requests.exceptions
        transport_exceptions = (
            request_exceptions.ConnectionError,
            request_exceptions.Timeout,
            request_exceptions.ChunkedEncodingError,
        )
        if isinstance(exc, transport_exceptions):
            return True

    if CurlCffiRequestException is not None:
        if isinstance(exc, CurlCffiRequestException):
            if CurlCffiHTTPError is not None and isinstance(exc, CurlCffiHTTPError):
                return False
            return type(exc).__name__ in {
                "ConnectionError",
                "ConnectTimeout",
                "ProxyError",
                "ReadTimeout",
                "SSLError",
                "Timeout",
            }

    return False


def response_json(response: Any, stage: str) -> dict[str, Any]:
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise ProtocolError(502, f"{stage} invalid json: {safe_log_text(exc)}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(502, f"{stage} returned non-object json")
    return payload


class DefaultTransportFactory:
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any:
        device_id = str(uuid.uuid4())
        session = new_session()
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": f"{country_locale(config)},en;q=0.9",
                "Authorization": f"Bearer {config.access_token}",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Content-Type": "application/json",
                "oai-device-id": device_id,
                "oai-language": country_locale(config),
                "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "Cookie": f"oai-did={device_id}",
            }
        )
        set_proxy_url(session, proxy)
        return session

    def stripe(self, config: ExtractionConfig) -> Any:
        session = new_session()
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": f"{country_locale(config)},en;q=0.9",
            }
        )
        set_proxy_url(session, config.checkout_proxy)
        return session


def country_locale(config: ExtractionConfig) -> str:
    # Config is normalized before a transport is created. Keep this helper
    # dependency-free so fake factories can use the same interface.
    from .config import country_config

    return country_config(config.country)[2]
