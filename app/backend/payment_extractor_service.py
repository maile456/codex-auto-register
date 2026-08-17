from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import threading
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest, urlopen

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .chatgpt_plan import normalize_access_token as normalize_bearer_token
from .oai_payment_extractor.auth import normalize_access_token as normalize_session_json
from .oai_payment_extractor.config import (
    COUNTRY_PROFILES,
    SUPPORTED_COUNTRIES,
    country_config,
    normalize_payment_method,
)
from .oai_payment_extractor.errors import ConfigurationError
from .oai_payment_extractor.logging_utils import configure_logging
from .oai_payment_extractor.models import ExtractionConfig
from .oai_payment_extractor.web.proxy_probe import ProxyProbeError, probe_proxy
from .oai_payment_extractor.web.tasks import (
    TERMINAL_STATES,
    TaskManager,
    TaskNotFoundError,
    TaskStateError,
)
from .oai_iprocket_chain_bridge import (
    ensure_background_server,
    stop_background_server,
)


MAX_TOKEN_CHARS = 16_384
MAX_PROXY_POOL_CHARS = 200_000
MAX_HCAPTCHA_CHARS = 16_384
DEFAULT_TASK_LIMIT = 10_000
VENDOR_PROXY_MARKERS = ("iprocket.", "iproyal.", "1024proxy.")
COUNTRY_NAMES = {
    "GB": "英国",
    "US": "美国",
    "BR": "巴西",
    "DE": "德国",
    "TH": "泰国",
    "BA": "波黑",
    "PH": "菲律宾",
    "ID": "印度尼西亚",
    "NL": "荷兰",
    "AE": "阿联酋",
    "DK": "丹麦",
    "JP": "日本",
    "ES": "西班牙",
    "FI": "芬兰",
    "FR": "法国",
}
PAYMENT_METHOD_LABELS = {
    "paypal": "PayPal",
    "gopay": "GoPay",
    "gcash": "GCash",
}


class ExtractorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PaymentExtractorTaskCreate(ExtractorModel):
    accessToken: str = Field(
        default="",
        max_length=MAX_TOKEN_CHARS,
        validation_alias=AliasChoices("accessToken", "access_token", "token"),
    )
    checkoutProxy: str = Field(
        default="",
        max_length=MAX_PROXY_POOL_CHARS,
        validation_alias=AliasChoices("checkoutProxy", "checkout_proxy"),
    )
    updateProxy: str = Field(
        default="",
        max_length=MAX_PROXY_POOL_CHARS,
        validation_alias=AliasChoices("updateProxy", "update_proxy"),
    )
    stripeHcaptchaToken: str = Field(
        default="",
        max_length=MAX_HCAPTCHA_CHARS,
        validation_alias=AliasChoices(
            "stripeHcaptchaToken", "stripe_hcaptcha_token"
        ),
    )
    country: str | None = Field(default=None, min_length=2, max_length=2)
    paymentMethod: Literal["paypal", "gopay", "gcash"] | None = Field(
        default=None,
        validation_alias=AliasChoices("paymentMethod", "payment_method"),
    )
    applyCheckoutUpdate: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "applyCheckoutUpdate", "apply_checkout_update"
        ),
    )
    checkoutMode: Literal["auto", "oaics_only"] = Field(
        default="auto",
        validation_alias=AliasChoices("checkoutMode", "checkout_mode"),
    )
    rotateCheckoutProxy: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "rotateCheckoutProxy", "rotate_checkout_proxy"
        ),
    )
    rotateUpdateProxy: bool = Field(
        default=False,
        validation_alias=AliasChoices("rotateUpdateProxy", "rotate_update_proxy"),
    )

    @field_validator("accessToken")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        return normalize_bearer_token(normalize_session_json(text))

    @field_validator("checkoutProxy", "updateProxy", "stripeHcaptchaToken")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        country = value.strip().upper()
        country_config(country)
        return country


