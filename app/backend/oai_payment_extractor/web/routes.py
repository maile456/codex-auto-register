from __future__ import annotations

import os
import re
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request

from ..config import SUPPORTED_COUNTRIES, country_config, normalize_payment_method
from ..errors import ConfigurationError
from ..models import ExtractionConfig
from .proxy_probe import ProxyProbeError, probe_proxy
from .tasks import TaskManager, TaskNotFoundError, TaskStateError


def register_routes(app: Flask, manager: TaskManager) -> None:
    @app.get("/api/health")
    def health() -> Any:
        return jsonify({"ok": True, "service": "payment-link-extractor"})

    @app.get("/api/defaults")
    def defaults() -> Any:
        proxy_pool = _configured_proxy_pool()
        forced_country = os.getenv("OPLL_FORCE_COUNTRY", "").strip().upper()
        return jsonify(
            {
                "ok": True,
                "country": forced_country or os.getenv("OPLL_COUNTRY", "DE"),
                "force_country": forced_country,
                "payment_method": "paypal",
                "checkout_proxy": proxy_pool or os.getenv("OPLL_CHECKOUT_PROXY", ""),
                "update_proxy": proxy_pool or os.getenv("OPLL_UPDATE_PROXY", ""),
                "proxy_pool_id": hashlib.sha256(proxy_pool.encode("utf-8")).hexdigest()[:16] if proxy_pool else "",
                "proxy_source_url": os.getenv("OPLL_PROXY_SOURCE_URL", ""),
                "apply_checkout_update": _env_bool("OPLL_UPDATE_CHECKOUT", True),
            }
        )

    @app.get("/api/proxy/source")
    def proxy_source() -> Any:
        source_url = str(request.args.get("url") or os.getenv("OPLL_PROXY_SOURCE_URL", "")).strip()
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.hostname not in {"app.iprocket.io"}:
            return _error("仅支持 IPRocket HTTPS 代理订阅链接", 400)
        try:
            req = Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as response:
                body = response.read(1024 * 1024).decode("utf-8", errors="replace")
        except Exception:
            return _error("IPRocket 代理订阅读取失败", 502)
        proxies = [line.strip() for line in body.splitlines() if line.strip()]
        if not proxies:
            return _error("IPRocket 代理订阅没有返回代理", 502)
        return jsonify({"ok": True, "proxies": proxies, "count": len(proxies), "unique_count": len(set(proxies))})

    @app.get("/api/tasks")
    def list_tasks() -> Any:
        return jsonify({"ok": True, "tasks": manager.list()})

    @app.post("/api/tasks")
    def create_task() -> Any:
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        try:
            config = _config_from_payload(payload)
            snapshot = manager.create(config)
        except (ConfigurationError, ValueError) as exc:
            return _error(str(exc), 400)
        task_id = snapshot["task_id"]
        snapshot.update(
            {
                "status_url": f"/api/tasks/{task_id}",
                "websocket_url": "/ws/tasks",
            }
        )
        return jsonify(snapshot), 202

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str) -> Any:
        snapshot = manager.get(task_id)
        if snapshot is None:
            return _error("task not found", 404)
        return jsonify(snapshot)

    @app.post("/api/proxy/test")
    def test_proxy() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        checkout_proxy = payload.get("checkout_proxy")
        if not isinstance(checkout_proxy, str):
            return _error("checkout_proxy must be a string", 400)
        try:
            location = probe_proxy(checkout_proxy)
        except ProxyProbeError as exc:
            return _error(str(exc), exc.status_code)
        return jsonify({"ok": True, **location.to_dict()})

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_task(task_id: str) -> Any:
        try:
            return jsonify(manager.cancel(task_id))
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)

    @app.post("/api/tasks/<task_id>/retry")
    def retry_task(task_id: str) -> Any:
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        checkout_proxy = payload.get("checkout_proxy")
        if checkout_proxy is not None and not isinstance(checkout_proxy, str):
            return _error("checkout_proxy must be a string", 400)
        update_proxy = payload.get("update_proxy")
        if update_proxy is not None and not isinstance(update_proxy, str):
            return _error("update_proxy must be a string", 400)
        try:
            snapshot = manager.retry(
                task_id,
                checkout_proxy=checkout_proxy,
                update_proxy=update_proxy,
            )
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)
        new_task_id = snapshot["task_id"]
        snapshot.update(
            {
                "status_url": f"/api/tasks/{new_task_id}",
                "websocket_url": "/ws/tasks",
            }
        )
        return jsonify(snapshot), 202

    @app.post("/api/tasks/<task_id>/resolve-paypal")
    def resolve_paypal_task(task_id: str) -> Any:
        try:
            return jsonify(manager.resolve_paypal(task_id))
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)

    @app.delete("/api/tasks/<task_id>")
    def delete_task(task_id: str) -> Any:
        try:
            return jsonify(manager.delete(task_id))
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)

    @app.post("/api/tasks/bulk-delete")
    def bulk_delete_tasks() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        target = payload.get("target")
        status_groups = {
            "failed": {"failed", "cancelled"},
            "succeeded": {"succeeded"},
        }
        if not isinstance(target, str) or target not in status_groups:
            return _error("target must be failed or succeeded", 400)
        statuses = status_groups[target]
        return jsonify(manager.delete_by_statuses(statuses))


