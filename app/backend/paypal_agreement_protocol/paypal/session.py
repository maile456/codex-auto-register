import json
import os

import httpx
try:
    from curl_cffi.requests import Session as CurlSession
except Exception:
    CurlSession = None
from loguru import logger
from typing import Any, Optional
from paypal.models import SessionState
from config import USER_AGENT


def build_common_headers(locale: str = "pt_BR") -> dict:
    accept_language = {
        "pt_BR": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "en_GB": "en-GB,en;q=0.9,en-US;q=0.8",
        "en_US": "en-US,en;q=0.9",
        "ja_JP": "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.5",
        "th_TH": "th-TH,th;q=0.9,en-US;q=0.7,en;q=0.5",
        "id_ID": "id-ID,id;q=0.9,en-US;q=0.7,en;q=0.5",
        "en_AE": "en-AE,en;q=0.9,en-US;q=0.8",
    }.get(locale, "en-US,en;q=0.9")
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": accept_language,
        "sec-ch-ua": '"Chromium";v="136", "Not.A/Brand";v="24", "Google Chrome";v="136"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-device-memory": "8",
    }


def _mask_middle(value: str, left: int = 6, right: int = 4) -> str:
    if len(value) <= left + right:
        return "<redacted>"
    return f"{value[:left]}...{value[-right:]}"


def _mask_email(value: str) -> str:
    if "@" not in value:
        return "<redacted>"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1:]}@{domain}"


def _mask_digits(value: str, keep: int = 4) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) <= keep:
        return "<redacted>"
    return f"{'*' * (len(digits) - keep)}{digits[-keep:]}"