class PaymentExtractorTaskRetry(ExtractorModel):
    checkoutProxy: str | None = Field(
        default=None,
        max_length=MAX_PROXY_POOL_CHARS,
        validation_alias=AliasChoices("checkoutProxy", "checkout_proxy"),
    )
    updateProxy: str | None = Field(
        default=None,
        max_length=MAX_PROXY_POOL_CHARS,
        validation_alias=AliasChoices("updateProxy", "update_proxy"),
    )
    rotateCheckoutProxy: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "rotateCheckoutProxy", "rotate_checkout_proxy"
        ),
    )
    rotateUpdateProxy: bool = Field(
        default=False,
        validation_alias=AliasChoices("rotateUpdateProxy", "rotate_update_proxy"),
    )

    @field_validator("checkoutProxy", "updateProxy")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else None


class PaymentExtractorProxyTest(ExtractorModel):
    checkoutProxy: str = Field(
        min_length=1,
        max_length=MAX_PROXY_POOL_CHARS,
        validation_alias=AliasChoices("checkoutProxy", "checkout_proxy"),
    )

    @field_validator("checkoutProxy")
    @classmethod
    def trim_proxy(cls, value: str) -> str:
        return value.strip()


class PaymentExtractorProxySource(ExtractorModel):
    url: str = Field(default="", max_length=4096)

    @field_validator("url")
    @classmethod
    def trim_url(cls, value: str) -> str:
        return value.strip()


class PaymentExtractorConcurrencyUpdate(ExtractorModel):
    concurrency: int = Field(ge=1, le=10)


class PaymentExtractorBulkDelete(ExtractorModel):
    target: Literal["failed", "succeeded"]


class PaymentExtractorServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _camel_key(value: str) -> str:
    return re.sub(r"_([a-z])", lambda match: match.group(1).upper(), value)


def _camelize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_camel_key(str(key)): _camelize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_camelize(item) for item in value]
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _configured_proxy_pool() -> str:
    file_name = os.getenv("OPLL_PROXY_POOL_FILE", "").strip()
    if not file_name:
        return ""
    try:
        content = Path(file_name).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    return "\n".join(_proxy_lines(content))


def _normalize_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return normalize_bearer_token(normalize_session_json(text))


def _proxy_country(value: str) -> str:
    match = re.search(
        r"-(?:res|country|region|area|dc|res_sc)-([A-Za-z]{2})(?:[-_:]|$)",
        str(value or ""),
    )
    return match.group(1).upper() if match else ""


def _proxy_lines(value: str) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _session_token(length: int) -> str:
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(1, length)))


def _rotate_proxy_session(value: str) -> str:
    text = str(value or "")
    match = re.match(r"^([a-z][a-z\d+.-]*://)([^/@]+)(@.*)$", text, re.I)
    if not match:
        return text
    user_info = match.group(2)
    sid_match = re.search(r"(^|-)sid-([A-Za-z0-9]+)(?=-|:)", user_info, re.I)
    if sid_match:
        replacement = sid_match.group(0)[: -len(sid_match.group(2))] + _session_token(
            len(sid_match.group(2))
        )
        user_info = user_info[: sid_match.start()] + replacement + user_info[sid_match.end() :]
        return f"{match.group(1)}{user_info}{match.group(3)}"
    number_match = re.search(r"-(\d+)$", user_info)
    if not number_match:
        return text
    digits = len(number_match.group(1))
    replacement = str(__import__("secrets").randbelow(9 * (10 ** (digits - 1))) + 10 ** (digits - 1))
    return f"{match.group(1)}{user_info[: number_match.start(1)]}{replacement}{match.group(3)}"


def _proxy_preview(value: str) -> str:
    line = next(iter(_proxy_lines(value)), "")
    if not line:
        return ""
    try:
        candidate = line if "://" in line else f"http://{line}"
        parsed = urlsplit(candidate)
        if parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            scheme = parsed.scheme or "http"
            auth = "***@" if parsed.username is not None or "@" in parsed.netloc else ""
            return f"{scheme}://{auth}{parsed.hostname}{port}"
    except (TypeError, ValueError):
        pass
    parts = line.split(":", 3)
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}:***"
    return "configured"