def _config_from_payload(payload: dict[str, Any]) -> ExtractionConfig:
    access_token = _credential_value(payload) or os.getenv("OPLL_AT", "")
    pool_lines = _configured_proxy_pool().splitlines()
    pool_first = pool_lines[0] if pool_lines else ""
    checkout_proxy = payload.get("checkout_proxy") or pool_first or os.getenv("OPLL_CHECKOUT_PROXY", "")
    update_proxy = payload.get("update_proxy") or pool_first or os.getenv("OPLL_UPDATE_PROXY", "")
    if _env_bool("OPLL_STICKY_TASK_PROXY", False) and checkout_proxy:
        update_proxy = checkout_proxy
    hcaptcha = _value(payload, "stripe_hcaptcha_token", "OPLL_STRIPE_HCAPTCHA_TOKEN")
    forced_country = os.getenv("OPLL_FORCE_COUNTRY", "").strip().upper()
    payload_country = str(payload.get("country") or "").strip().upper()
    configured_country = os.getenv("OPLL_COUNTRY", "").strip().upper()
    country = forced_country or payload_country or configured_country or "DE"
    proxy_country = re.search(
        r"-(?:res|country|region|area|dc|res_sc)-([A-Za-z]{2})(?:[-_:]|$)",
        str(checkout_proxy or ""),
    )
    if proxy_country and not forced_country and not payload_country and not configured_country:
        country = proxy_country.group(1).upper()
    payment_method = str(payload.get("payment_method", os.getenv("OPLL_PAYMENT_METHOD", "paypal")) or "paypal").lower()
    apply_update = payload.get("apply_checkout_update", _env_bool("OPLL_UPDATE_CHECKOUT", True))
    # Accept both OAICS (oaics_*) and Stripe Checkout (cs_*) PayPal flows.
    # Old browser preferences could keep oaics_only=true and discard most
    # otherwise usable accounts before provider confirmation.
    oaics_only = False
    if not isinstance(apply_update, bool):
        raise ConfigurationError("apply_checkout_update must be boolean")
    if not str(access_token or "").strip():
        raise ConfigurationError("AT is required")
    if not str(checkout_proxy or "").strip():
        raise ConfigurationError("checkout proxy is required")
    if apply_update and not str(update_proxy or "").strip():
        raise ConfigurationError("update proxy is required")
    if country not in SUPPORTED_COUNTRIES:
        country_config(country)
    normalize_payment_method(payment_method)
    return ExtractionConfig(
        access_token=str(access_token).strip(),
        checkout_proxy=str(checkout_proxy).strip(),
        update_proxy=str(update_proxy or "").strip(),
        stripe_hcaptcha_token=str(hcaptcha or "").strip(),
        country=country,
        payment_method=payment_method,
        apply_checkout_update=apply_update,
        verbose=False,
        oaics_only=oaics_only,
    )


def _value(payload: dict[str, Any], key: str, env_key: str, default: str = "") -> Any:
    value = payload.get(key)
    return value if value is not None else os.getenv(env_key, default)


def _credential_value(payload: dict[str, Any]) -> str:
    for key in ("access_token", "accessToken", "token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _configured_proxy_pool() -> str:
    file_name = os.getenv("OPLL_PROXY_POOL_FILE", "").strip()
    if not file_name:
        return ""
    try:
        content = Path(file_name).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    return "\n".join(line.strip() for line in content.splitlines() if line.strip())


def _error(message: str, status_code: int) -> Any:
    return jsonify({"ok": False, "error": message}), status_code