def sanitize_for_log(value: Any, key: str = "") -> Any:
    """Remove secrets and high-risk PII before writing diagnostics."""
    if isinstance(value, dict):
        return {k: sanitize_for_log(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_log(item, key) for item in value]
    if not isinstance(value, str):
        return value

    lowered_key = key.lower()
    compact_key = lowered_key.replace("_", "").replace("-", "")

    if compact_key in {"password", "securitycode", "cvv", "pin"}:
        return "<redacted>"
    if "authorization" in compact_key or "cookie" in compact_key:
        return "<redacted>"
    if "accesstoken" in compact_key or "euat" in compact_key:
        return "<redacted>"
    if compact_key in {"token", "batoken", "ectoken", "billingagreementid"}:
        return _mask_middle(value)
    if compact_key in {"cardnumber", "encryptednumber"}:
        return _mask_digits(value)
    if compact_key in {"cpf", "identitydocument", "document", "value"}:
        return "<redacted>"
    if compact_key == "email":
        return _mask_email(value)
    if compact_key in {"phonenumber", "phone", "number"} and sum(ch.isdigit() for ch in value) >= 8:
        return _mask_digits(value)

    return value


def _paypal_debug_id(headers: httpx.Headers) -> str:
    for name in ("paypal-debug-id", "Paypal-Debug-Id", "PayPal-Debug-Id"):
        value = headers.get(name)
        if value:
            return value
    return ""


class PayPalSession:
    """Manages HTTP session with cookie persistence and logging."""

    def __init__(
        self,
        state: SessionState,
        proxy_url: str | None = None,
        proxy_label: str = "",
        country: str = "BR",
        locale: str = "pt_BR",
    ):
        self.state = state
        self.proxy_url = proxy_url
        self.proxy_label = proxy_label or ("代理已开启" if proxy_url else "代理关闭")
        self.country = str(country or "BR").upper()
        self.locale = str(locale or "pt_BR")
        self.last_graphql_meta: dict[str, Any] = {}
        requested_engine = (os.getenv("PAYPAL_HTTP_ENGINE") or "curl_cffi").strip().lower()
        self.engine = "curl_cffi" if requested_engine != "httpx" and CurlSession is not None else "httpx"
        if self.engine == "curl_cffi":
            # Let curl_cffi generate the complete browser header set that
            # matches its TLS/HTTP2 fingerprint. Manually overriding UA or
            # sec-ch-* makes the visible headers disagree with the transport
            # fingerprint and is rejected by the current PayPal/DataDome edge.
            self.client = CurlSession(
                impersonate=(os.getenv("PAYPAL_CURL_IMPERSONATE") or "chrome").strip(),
            )
            self.client.headers.update({
                "Accept-Language": build_common_headers(self.locale)["Accept-Language"],
            })
            try:
                self.client.trust_env = False
            except Exception:
                pass
            if proxy_url:
                self.client.proxies = {"http": proxy_url, "https": proxy_url}
        else:
            client_kwargs = {
                "follow_redirects": False,
                "timeout": httpx.Timeout(30.0),
                "headers": build_common_headers(self.locale),
                "trust_env": False,
            }
            if proxy_url:
                client_kwargs["proxy"] = proxy_url
            self.client = httpx.Client(**client_kwargs)
        logger.info("HTTP transport engine: {}", self.engine)

    def close(self):
        self.client.close()

    def _sync_state_cookies(self):
        """Pull important cookies into SessionState after each request."""
        jar = self.client.cookies
        cookie_dict = {}
        # PayPal may set the same cookie name for multiple domain/path scopes
        # (ddgl is a common example). httpx.Cookies.items() raises
        # CookieConflict in that case, so iterate the underlying jar instead.
        for cookie in jar.jar:
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if name:
                cookie_dict[name] = value
        self.state.update_from_cookies(cookie_dict)

    def set_euat_token(self, token: str) -> None:
        """Atomically keep the PayPal EUAT header state and cookie jar aligned."""
        name = "AV894Kt2TSumQQrJwe-8mzmyREO"
        value = str(token or "")
        jar = self.client.cookies.jar
        for cookie in list(jar):
            if str(getattr(cookie, "name", "") or "") != name:
                continue
            try:
                jar.clear(
                    domain=str(getattr(cookie, "domain", "") or ""),
                    path=str(getattr(cookie, "path", "") or "/"),
                    name=name,
                )
            except Exception:
                pass
        self.state.euat_token = value
        if value:
            self.client.cookies.set(name, value, domain=".paypal.com", path="/")

    def _request_kwargs(self, kwargs: dict) -> dict:
        values = dict(kwargs)
        if self.engine == "curl_cffi":
            if "follow_redirects" in values:
                values["allow_redirects"] = values.pop("follow_redirects")
            # httpx names a raw request body `content`; curl_cffi follows the
            # requests API and calls the same argument `data`.
            if "content" in values and "data" not in values:
                values["data"] = values.pop("content")
            values.setdefault("allow_redirects", False)
            values.setdefault("timeout", 30)
        return values

    def get(self, url: str, **kwargs):
        logger.debug(f"GET {url}")
        resp = self.client.get(url, **self._request_kwargs(kwargs))
        self._sync_state_cookies()
        logger.debug(f"  -> {resp.status_code} ({len(resp.content)} bytes)")
        return resp

    def post(self, url: str, **kwargs):
        logger.debug(f"POST {url}")
        resp = self.client.post(url, **self._request_kwargs(kwargs))
        self._sync_state_cookies()
        logger.debug(f"  -> {resp.status_code} ({len(resp.content)} bytes)")
        return resp

    def diagnostic_snapshot(self) -> dict[str, Any]:
        cookie_names = []
        try:
            cookie_names = sorted({
                str(getattr(cookie, "name", "") or "")
                for cookie in self.client.cookies.jar
                if getattr(cookie, "name", "")
            })
        except Exception:
            pass
        return {
            "engine": self.engine,
            "cookie_names": cookie_names,
            "last_graphql": dict(self.last_graphql_meta),
        }

    def graphql(self, operation_name: str, query: str, variables: dict,
                extra_headers: Optional[dict] = None,
                extra_body: Optional[dict] = None,
                batched: bool = False,
                endpoint: Optional[str] = None) -> dict:
        """Send a GraphQL request to PayPal's graphql endpoint."""
        url = endpoint or "https://www.paypal.com/graphql"
        if operation_name and endpoint is None:
            url = f"{url}?{operation_name}"

        context_token = str(
            variables.get("token")
            or variables.get("billingAgreementId")
            or self.state.ec_token
            or self.state.ba_token
        )
        referer = (
            self.state.signup_url
            if self.state.ec_token
            else f"https://www.paypal.com/pay?token={self.state.ba_token}&ul=1"
        )
        app_name = "checkoutuinodeweb" if operation_name == "authorize" else "checkoutuinodeweb_weasley"
        headers = {
            "Content-Type": "application/json",
            "X-App-Name": app_name,
            "X-Requested-With": "fetch",
            "PayPal-Client-Context": context_token,
            "PayPal-Client-Metadata-Id": context_token,
            "X-Country": self.country,
            "X-Locale": self.locale,
            "Origin": "https://www.paypal.com",
            "Referer": referer,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if self.state.euat_token:
            headers["X-PayPal-Internal-EUAT"] = self.state.euat_token
        if extra_headers:
            # Passing None removes a default header. This is needed for the
            # browser-captured final Hagrid authorize call, which posts to
            # /graphql/ without PayPal-Client-Context/X-Country/X-Locale.
            for key, value in extra_headers.items():
                if value is None:
                    headers.pop(key, None)
                else:
                    headers[key] = value

        payload_item = {
            "operationName": operation_name,
            "variables": variables,
            "query": query,
        }
        if extra_body:
            # checkoutweb/weasley injects fn_sync_data at the top level of the
            # GraphQL JSON body for SignUpNewMemberMutation.
            payload_item.update(extra_body)

        payload = [payload_item] if batched else payload_item

        resp = self.post(url, json=payload, headers=headers)
        debug_id = _paypal_debug_id(resp.headers)
        self.last_graphql_meta = {
            "operation": operation_name,
            "status": resp.status_code,
            "paypal_debug_id": debug_id or "",
        }
        logger.info(
            "GraphQL {} HTTP {} bytes={} paypal_debug_id={}",
            operation_name,
            resp.status_code,
            len(resp.content),
            debug_id or "<missing>",
        )

        try:
            result = resp.json()
        except ValueError as first_parse_error:
            body = str(resp.text or "")
            body_lower = body.lower()
            content_type = str(resp.headers.get("content-type") or "")
            challenge_response = any(marker in body_lower for marker in (
                "authchallengenodeweb",
                "captcha",
                "challenge",
                "<!doctype html",
                "<html",
            ))
            empty_response = not body.strip()
            if challenge_response or empty_response:
                response_kind = "auth_challenge_html" if challenge_response else "empty_response"
                logger.warning(
                    "GraphQL {} returned {} instead of JSON; warming the same session and retrying once "
                    "(status={} paypal_debug_id={} bytes={})",
                    operation_name,
                    response_kind,
                    resp.status_code,
                    debug_id or "<missing>",
                    len(resp.content),
                )
                try:
                    self.get(
                        referer,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Referer": "https://www.paypal.com/",
                        },
                    )
                except Exception as warmup_error:
                    logger.info(
                        "GraphQL {} session warmup skipped: {}",
                        operation_name,
                        type(warmup_error).__name__,
                    )
                resp = self.post(url, json=payload, headers=headers)
                debug_id = _paypal_debug_id(resp.headers)
                self.last_graphql_meta = {
                    "operation": operation_name,
                    "status": resp.status_code,
                    "paypal_debug_id": debug_id or "",
                    "non_json_retry": True,
                }
                logger.info(
                    "GraphQL {} retry HTTP {} bytes={} paypal_debug_id={}",
                    operation_name,
                    resp.status_code,
                    len(resp.content),
                    debug_id or "<missing>",
                )
                try:
                    result = resp.json()
                except ValueError as retry_parse_error:
                    retry_body = str(resp.text or "")
                    retry_lower = retry_body.lower()
                    retry_kind = (
                        "AUTH_CHALLENGE"
                        if any(marker in retry_lower for marker in (
                            "authchallengenodeweb", "captcha", "challenge", "<!doctype html", "<html"
                        ))
                        else ("EMPTY_RESPONSE" if not retry_body.strip() else "NON_JSON")
                    )
                    raise RuntimeError(
                        f"PAYPAL_GRAPHQL_{retry_kind}: operation={operation_name}; "
                        f"HTTP {resp.status_code}; paypal_debug_id={debug_id or 'missing'}; "
                        "the PayPal edge response is retryable with a clean task/session"
                    ) from retry_parse_error
            else:
                raise RuntimeError(
                    f"PAYPAL_GRAPHQL_NON_JSON: operation={operation_name}; HTTP {resp.status_code}; "
                    f"content_type={content_type or 'unknown'}; bytes={len(resp.content)}; "
                    f"paypal_debug_id={debug_id or 'missing'}"
                ) from first_parse_error

        result_items = result if isinstance(result, list) else [result]
        for item in result_items:
            if not isinstance(item, dict) or not item.get("errors"):
                continue

            errors = item.get("errors") or []
            partial_access = any(
                isinstance(err, dict)
                and isinstance(err.get("errorData"), dict)
                and bool(err.get("errorData", {}).get("accessToken"))
                for err in errors
            )
            log_method = logger.warning if partial_access else logger.error
            log_method(
                "GraphQL {} returned {}: status={} paypal_debug_id={} errors={}",
                operation_name,
                "a recoverable partial result" if partial_access else "errors",
                resp.status_code,
                debug_id or "<missing>",
                json.dumps(sanitize_for_log(errors), ensure_ascii=False, indent=2),
            )
            logger.debug(
                "GraphQL {} sanitized variables: {}",
                operation_name,
                json.dumps(
                    sanitize_for_log(variables),
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        return result