def _uses_vendor_bridge(value: str) -> bool:
    lowered = str(value or "").casefold()
    return any(marker in lowered for marker in VENDOR_PROXY_MARKERS)


_EVENT_SECRET_KEY_PARTS = (
    "access_token",
    "accesstoken",
    "token",
    "hcaptcha",
    "password",
    "passwd",
    "secret",
    "apikey",
    "cookie",
    "authorization",
    "credential",
)


def _sanitize_event_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "***", text)
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://[^\s/:]+:)[^@\s]+@",
        r"\1***@",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:token|ba_token|auth|key|password|secret|session)=)[^&#\s]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)\b((?:access[_-]?token|token|password|passwd|secret|hcaptcha|"
        r"api[_-]?key)\s*[:=]\s*)[^\s,;]+",
        r"\1***",
        text,
    )
    return text[:1200]


def _sanitize_event_value(key: str, value: Any) -> Any:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    if normalized == "result":
        if not isinstance(value, dict):
            return {}
        # Result URLs and IDs are deliberately left to GET /api/tasks.  The
        # event stream only needs non-sensitive progress/result metadata.
        allowed = {
            "ok",
            "session_kind",
            "payment_method",
            "billing_country",
            "currency",
            "amount_due",
            "amount_due_minor",
        }
        return {
            item_key: _sanitize_event_value(item_key, item_value)
            for item_key, item_value in value.items()
            if item_key.casefold() in allowed
        }
    if any(part in normalized for part in _EVENT_SECRET_KEY_PARTS):
        return None
    if "proxy" in normalized:
        if isinstance(value, str):
            return _proxy_preview(value)
        if isinstance(value, list):
            return [_proxy_preview(str(item)) for item in value]
        return None
    if isinstance(value, dict):
        return {
            item_key: sanitized
            for item_key, item_value in value.items()
            if (sanitized := _sanitize_event_value(str(item_key), item_value)) is not None
        }
    if isinstance(value, list):
        return [_sanitize_event_value(key, item) for item in value]
    if isinstance(value, str):
        return _sanitize_event_text(value)
    return value


def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_event_value("event", event)
    return sanitized if isinstance(sanitized, dict) else {}


class PaymentExtractorService:
    def __init__(
        self,
        *,
        manager: TaskManager | None = None,
        max_workers: int | None = None,
        ttl_seconds: int | None = None,
        history_size: int | None = None,
        task_limit: int | None = None,
    ) -> None:
        configured_pool = _configured_proxy_pool()
        configured_country = os.getenv("OPLL_COUNTRY", "DE").strip().upper() or "DE"
        forced_country = os.getenv("OPLL_FORCE_COUNTRY", "").strip().upper()
        country_config(forced_country or configured_country)
        configured_method = normalize_payment_method(
            os.getenv("OPLL_PAYMENT_METHOD", "paypal").strip().lower() or "paypal"
        )

        self.default_access_token = _normalize_token(os.getenv("OPLL_AT", ""))
        self.default_country = configured_country
        self.force_country = forced_country
        self.default_payment_method = configured_method
        self.sticky_task_proxy = _env_bool("OPLL_STICKY_TASK_PROXY", False)
        self.default_apply_checkout_update = _env_bool(
            "OPLL_UPDATE_CHECKOUT", True
        )
        self.default_checkout_proxy = configured_pool or os.getenv(
            "OPLL_CHECKOUT_PROXY", ""
        ).strip()
        self.default_update_proxy = configured_pool or os.getenv(
            "OPLL_UPDATE_PROXY", ""
        ).strip()
        self.default_stripe_hcaptcha_token = os.getenv(
            "OPLL_STRIPE_HCAPTCHA_TOKEN", ""
        ).strip()
        self.proxy_source_url = os.getenv("OPLL_PROXY_SOURCE_URL", "").strip()
        self.proxy_pool_id = (
            hashlib.sha256(configured_pool.encode("utf-8")).hexdigest()[:16]
            if configured_pool
            else ""
        )
        configure_logging(
            level=os.getenv("OPLL_LOG_LEVEL", "INFO"),
            log_file=os.getenv("OPLL_LOG_FILE", "").strip(),
            serialize=_env_bool("OPLL_LOG_JSON", False),
        )
        self.default_concurrency = max(
            1, min(10, max_workers or _env_int("OPLL_TASK_WORKERS", 4))
        )
        self.manager = manager or TaskManager(
            max_workers=10,
            concurrency=self.default_concurrency,
            ttl_seconds=ttl_seconds
            or _env_int("OPLL_TASK_TTL_SECONDS", 3600),
            history_size=history_size
            or _env_int("OPLL_TASK_EVENT_HISTORY_SIZE", 500),
        )
        self.task_limit = max(
            1,
            task_limit
            if task_limit is not None
            else _env_int("OPLL_TASK_LIMIT", DEFAULT_TASK_LIMIT),
        )
        self._lock = threading.RLock()
        self._checkout_cursor = 0
        self._update_cursor = 0
        self._bridge_started = False

    def options(self) -> dict[str, Any]:
        countries = []
        for code in SUPPORTED_COUNTRIES:
            profile = COUNTRY_PROFILES[code]
            countries.append(
                {
                    "value": code,
                    "label": f"{COUNTRY_NAMES.get(code, code)} · {profile['currency']}",
                    "code": code,
                    "currency": profile["currency"],
                    "locale": profile["locale"],
                    "timezone": profile["timezone"],
                }
            )
        return {
            "ok": True,
            "countries": countries,
            "paymentMethods": [
                {"value": key, "label": label}
                for key, label in PAYMENT_METHOD_LABELS.items()
            ],
            "country": self.force_country or self.default_country,
            "forceCountry": self.force_country,
            "paymentMethod": self.default_payment_method,
            "stickyTaskProxy": self.sticky_task_proxy,
            "checkoutProxy": self.default_checkout_proxy,
            "updateProxy": self.default_update_proxy,
            "proxyPoolId": self.proxy_pool_id,
            "proxySourceUrl": self.proxy_source_url,
            "applyCheckoutUpdate": self.default_apply_checkout_update,
            "checkoutModes": ["auto", "oaics_only"],
            "taskLimit": self.task_limit,
            "concurrency": int(
                getattr(self.manager, "concurrency", self.default_concurrency)
            ),
            "maxConcurrency": int(getattr(self.manager, "max_concurrency", 10)),
        }

    def set_concurrency(
        self, payload: PaymentExtractorConcurrencyUpdate
    ) -> dict[str, int]:
        setter = getattr(self.manager, "set_concurrency", None)
        if not callable(setter):
            raise PaymentExtractorServiceError(
                "concurrency_not_supported",
                "当前任务管理器不支持动态并发",
                http_status=409,
            )
        return {
            "concurrency": int(setter(payload.concurrency)),
            "maxConcurrency": int(getattr(self.manager, "max_concurrency", 10)),
        }

    def _pick_proxy(self, pool: str, *, update: bool, rotate: bool) -> str:
        lines = _proxy_lines(pool)
        if not lines:
            return ""
        with self._lock:
            if update:
                selected = lines[self._update_cursor % len(lines)]
                self._update_cursor += 1
            else:
                selected = lines[self._checkout_cursor % len(lines)]
                self._checkout_cursor += 1
        return _rotate_proxy_session(selected) if rotate else selected

    def _ensure_bridge_for(self, *proxies: str) -> None:
        if not any(_uses_vendor_bridge(proxy) for proxy in proxies):
            return
        started = ensure_background_server()
        self._bridge_started = self._bridge_started or started

    def create(self, payload: PaymentExtractorTaskCreate) -> dict[str, Any]:
        access_token = payload.accessToken or self.default_access_token
        checkout_proxy = self._pick_proxy(
            payload.checkoutProxy or self.default_checkout_proxy,
            update=False,
            rotate=payload.rotateCheckoutProxy,
        )
        update_proxy = self._pick_proxy(
            payload.updateProxy or self.default_update_proxy,
            update=True,
            rotate=payload.rotateUpdateProxy,
        )
        if self.sticky_task_proxy and checkout_proxy:
            # Keep Checkout Update and Stripe confirmation on one proxy
            # session for this task.  The frontend may provide a separate
            # update pool, but cross-session egress changes raise payment risk.
            update_proxy = checkout_proxy
        country = self.force_country or payload.country or self.default_country
        # A caller-supplied billing country is authoritative.  Proxy usernames
        # may contain an egress hint such as ``region-BR``, but that describes
        # the network route and must not rewrite the billing profile.
        if not self.force_country and payload.country is None:
            country = _proxy_country(checkout_proxy) or country
        payment_method = payload.paymentMethod or self.default_payment_method
        apply_checkout_update = (
            payload.applyCheckoutUpdate
            if payload.applyCheckoutUpdate is not None
            else self.default_apply_checkout_update
        )
        stripe_hcaptcha_token = (
            payload.stripeHcaptchaToken or self.default_stripe_hcaptcha_token
        )
        if not access_token:
            raise PaymentExtractorServiceError(
                "access_token_required", "AT 不能为空", http_status=422
            )
        if not checkout_proxy:
            raise PaymentExtractorServiceError(
                "checkout_proxy_required", "Checkout 代理不能为空", http_status=422
            )
        if apply_checkout_update and not update_proxy:
            raise PaymentExtractorServiceError(
                "update_proxy_required", "启用 Checkout Update 时必须填写 Update 代理", http_status=422
            )
        self._ensure_bridge_for(checkout_proxy, update_proxy)
        try:
            method = normalize_payment_method(payment_method)
            config = ExtractionConfig(
                access_token=_normalize_token(access_token),
                checkout_proxy=checkout_proxy,
                update_proxy=update_proxy,
                stripe_hcaptcha_token=stripe_hcaptcha_token,
                country=country,
                payment_method=method,
                apply_checkout_update=apply_checkout_update,
                verbose=False,
                oaics_only=payload.checkoutMode == "oaics_only",
            )
        except (ConfigurationError, ValueError) as exc:
            raise PaymentExtractorServiceError(
                "extractor_configuration_invalid", str(exc), http_status=422
            ) from exc
        with self._lock:
            active_count = sum(
                1
                for task in self.manager.list()
                if str(task.get("status") or "") not in TERMINAL_STATES
            )
            if active_count >= self.task_limit:
                raise PaymentExtractorServiceError(
                    "extractor_queue_full", "提链任务队列已满", http_status=409
                )
            return self._public_snapshot(self.manager.create(config))

    def list(self) -> list[dict[str, Any]]:
        return [self._public_snapshot(item) for item in self.manager.list()]

    def get(self, task_id: str) -> dict[str, Any]:
        snapshot = self.manager.get(task_id)
        if snapshot is None:
            raise PaymentExtractorServiceError(
                "extractor_task_not_found", "提链任务不存在", http_status=404
            )
        return self._public_snapshot(snapshot)

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._task_action(lambda: self.manager.cancel(task_id))

    def retry(
        self, task_id: str, payload: PaymentExtractorTaskRetry
    ) -> dict[str, Any]:
        checkout_proxy = None
        update_proxy = None
        if payload.checkoutProxy is not None:
            checkout_proxy = self._pick_proxy(
                payload.checkoutProxy,
                update=False,
                rotate=payload.rotateCheckoutProxy,
            )
        if payload.updateProxy is not None:
            update_proxy = self._pick_proxy(
                payload.updateProxy,
                update=True,
                rotate=payload.rotateUpdateProxy,
            )
        if self.sticky_task_proxy and checkout_proxy:
            update_proxy = checkout_proxy
        self._ensure_bridge_for(checkout_proxy or "", update_proxy or "")
        return self._task_action(
            lambda: self.manager.retry(
                task_id,
                checkout_proxy=checkout_proxy,
                update_proxy=update_proxy,
            )
        )

    def resolve_paypal(self, task_id: str) -> dict[str, Any]:
        return self._task_action(lambda: self.manager.resolve_paypal(task_id))

    def delete(self, task_id: str) -> dict[str, Any]:
        return self._task_action(lambda: self.manager.delete(task_id))

    def bulk_delete(self, payload: PaymentExtractorBulkDelete) -> dict[str, Any]:
        statuses = (
            {"failed", "cancelled"}
            if payload.target == "failed"
            else {"succeeded"}
        )
        return _camelize(self.manager.delete_by_statuses(statuses))

    def proxy_test(self, payload: PaymentExtractorProxyTest) -> dict[str, Any]:
        proxy = self._pick_proxy(payload.checkoutProxy, update=False, rotate=False)
        self._ensure_bridge_for(proxy)
        try:
            result = probe_proxy(proxy)
        except ProxyProbeError as exc:
            raise PaymentExtractorServiceError(
                "proxy_probe_failed", str(exc), http_status=exc.status_code
            ) from exc
        return {"ok": True, **_camelize(result.to_dict())}

    def proxy_source(self, payload: PaymentExtractorProxySource) -> dict[str, Any]:
        source_url = payload.url or self.proxy_source_url
        parsed = urlsplit(source_url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() != "app.iprocket.io"
        ):
            raise PaymentExtractorServiceError(
                "proxy_source_invalid",
                "仅支持 IPRocket HTTPS 代理订阅链接",
                http_status=422,
            )
        try:
            request = UrlRequest(source_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=15) as response:
                body = response.read(1024 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:
            raise PaymentExtractorServiceError(
                "proxy_source_failed", "IPRocket 代理订阅读取失败", http_status=502
            ) from exc
        proxies = _proxy_lines(body)
        if not proxies:
            raise PaymentExtractorServiceError(
                "proxy_source_empty", "IPRocket 代理订阅没有返回代理", http_status=502
            )
        return {
            "ok": True,
            "proxies": proxies,
            "count": len(proxies),
            "uniqueCount": len(set(proxies)),
        }

    def subscribe(self) -> tuple[list[dict[str, Any]], Any]:
        history, subscriber = self.manager.subscribe()
        return [self.public_event(event) for event in history], subscriber

    def unsubscribe(self, subscriber: Any) -> None:
        self.manager.unsubscribe(subscriber)

    @staticmethod
    def public_event(event: dict[str, Any]) -> dict[str, Any]:
        """Return a progress event without task credentials or full proxies."""
        return _sanitize_event(event)

    def close(self) -> None:
        for snapshot in self.manager.list():
            if str(snapshot.get("status") or "") not in TERMINAL_STATES:
                try:
                    self.manager.cancel(str(snapshot.get("task_id") or ""))
                except (TaskNotFoundError, TaskStateError):
                    pass
        self.manager.close(wait=False)
        if self._bridge_started:
            stop_background_server()

    @staticmethod
    def _task_action(action: Any) -> dict[str, Any]:
        try:
            return PaymentExtractorService._public_snapshot(action())
        except TaskNotFoundError as exc:
            raise PaymentExtractorServiceError(
                "extractor_task_not_found", "提链任务不存在", http_status=404
            ) from exc
        except TaskStateError as exc:
            raise PaymentExtractorServiceError(
                "extractor_task_state_conflict", str(exc), http_status=409
            ) from exc

    @staticmethod
    def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        source = dict(snapshot)
        configured_proxy = str(source.pop("checkout_proxy", "") or "")
        public = _camelize(source)
        if configured_proxy:
            public["checkoutProxyConfigured"] = True
            public["checkoutProxyPreview"] = _proxy_preview(configured_proxy)
        else:
            public["checkoutProxyConfigured"] = False
            public["checkoutProxyPreview"] = ""
        return public
