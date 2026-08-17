#!/usr/bin/env python3
"""Local web UI for the PayPal Billing Agreement flow.

Run:
    python web.py --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html as html_lib
import json
import mimetypes
import os
import random
import re
import unicodedata
import secrets
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from loguru import logger

from config import USER_AGENT
from paypal.flow import PayPalFlow
from paypal.elevation_flow import IdentityElevationPayPalFlow
from paypal.manual_browser import ManualBrowserController
from paypal.models import BillingAddress, CardInfo, UserInfo, generate_address, generate_card, generate_user
from paypal.online_address import resolve_online_address
from paypal.proxy import ProxyConfig, ProxyEntry, build_proxy_config
from paypal.runtime_country_resolver import infer_dynamic_kyc, resolve_runtime_country_schema, validate_runtime_address, validate_runtime_phone

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web_static"
METRICS_PATH = Path(
    os.getenv("PAYPAL_WEB_METRICS_PATH")
    or str(ROOT / "data" / "protocol_metrics.json")
)
PAYMENT_AUDIT_PATH = Path(
    os.getenv("PAYPAL_WEB_PAYMENT_AUDIT_PATH")
    or str(ROOT / "data" / "payment_audit.jsonl")
)
PAYMENT_AUDIT_KEY_PATH = Path(
    os.getenv("PAYPAL_WEB_PAYMENT_AUDIT_KEY_PATH")
    or str(ROOT / "data" / ".payment_audit_hmac_key")
)
# Full protocol traces are kept in a local, permission-restricted JSONL file.
# The browser/API response continues to use the existing redaction layer.
FULL_LOG_PATH = Path(
    os.getenv("PAYPAL_WEB_FULL_LOG_PATH")
    or str(ROOT / "data" / "protocol_full.log")
)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, "")
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(min_value, min(value, max_value))


PRODUCTION_MODE = env_bool("PAYPAL_WEB_PRODUCTION", False)
MAX_LOG_LINES = env_int("PAYPAL_WEB_MAX_LOG_LINES", 300, 50, 2000)
MAX_TOTAL_JOBS = env_int("PAYPAL_WEB_MAX_TOTAL_JOBS", 200, 10, 5000)
MAX_ACTIVE_JOBS = env_int("PAYPAL_WEB_MAX_ACTIVE_JOBS", 20, 1, 100)
MAX_QUEUED_JOBS = env_int("PAYPAL_WEB_MAX_QUEUED_JOBS", 50, 5, 1000)
MAX_ACTIVE_JOBS_PER_DEVICE = env_int("PAYPAL_WEB_MAX_ACTIVE_JOBS_PER_DEVICE", 2, 1, 20)
PROXY_PROBE_LIMIT = env_int("PAYPAL_WEB_PROXY_PROBE_LIMIT", 8, 1, 30)
JOB_RETENTION_SECONDS = env_int("PAYPAL_WEB_JOB_RETENTION_SECONDS", 24 * 60 * 60, 60, 30 * 24 * 60 * 60)
OTP_INPUT_TIMEOUT_SECONDS = env_int("PAYPAL_WEB_OTP_TIMEOUT_SECONDS", 30 * 60, 60, 24 * 60 * 60)
ALLOW_DEBUG_LOGS = env_bool("PAYPAL_WEB_ALLOW_DEBUG_LOGS", False)
# Enable complete backend traces without exposing them through the public UI.
# Set PAYPAL_WEB_FULL_LOGS=0 to disable local full traces.
FULL_LOGS_ENABLED = env_bool("PAYPAL_WEB_FULL_LOGS", True)
FULL_LOGS_UI_ENABLED = env_bool("PAYPAL_WEB_FULL_LOGS_UI", False)
ENABLE_DYNAMIC_COUNTRIES = env_bool("PAYPAL_WEB_ENABLE_DYNAMIC_COUNTRIES", False)
PAY153_INTERNAL_BASE = str(os.getenv("PAYPAL_WEB_PAY153_INTERNAL_BASE", "http://127.0.0.1:18096")).rstrip("/")
# The source tree contains an optional post-authorization bridge for a separate
# service.  Keep it opt-in in the integrated copy so a successful protocol job
# remains successful when that unrelated service is not running.
ENABLE_PAY153_BRIDGE = env_bool("PAYPAL_WEB_ENABLE_PAY153_BRIDGE", False)
COOKIE_SECURE = env_bool("PAYPAL_WEB_COOKIE_SECURE", False)
DEVICE_COOKIE_NAME = "paypal_web_device_id"
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
DEVICE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
BA_TOKEN_RE = re.compile(r"^BA-[A-Za-z0-9]{8,80}$")
PHONE_RE = re.compile(r"^\+?\d{8,20}$")
DATADOME_HOST_RE = re.compile(r"(^|\.)captcha-delivery\.com$", re.I)

ACTIVE_STATUSES = {"queued", "running", "awaiting_otp", "awaiting_captcha", "cancelling"}
RUNNER_SEMAPHORE = threading.BoundedSemaphore(MAX_ACTIVE_JOBS)
RATE_LOCK = threading.RLock()
RATE_BUCKETS: dict[tuple[str, str], list[float]] = {}
METRICS_LOCK = threading.RLock()
PAYMENT_AUDIT_LOCK = threading.RLock()
FULL_LOG_LOCK = threading.RLock()


# ----------------------------- helpers -----------------------------


def now_ts() -> float:
    return time.time()


def write_full_log(*, level: str, message: Any, ts: float | None = None, job_id: str = "") -> None:
    """Write the original backend log line to a local JSONL audit file.

    This file is deliberately separate from the public job payload. It is useful
    for diagnosing protocol failures while keeping tokens, URLs and PII out of
    the browser-facing API.
    """
    if not FULL_LOGS_ENABLED:
        return
    event = {
        "time": ts if ts is not None else now_ts(),
        "level": str(level or "INFO"),
        "job_id": str(job_id or ""),
        "message": str(message),
    }
    try:
        with FULL_LOG_LOCK:
            FULL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with FULL_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            try:
                os.chmod(FULL_LOG_PATH, 0o600)
            except OSError:
                pass
    except Exception:
        # Logging must never interrupt a payment job.
        pass


def _empty_metrics() -> dict[str, Any]:
    return {
        "success_total": 0,
        "failure_total": 0,
        "events": [],
        "updated_at": 0,
    }


def _load_metrics() -> dict[str, Any]:
    try:
        payload = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return _empty_metrics()
        payload.setdefault("success_total", 0)
        payload.setdefault("failure_total", 0)
        payload.setdefault("events", [])
        payload.setdefault("updated_at", 0)
        return payload
    except Exception:
        return _empty_metrics()


def _save_metrics(payload: dict[str, Any]) -> None:
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = METRICS_PATH.with_suffix(METRICS_PATH.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(METRICS_PATH)


def record_protocol_metric(job: "WebJob") -> None:
    if job.exclude_public_metrics:
        return
    if job.status not in {"completed", "failed"}:
        return
    finished_at = float(job.finished_at or now_ts())
    started_at = float(job.started_at or job.created_at or finished_at)
    event = {
        "ts": finished_at,
        "status": "success" if job.status == "completed" else "failed",
        "country": str(job.country or "BR"),
        "duration": round(max(0.0, finished_at - started_at), 2),
    }
    with METRICS_LOCK:
        payload = _load_metrics()
        if event["status"] == "success":
            payload["success_total"] = int(payload.get("success_total") or 0) + 1
        else:
            payload["failure_total"] = int(payload.get("failure_total") or 0) + 1
        events = [item for item in payload.get("events", []) if isinstance(item, dict)]
        events.append(event)
        payload["events"] = events[-5000:]
        payload["updated_at"] = finished_at
        _save_metrics(payload)


def _payment_audit_key() -> bytes:
    with PAYMENT_AUDIT_LOCK:
        if PAYMENT_AUDIT_KEY_PATH.exists():
            key = PAYMENT_AUDIT_KEY_PATH.read_bytes().strip()
            if key:
                return key
        PAYMENT_AUDIT_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        PAYMENT_AUDIT_KEY_PATH.write_bytes(key)
        try:
            os.chmod(PAYMENT_AUDIT_KEY_PATH, 0o600)
        except OSError:
            pass
        return key


def _payment_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hmac.new(_payment_audit_key(), text.encode("utf-8"), hashlib.sha256).hexdigest()


def record_payment_audit(job: "WebJob", result: dict[str, Any]) -> None:
    """Persist correlation fingerprints without retaining replayable secrets."""
    if not isinstance(result, dict):
        return
    return_url = str(result.get("return_url") or "")
    final_url = str(result.get("final_redirect_url") or "")
    return_parsed = urlparse(return_url) if return_url else None
    final_parsed = urlparse(final_url) if final_url else None
    nonce = ""
    if return_parsed:
        nonce_match = re.search(r"/(?:pa|sa)_nonce_([^/?#]+)", return_parsed.path or "")
        nonce = nonce_match.group(1) if nonce_match else ""
    return_path = (return_parsed.path or "") if return_parsed else ""
    return_path = re.sub(
        r"((?:pa|sa)_nonce_)[^/?#]+",
        r"\1<fingerprinted>",
        return_path,
    )
    event = {
        "ts": now_ts(),
        "job_id": job.id,
        "country": str(job.country or "").upper(),
        "result_status": str(result.get("status") or ""),
        "settlement_status": str(result.get("settlement_status") or ""),
        "redirect_status": str(result.get("redirect_status") or ""),
        "payment_action": str(result.get("payment_action") or ""),
        "ba_fingerprint": _payment_fingerprint(result.get("ba_token")),
        "ec_fingerprint": _payment_fingerprint(result.get("ec_token")),
        "payer_fingerprint": _payment_fingerprint(result.get("user_id")),
        "nonce_fingerprint": _payment_fingerprint(nonce),
        "return_host": (return_parsed.hostname or "") if return_parsed else "",
        "return_path": return_path,
        "final_host": (final_parsed.hostname or "") if final_parsed else "",
        "final_path": (final_parsed.path or "") if final_parsed else "",
    }
    PAYMENT_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PAYMENT_AUDIT_LOCK:
        with PAYMENT_AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            os.chmod(PAYMENT_AUDIT_PATH, 0o600)
        except OSError:
            pass


def protocol_metrics_public() -> dict[str, Any]:
    with METRICS_LOCK:
        payload = _load_metrics()
    success_total = int(payload.get("success_total") or 0)
    failure_total = int(payload.get("failure_total") or 0)
    events = [item for item in payload.get("events", []) if isinstance(item, dict)]
    success_events = [item for item in events if item.get("status") == "success"]
    recent_successes = success_events[-500:]
    durations = [
        float(item.get("duration") or 0)
        for item in success_events[-100:]
        if float(item.get("duration") or 0) > 0
    ]
    # Build the public timeline from the complete retained event window rather
    # than from ``recent_successes``.  Returning only the latest 500 successes
    # caused earlier bars from the same day to disappear on busy days.
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    first_hour = current_hour - timedelta(hours=23)
    hourly = [
        {
            "start_ts": (first_hour + timedelta(hours=index)).timestamp(),
            "label": (first_hour + timedelta(hours=index)).strftime("%H:%M"),
            "count": 0,
        }
        for index in range(24)
    ]
    first_hour_ts = first_hour.timestamp()
    for item in success_events:
        event_ts = float(item.get("ts") or 0)
        index = int((event_ts - first_hour_ts) // 3600)
        if 0 <= index < len(hourly):
            hourly[index]["count"] += 1
    today_start_ts = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    success_today = sum(
        1 for item in success_events if float(item.get("ts") or 0) >= today_start_ts
    )
    finished_total = success_total + failure_total
    return {
        "success_total": success_total,
        "failure_total": failure_total,
        "finished_total": finished_total,
        "success_rate": round(success_total / finished_total * 100, 1) if finished_total else 0,
        "latest_success_at": float(success_events[-1].get("ts") or 0) if success_events else 0,
        "average_success_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
        "success_events": recent_successes,
        "success_today": success_today,
        "success_hourly_24h": hourly,
        "updated_at": float(payload.get("updated_at") or 0),
    }


def extract_ba_token(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"BA-[A-Za-z0-9]{8,80}", text)
    return match.group(0) if match else text


def _decode_embedded_urls(value: str) -> str:
    """Normalize URL escaping used by DataDome HTML/JSON snippets."""
    text = html_lib.unescape(str(value or ""))
    for _ in range(2):
        previous = text
        text = (
            text.replace("\\/", "/")
            .replace("\\u002F", "/")
            .replace("\\u002f", "/")
            .replace("\\u003A", ":")
            .replace("\\u003a", ":")
            .replace("\\u0026", "&")
            .replace("\\x2F", "/")
            .replace("\\x2f", "/")
            .replace("\\x3A", ":")
            .replace("\\x3a", ":")
            .replace("\\x26", "&")
        )
        text = html_lib.unescape(text)
        if text == previous:
            break
    return text


def _valid_datadome_challenge_url(value: str, base_url: str = "") -> str:
    candidate = _decode_embedded_urls(value).strip().strip("'\"`<>()[]{};,\\")
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif candidate.startswith("/") and base_url:
        candidate = urljoin(base_url, candidate)
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if not DATADOME_HOST_RE.search(parsed.hostname):
        return ""
    if "/captcha" not in (parsed.path or "").lower():
        return ""
    return candidate


def _datadome_config_from_body(body: str) -> dict[str, str]:
    normalized = _decode_embedded_urls(body)
    config: dict[str, str] = {}
    aliases = {
        "hsh": "hash",
        "hash": "hash",
        "cid": "cid",
        "initialcid": "initialCid",
        "t": "t",
        "s": "s",
        "e": "e",
        "host": "host",
    }
    for match in re.finditer(
        r"(?<![A-Za-z0-9_])[\"']?(initialCid|cid|hsh|hash|t|s|e|host)[\"']?\s*:\s*"
        r"(?:[\"']([^\"']*)[\"']|([0-9]+))",
        normalized,
        re.I,
    ):
        key = aliases.get(match.group(1).lower())
        value = match.group(2) if match.group(2) is not None else match.group(3)
        if key and value is not None and key not in config:
            config[key] = str(value)
    return config


def _build_datadome_challenge_url(
    response,
    base_url: str,
    initial_cid: str = "",
) -> str:
    body = str(getattr(response, "text", "") or "")
    config = _datadome_config_from_body(body)
    headers = getattr(response, "headers", {}) or {}
    try:
        header_cid = headers.get("x-datadome-cid", "") or headers.get("X-DataDome-CID", "")
    except Exception:
        header_cid = ""
    cid = config.get("cid") or str(header_cid or "")
    challenge_hash = config.get("hash", "")
    if not cid or not challenge_hash:
        return ""

    host = config.get("host", "geo.captcha-delivery.com").strip()
    if host.startswith("http://") or host.startswith("https://"):
        parsed_host = urlparse(host).hostname or ""
        host = parsed_host
    host = host.strip("/ ")
    if not DATADOME_HOST_RE.search(host):
        host = "geo.captcha-delivery.com"

    query = {
        "initialCid": initial_cid or config.get("initialCid") or cid,
        "hash": challenge_hash,
        "cid": cid,
        "t": config.get("t") or "fe",
        "referer": base_url,
    }
    if config.get("s"):
        query["s"] = config["s"]
    if config.get("e"):
        query["e"] = config["e"]
    return f"https://{host}/captcha/?{urlencode(query)}"


def extract_datadome_challenge_url(
    response,
    base_url: str = "",
    initial_cid: str = "",
) -> str:
    """Extract the real DataDome CAPTCHA URL without fabricating a fallback."""
    candidates: list[str] = []
    response_url = str(getattr(response, "url", "") or "")
    if response_url:
        candidates.append(response_url)

    headers = getattr(response, "headers", {}) or {}
    for header_name in ("Location", "location", "Refresh", "refresh"):
        try:
            header_value = headers.get(header_name, "")
        except Exception:
            header_value = ""
        if header_value:
            candidates.append(str(header_value).split("url=", 1)[-1].strip())

    raw_body = str(getattr(response, "text", "") or "")
    bodies = [_decode_embedded_urls(raw_body)]
    try:
        decoded = unquote(bodies[0])
        if decoded != bodies[0]:
            bodies.append(decoded)
    except Exception:
        pass

    url_pattern = re.compile(r"(?:https?:)?//[^\s\"'<>]+", re.I)
    for body in bodies:
        candidates.extend(match.group(0) for match in url_pattern.finditer(body))

        # Some responses expose the challenge under a JSON key with a
        # protocol-relative or escaped value.
        for match in re.finditer(
            r"(?:captchaUrl|captcha_url|challengeUrl|challenge_url|iframeSrc|iframe_src)"
            r"\s*[\"']?\s*[:=]\s*[\"']([^\"']+)",
            body,
            re.I,
        ):
            candidates.append(match.group(1))

    for candidate in candidates:
        valid = _valid_datadome_challenge_url(candidate, base_url or response_url)
        if valid:
            return valid
    return _build_datadome_challenge_url(response, base_url or response_url, initial_cid)


def parse_proxy_pool(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        candidates = [str(item).strip() for item in value]
    else:
        candidates = [item.strip() for item in str(value).replace("\r", "\n").split("\n")]
    entries = [item for item in candidates if item and not item.startswith("#")]
    if len(entries) > 500:
        raise ValueError("Proxy pool supports at most 500 entries")
    for entry in entries:
        ProxyEntry.parse(entry)
    return entries


def _country_proxy_candidates(proxy_pool: list[str], country: str) -> list[str]:
    country = str(country or "").strip().upper()
    if not country:
        return list(proxy_pool)
    pattern = re.compile(rf"(?i)(?:region|country)[-_=:]?{re.escape(country)}(?:\b|[-_])")
    return [item for item in proxy_pool if pattern.search(item)]


def build_ephemeral_proxy_config(proxy_pool: list[str], country: str = "BR") -> ProxyConfig:
    if not proxy_pool:
        raise ValueError(f"PayPal protocol payment requires at least one {country} proxy")
    country_candidates = _country_proxy_candidates(proxy_pool, country)
    selected = random.choice(country_candidates or proxy_pool)
    return ProxyConfig(enabled=True, entry=ProxyEntry.parse(selected))


def proxy_probe(proxy_config: ProxyConfig, timeout_seconds: float = 8.0) -> tuple[bool, str]:
    """Verify that a proxy can establish an HTTPS tunnel before a job starts."""
    if not proxy_config.enabled or not proxy_config.url:
        return False, "proxy is disabled"
    try:
        bridge_started = proxy_config.prepare()
        if proxy_config.uses_bridge:
            logger.info(
                "Proxy transport bridge ready ({})",
                "started" if bridge_started else "reused",
            )
        with httpx.Client(
            proxy=proxy_config.url,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(
                "https://www.paypal.com/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
            )
        # Any HTTP response proves the CONNECT tunnel worked. A PayPal 403 can
        # be an application challenge and is handled later by the flow.
        if response.status_code == 407:
            return False, "proxy authentication required (HTTP 407)"
        return True, f"HTTP {response.status_code}"
    except Exception as exc:
        return False, redact_text(exc)


def select_working_proxy(
    proxy_pool: list[str],
    preferred: ProxyConfig | None = None,
    country: str = "BR",
    cancel_event: threading.Event | None = None,
) -> ProxyConfig:
    if not proxy_pool:
        return preferred or build_proxy_config(enabled=None)

    country_candidates = _country_proxy_candidates(proxy_pool, country)
    candidates = list(country_candidates or proxy_pool)
    random.shuffle(candidates)
    if preferred and preferred.entry:
        preferred_raw = next(
            (item for item in candidates if ProxyEntry.parse(item) == preferred.entry),
            None,
        )
        if preferred_raw:
            candidates.remove(preferred_raw)
            candidates.insert(0, preferred_raw)

    attempts = min(len(candidates), PROXY_PROBE_LIMIT)
    last_reason = "no candidates"
    for index, raw in enumerate(candidates[:attempts], start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Task cancelled")
        config = ProxyConfig(enabled=True, entry=ProxyEntry.parse(raw))
        logger.info("Checking proxy {}/{} before task start", index, attempts)
        ok, reason = proxy_probe(config)
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Task cancelled")
        if ok:
            logger.info("Proxy check passed ({})", reason)
            return config
        last_reason = reason
        logger.warning("Proxy check failed; switching to another entry: {}", reason)
    raise RuntimeError(f"代理池检测失败：已尝试 {attempts} 条线路；最后错误：{last_reason}")


def mask_middle(value: str, left: int = 6, right: int = 4) -> str:
    value = value or ""
    if len(value) <= left + right:
        return "***" if value else ""
    return f"{value[:left]}…{value[-right:]}"


def mask_card(number: str) -> str:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if len(digits) <= 4:
        return "••••"
    grouped = " ".join([digits[i : i + 4] for i in range(0, len(digits), 4)])
    return f"•••• •••• •••• {grouped[-4:]}"


def mask_email(value: str) -> str:
    if "@" not in (value or ""):
        return "***"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1:]}@{domain}"


def mask_digits(value: str, keep: int = 4) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) <= keep:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - keep)}{digits[-keep:]}"


def mask_phone(value: str) -> str:
    return mask_digits(value, keep=4)


def redact_text(value: Any) -> str:
    """Best-effort redaction for logs/UI errors. Keep status information, hide secrets/PII."""
    text = str(value or "")
    if not text:
        return text
    # Explicit diagnostic mode: keep the web job log stream byte-for-byte clear.
    if FULL_LOGS_UI_ENABLED:
        return text

    # URL query parameters and JSON-ish key/value pairs.
    text = re.sub(
        r"(?i)([?&](?:ba_token|token|ec_token|billingAgreementId|access_token|code|pin|password|otp)=)([^&\s\"']+)",
        lambda m: f"{m.group(1)}{mask_middle(m.group(2), 4, 4)}",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:ba_token|ec_token|billingAgreementId|token|accessToken|password|securityCode|cvv|pin|otp)\b\s*[:=]\s*)([\"']?)([^&,\"'\s}{]+)([\"']?)",
        lambda m: f"{m.group(1)}{m.group(2)}<redacted>{m.group(4)}",
        text,
    )

    # Common token formats.
    text = re.sub(r"\bBA-[A-Za-z0-9]{8,80}\b", lambda m: mask_middle(m.group(0), 4, 4), text)
    text = re.sub(r"\bEC-[A-Za-z0-9]{8,80}\b", lambda m: mask_middle(m.group(0), 4, 4), text)

    # Email, CPF, card-like long digit sequences, Brazil/international phone-like values.
    text = re.sub(
        r"\b([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b",
        lambda m: mask_email(m.group(0)),
        text,
    )
    text = re.sub(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", "<redacted-cpf>", text)
    text = re.sub(
        r"(?<!\w)(?:\d[ -]?){13,19}(?!\w)",
        lambda m: mask_digits(m.group(0), keep=4),
        text,
    )
    text = re.sub(
        r"(?<!\w)\+?\d[\d(). -]{7,18}\d(?!\w)",
        lambda m: mask_phone(m.group(0)),
        text,
    )
    return text


def sanitize_url_for_output(value: str) -> str:
    try:
        parsed = urlparse(str(value or ""))
        cleaned_query: list[tuple[str, str]] = []
        for name, item in parse_qsl(parsed.query, keep_blank_values=True):
            compact = name.lower().replace("-", "_")
            if any(marker in compact for marker in ("client_secret", "access_token", "ba_token", "ec_token", "nonce")) or compact == "token":
                item = "<redacted>"
            cleaned_query.append((name, item))
        return urlunparse(parsed._replace(query=urlencode(cleaned_query), fragment=""))
    except Exception:
        return "<redacted>"


def sanitize_payload(value: Any, key: str = "") -> Any:
    """Redact sensitive values before returning API payloads to the browser."""
    compact_key = key.lower().replace("_", "").replace("-", "")
    if isinstance(value, dict):
        return {k: sanitize_payload(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, key) for item in value]
    if not isinstance(value, str):
        return value

    if compact_key in {"password", "securitycode", "cvv", "pin", "otp", "authorization", "cookie", "accesstoken"}:
        return "<redacted>"
    if compact_key in {"token", "batoken", "ectoken", "billingagreementid", "billingagreementtoken"}:
        return mask_middle(value, 4, 4)
    if compact_key in {"cardnumber", "encryptednumber"}:
        return mask_digits(value, keep=4)
    if compact_key in {"cpf", "identitydocument", "document"}:
        return "<redacted>"
    if compact_key == "email":
        return mask_email(value)
    if compact_key in {"phonenumber", "phone", "number", "phonelocal"} and sum(ch.isdigit() for ch in value) >= 8:
        return mask_phone(value)
    if compact_key.endswith("url") or compact_key in {"href", "referer", "location"}:
        return truncate_text(sanitize_url_for_output(value))
    return truncate_text(redact_text(value))


def truncate_text(value: str, max_chars: int = 1000) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}…<truncated>"


def safe_result_payload(value: Any) -> Any:
    sanitized = sanitize_payload(value)
    if isinstance(sanitized, dict) and "raw_response" in sanitized:
        sanitized["raw_response"] = "<redacted>"
    if isinstance(value, dict) and isinstance(sanitized, dict):
        # The owning browser needs the complete OpenAI pending/verification URL
        # to finish settlement while logged into the corresponding account.
        for key in ("pending_url", "verification_url"):
            candidate = str(value.get(key) or "").strip()
            try:
                parsed = urlparse(candidate)
                if (
                    parsed.scheme == "https"
                    and (parsed.hostname or "").lower()
                    in {"chatgpt.com", "chat.openai.com", "pay.openai.com"}
                ):
                    sanitized[key] = candidate
            except Exception:
                pass
    return sanitized


def parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key:
            cookies[key] = value
    return cookies


def public_generated_payload(user: UserInfo, card: CardInfo, address: BillingAddress) -> dict[str, Any]:
    """Data shown in the browser. Keep secrets/PII masked in API responses."""
    return {
        "user": {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": mask_email(user.email),
            "phone": mask_phone(user.phone),
            "phone_country_code": user.phone_country_code,
            "phone_local": mask_phone(user.phone_local),
            "password": "<redacted>",
            "dob": "<redacted>",
            "cpf": "<redacted>",
        },
        "card": {
            "number": mask_card(card.number),
            "expiry": card.expiry,
            "cvv": "***",
            "card_type": card.card_type,
        },
        "address": sanitize_payload(asdict(address)),
    }


# ----------------------------- job model -----------------------------


@dataclass
class WebJob:
    id: str
    owner_device_id: str
    ba_token: str
    phone: str
    country: str = "BR"
    buyer_mode: str = "identity_elevation"
    debug: bool = False
    max_card_attempts: int = 5
    manual_funding: bool = False
    agreement_only: bool = False
    exclude_public_metrics: bool = False
    proxy_enabled: bool = False
    proxy_label: str = "代理关闭"
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    started_at: float | None = None
    finished_at: float | None = None
    status: str = "queued"  # queued | running | awaiting_otp | completed | failed
    stage: str = "排队中"
    result: dict[str, Any] | None = None
    error: str = ""
    traceback_text: str = ""
    generated: dict[str, Any] | None = None
    runtime_schema: dict[str, Any] | None = None
    awaiting_prompt: str = ""
    challenge_url: str = ""
    logs: list[dict[str, Any]] = field(default_factory=list)
    _condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _input_queue: list[str] = field(default_factory=list, repr=False)
    _captcha_queue: list[str] = field(default_factory=list, repr=False)
    _proxy_config: ProxyConfig | None = field(default=None, repr=False)
    _proxy_pool: list[str] = field(default_factory=list, repr=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _flow: Any = field(default=None, repr=False)
    _browser: ManualBrowserController | None = field(default=None, repr=False)
    _slot_held: bool = field(default=False, repr=False)

    def set_status(self, status: str, stage: str | None = None) -> None:
        with self._condition:
            if self._cancel_event.is_set() and status not in {"cancelling", "cancelled"}:
                return
            self.status = status
            if stage is not None:
                self.stage = stage
            self.updated_at = now_ts()
            self._condition.notify_all()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise RuntimeError("Task cancelled")

    def cancel(self) -> None:
        with self._condition:
            if self.status in {"completed", "failed", "cancelled"}:
                return
            self._cancel_event.set()
            # The API/UI must acknowledge cancellation immediately.  Network
            # transports and Chromium cleanup can block internally, so resource
            # shutdown is performed asynchronously below.
            self.status = "cancelled"
            self.stage = "Cancelled"
            self.finished_at = now_ts()
            self.awaiting_prompt = ""
            self.challenge_url = ""
            self.updated_at = now_ts()
            self._condition.notify_all()
        # Do not let a cancelled task keep a public execution slot occupied
        # while a third-party HTTP request finishes its own timeout.
        self.release_execution_slot()
        threading.Thread(
            target=self._cleanup_cancelled_resources,
            name=f"paypal-cancel-{self.id}",
            daemon=True,
        ).start()

    def _cleanup_cancelled_resources(self) -> None:
        flow = self._flow
        if flow is not None:
            try:
                flow.close()
            except Exception:
                pass
        browser = self._browser
        if browser is not None:
            try:
                browser.stop()
            except Exception:
                pass

    def mark_cancelled(self) -> None:
        with self._condition:
            self.status = "cancelled"
            self.stage = "Cancelled"
            self.finished_at = now_ts()
            self.updated_at = now_ts()
            self.awaiting_prompt = ""
            self.challenge_url = ""
            self._browser = None
            self._condition.notify_all()

    def acquire_execution_slot(self) -> None:
        while True:
            self.check_cancelled()
            if RUNNER_SEMAPHORE.acquire(timeout=0.5):
                with self._condition:
                    if self._cancel_event.is_set():
                        RUNNER_SEMAPHORE.release()
                        raise RuntimeError("Task cancelled")
                    self._slot_held = True
                    self.updated_at = now_ts()
                return

    def release_execution_slot(self) -> None:
        should_release = False
        with self._condition:
            if self._slot_held:
                self._slot_held = False
                self.updated_at = now_ts()
                should_release = True
        if should_release:
            RUNNER_SEMAPHORE.release()

    def set_generated(self, generated: dict[str, Any]) -> None:
        with self._condition:
            self.generated = generated
            self.updated_at = now_ts()
            self._condition.notify_all()

    def add_log(self, level: str, message: str, ts: float | None = None) -> None:
        with self._condition:
            rendered_message = str(message).rstrip()
            if not FULL_LOGS_UI_ENABLED:
                rendered_message = redact_text(rendered_message)
            self.logs.append({
                "time": ts or now_ts(),
                "level": level,
                "message": truncate_text(rendered_message, 900),
            })
            if len(self.logs) > MAX_LOG_LINES:
                del self.logs[: len(self.logs) - MAX_LOG_LINES]
            self.updated_at = now_ts()
            self._condition.notify_all()

    def wait_for_input(self, prompt: str) -> str:
        with self._condition:
            self.check_cancelled()
            self.status = "awaiting_otp"
            self.stage = "Waiting for SMS code / new phone"
            self.awaiting_prompt = redact_text(prompt)
            self.updated_at = now_ts()
            self._condition.notify_all()
        # Human wait time must not occupy a network execution slot.
        self.release_execution_slot()
        with self._condition:
            deadline = now_ts() + OTP_INPUT_TIMEOUT_SECONDS
            while not self._input_queue:
                self.check_cancelled()
                remaining = deadline - now_ts()
                if remaining <= 0:
                    raise TimeoutError("Waiting for SMS code or phone timed out")
                self._condition.wait(timeout=min(0.5, remaining))
            value = self._input_queue.pop(0).strip()
            self.check_cancelled()
            self.status = "queued"
            self.stage = "SMS input received; waiting for execution slot"
            self.awaiting_prompt = ""
            self.updated_at = now_ts()
            self._condition.notify_all()
        self.acquire_execution_slot()
        with self._condition:
            self.check_cancelled()
            self.status = "running"
            self.stage = "SMS input received; continuing"
            self.updated_at = now_ts()
            self._condition.notify_all()
        return value

    def wait_for_captcha(self, challenge_url: str) -> str:
        with self._condition:
            self.check_cancelled()
            self.status = "awaiting_captcha"
            self.stage = "Waiting for manual CAPTCHA"
            self.awaiting_prompt = (
                "打开真实验证地址完成验证，再粘贴 datadome Cookie 或 adsddtoken"
                if challenge_url
                else "响应中没有真实验证地址；请提交从同一任务会话取得的 datadome Cookie 或 adsddtoken"
            )
            self.challenge_url = challenge_url
            self.updated_at = now_ts()
            self._condition.notify_all()
        self.release_execution_slot()
        with self._condition:
            deadline = now_ts() + OTP_INPUT_TIMEOUT_SECONDS
            while not self._captcha_queue:
                self.check_cancelled()
                remaining = deadline - now_ts()
                if remaining <= 0:
                    raise TimeoutError("Waiting for manual CAPTCHA timed out")
                self._condition.wait(timeout=min(0.5, remaining))
            value = self._captcha_queue.pop(0).strip()
            self.check_cancelled()
            self.status = "queued"
            self.stage = "Manual CAPTCHA received; waiting for execution slot"
            self.awaiting_prompt = ""
            self.challenge_url = ""
            self.updated_at = now_ts()
            self._condition.notify_all()
        self.acquire_execution_slot()
        with self._condition:
            self.check_cancelled()
            self.status = "running"
            self.stage = "Manual CAPTCHA received; continuing"
            self.updated_at = now_ts()
            self._condition.notify_all()
        return value

    def wait_for_browser(
        self,
        browser: ManualBrowserController,
        *,
        stage: str = "等待服务器临时浏览器验证",
        prompt: str = "请在下方临时 Chromium 画面中完成验证",
    ) -> list[dict[str, Any]]:
        with self._condition:
            self.check_cancelled()
            self._browser = browser
            self.status = "awaiting_captcha"
            self.stage = "等待服务器临时浏览器验证"
            self.awaiting_prompt = "请在下方临时 Chromium 画面中手动完成验证"
            self.challenge_url = ""
            self.updated_at = now_ts()
            self._condition.notify_all()
        browser.start()
        self.release_execution_slot()
        try:
            cookies = browser.wait(OTP_INPUT_TIMEOUT_SECONDS, self._cancel_event)
        finally:
            browser.stop()
        with self._condition:
            self.check_cancelled()
            self.status = "queued"
            self.stage = "手动验证完成；等待执行资源"
            self.awaiting_prompt = ""
            self.updated_at = now_ts()
            self._condition.notify_all()
        self.acquire_execution_slot()
        with self._condition:
            self.check_cancelled()
            self.status = "running"
            self.stage = "手动验证 Cookie 已同步；继续协议流程"
            self._browser = None
            self.updated_at = now_ts()
            self._condition.notify_all()
        return cookies

    def browser_frame(self) -> bytes:
        browser = self._browser
        return browser.frame() if browser is not None else b""

    def browser_action(self, payload: dict[str, Any]) -> None:
        browser = self._browser
        if self.status != "awaiting_captcha" or browser is None:
            raise ValueError("This job has no active manual browser")
        browser.action(payload)

    def submit_captcha(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            raise ValueError("CAPTCHA result is required")
        with self._condition:
            if self.status != "awaiting_captcha":
                raise ValueError("This job is not waiting for a manual CAPTCHA")
            self._captcha_queue.append(value)
            self.stage = "Manual CAPTCHA submitted; validating"
            self.updated_at = now_ts()
            self._condition.notify_all()

    def submit_input(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            raise ValueError("输入不能为空")
        with self._condition:
            if self.status != "awaiting_otp":
                raise ValueError("This job is not waiting for an SMS code")
            self._input_queue.append(value)
            self.stage = "已提交验证码/手机号，等待程序处理"
            self.updated_at = now_ts()
            self._condition.notify_all()

    def complete(self, result: dict[str, Any]) -> None:
        if self._cancel_event.is_set():
            self.mark_cancelled()
            return
        with self._condition:
            result_obj = result if isinstance(result, dict) else {
                "status": "error",
                "error_code": "UNEXPECTED_RESPONSE_TYPE",
                "error": f"flow result type was {type(result).__name__}",
            }
            try:
                record_payment_audit(self, result_obj)
            except Exception as audit_error:
                logger.warning("Payment audit write failed: {}", redact_text(audit_error))
            succeeded = result_obj.get("status") == "success"
            pending_verification = result_obj.get("settlement_status") == "pending_verification"
            self.status = "completed" if succeeded else "failed"
            self.stage = (
                "协议授权完成，等待到账确认"
                if succeeded and pending_verification
                else ("已完成" if succeeded else "最终授权失败")
            )
            self.result = result_obj
            if not succeeded:
                self.error = redact_text(
                    result_obj.get("error_code")
                    or result_obj.get("error")
                    or "protocol flow returned an error result"
                )
            self.finished_at = now_ts()
            self.updated_at = now_ts()
            self.awaiting_prompt = ""
            self.challenge_url = ""
            self._browser = None
            self._condition.notify_all()

    def fail(self, exc: BaseException) -> None:
        if self._cancel_event.is_set():
            self.mark_cancelled()
            return
        with self._condition:
            self.status = "failed"
            self.stage = "执行失败"
            self.error = redact_text(str(exc))
            self.traceback_text = redact_text(traceback.format_exc()) if (self.debug and ALLOW_DEBUG_LOGS) else ""
            self.finished_at = now_ts()
            self.updated_at = now_ts()
            self.awaiting_prompt = ""
            self.challenge_url = ""
            self._browser = None
            self._condition.notify_all()

    def to_dict(self, *, include_logs: bool = True, log_offset: int = 0) -> dict[str, Any]:
        with self._condition:
            logs = self.logs[max(0, log_offset) :] if include_logs else []
            return {
                "id": self.id,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration": (self.finished_at or now_ts()) - (self.started_at or self.created_at),
                "status": self.status,
                "stage": self.stage,
                "ba_token": mask_middle(self.ba_token),
                "phone": mask_phone(self.phone),
                "country": self.country,
                "buyer_mode": self.buyer_mode,
                "debug": self.debug and ALLOW_DEBUG_LOGS,
                "max_card_attempts": self.max_card_attempts,
                "manual_funding": self.manual_funding,
                "agreement_only": self.agreement_only,
                "generated": sanitize_payload(self.generated),
                "runtime_schema": sanitize_payload(self.runtime_schema),
                "cancellable": self.status in ACTIVE_STATUSES,
                "cancel_requested": self._cancel_event.is_set(),
                "awaiting_otp": self.status == "awaiting_otp",
                "awaiting_captcha": self.status == "awaiting_captcha",
                "browser_active": self.status == "awaiting_captcha" and self._browser is not None,
                "browser_state": (
                    self._browser.state().__dict__
                    if self.status == "awaiting_captcha" and self._browser is not None
                    else None
                ),
                "awaiting_prompt": redact_text(self.awaiting_prompt),
                "challenge_url": self.challenge_url if self.status == "awaiting_captcha" else "",
                "result": safe_result_payload(self.result),
                "error": redact_text(self.error),
                "traceback": self.traceback_text if (self.debug and ALLOW_DEBUG_LOGS) else "",
                "logs": logs,
                "log_count": len(self.logs),
            }


JOBS: dict[str, WebJob] = {}
JOBS_LOCK = threading.RLock()


def client_rate_limit(bucket: str, key: str, *, limit: int, window_seconds: int) -> bool:
    current_ts = now_ts()
    with RATE_LOCK:
        cutoff = current_ts - window_seconds
        values = [ts for ts in RATE_BUCKETS.get((bucket, key), []) if ts >= cutoff]
        if len(values) >= limit:
            RATE_BUCKETS[(bucket, key)] = values
            return False
        values.append(current_ts)
        RATE_BUCKETS[(bucket, key)] = values
        return True


def prune_jobs_locked() -> None:
    """Drop old finished jobs and keep the in-memory job list bounded."""
    current_ts = now_ts()
    for job_id, job in list(JOBS.items()):
        if job.status in ACTIVE_STATUSES:
            continue
        finished_or_updated = job.finished_at or job.updated_at
        if current_ts - finished_or_updated > JOB_RETENTION_SECONDS:
            JOBS.pop(job_id, None)

    # Leave one slot available for the job currently being created.  The old
    # <= check returned at exactly MAX_TOTAL_JOBS, while create_job rejected
    # len(JOBS) >= MAX_TOTAL_JOBS immediately afterwards.  Once the history
    # reached 200 entries the public API therefore stayed locked until the
    # retention window expired.
    if len(JOBS) < MAX_TOTAL_JOBS:
        return

    removable = sorted(
        [job for job in JOBS.values() if job.status not in ACTIVE_STATUSES],
        key=lambda item: item.updated_at,
    )
    while len(JOBS) >= MAX_TOTAL_JOBS and removable:
        JOBS.pop(removable.pop(0).id, None)


def active_job_count(owner_device_id: str | None = None) -> int:
    with JOBS_LOCK:
        return sum(
            1
            for job in JOBS.values()
            if job.status in ACTIVE_STATUSES and (owner_device_id is None or job.owner_device_id == owner_device_id)
        )


# ----------------------------- PayPal flow adapter -----------------------------


class WebPayPalFlow(PayPalFlow):
    """PayPalFlow adapter that asks the web page for OTP/new-phone input."""

    def __init__(self, *args: Any, job: WebJob, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.job = job

    def _set_stage(self, stage: str) -> None:
        self.job.check_cancelled()
        self.job.set_status("running", stage)

    def _phase0_initial_load(self):
        self._set_stage("Phase 0：打开协议页")
        return super()._phase0_initial_load()

    def _phase1_risk_controls(self):
        self._set_stage("Phase 1：发送风控/指纹信号")
        return super()._phase1_risk_controls()

    def _phase2_create_account(self):
        self._set_stage("Phase 2：进入创建账号流程")
        result = super()._phase2_create_account()
        try:
            runtime_schema = resolve_runtime_country_schema(
                self.session, self.country, self.lang or "en"
            )
            runtime_schema["kyc"] = infer_dynamic_kyc(
                getattr(self, "_signup_html", "")
            )
            phone_error = validate_runtime_phone(
                runtime_schema, self.user.phone_local, self.user.phone
            )
            runtime_schema["phone_validation_error"] = phone_error
            if phone_error:
                raise RuntimeError(f"RUNTIME_PHONE_VALIDATION_FAILED: {phone_error}")

            # US account creation is especially sensitive to public landmarks
            # and reused POI addresses.  Resolve a fresh residential building
            # for every clean US session, then let PayPal normalize that exact
            # address before building SignUpNewMember variables.
            if self.country == "US":
                last_address_error = None
                for refresh_attempt in range(1, 4):
                    try:
                        self.address = resolve_online_address(
                            self.country,
                            runtime_schema,
                            force_refresh=True,
                        )
                        self._address_normalized_by_paypal = False
                        self._normalize_address_with_paypal(self.state.ec_token or self.ba_token)
                        self.job.set_generated(
                            public_generated_payload(self.user, self.card, self.address)
                        )
                        logger.info(
                            "US residential address refreshed and normalized on attempt {}",
                            refresh_attempt,
                        )
                        last_address_error = None
                        break
                    except Exception as address_error:
                        last_address_error = address_error
                        logger.warning(
                            "US residential address refresh attempt {} failed: {}",
                            refresh_attempt,
                            address_error,
                        )
                if last_address_error is not None:
                    raise RuntimeError(
                        "US_RESIDENTIAL_ADDRESS_RESOLUTION_FAILED: " + str(last_address_error)
                    )
            address_payload = {
                "line1": (
                    f"{self.address.street} {self.address.house_number}".strip()
                    if self.country == "NL"
                    else f"{self.address.house_number} {self.address.street}".strip()
                ),
                "line2": self.address.district,
                "city": self.address.city,
                "state": self.address.state,
                "postalCode": self.address.postal_code,
            }
            if self.country == "JP":
                # OSM/Nominatim occasionally prefixes Japanese postcodes with
                # the postal mark (?) or returns full-width punctuation.  The
                # live PayPal schema accepts seven digits in NNN-NNNN form.
                # Normalize locally before deciding that an online lookup is
                # needed; otherwise each resolver attempt can block for more
                # than a minute across shared Overpass endpoints.
                postcode_digits = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", str(self.address.postal_code or "")))
                if len(postcode_digits) == 7:
                    normalized_postcode = f"{postcode_digits[:3]}-{postcode_digits[3:]}"
                    self.address.postal_code = normalized_postcode
                    address_payload["postalCode"] = normalized_postcode
            address_errors = validate_runtime_address(runtime_schema, address_payload)
            if address_errors:
                logger.warning(
                    "Runtime address validation failed; resolving another online-map {} address: {}",
                    self.country,
                    ", ".join(address_errors),
                )
                for refresh_attempt in range(1, 4):
                    try:
                        candidate = resolve_online_address(
                            self.country,
                            runtime_schema,
                            force_refresh=refresh_attempt > 1,
                        )
                    except Exception as address_error:
                        logger.warning(
                            "Online-map address resolution attempt {} failed: {}",
                            refresh_attempt,
                            address_error,
                        )
                        continue
                    candidate_payload = {
                        "line1": (
                            f"{candidate.street} {candidate.house_number}".strip()
                            if self.country == "NL"
                            else f"{candidate.house_number} {candidate.street}".strip()
                        ),
                        "line2": candidate.district,
                        "city": candidate.city,
                        "state": candidate.state,
                        "postalCode": candidate.postal_code,
                    }
                    candidate_errors = validate_runtime_address(
                        runtime_schema, candidate_payload
                    )
                    if not candidate_errors:
                        self.address = candidate
                        address_payload = candidate_payload
                        address_errors = []
                        self.job.set_generated(
                            public_generated_payload(self.user, self.card, self.address)
                        )
                        logger.info(
                            "Online-map address refreshed and validated on attempt {}",
                            refresh_attempt,
                        )
                        break
                    address_errors = candidate_errors
            runtime_schema["resolved_address"] = sanitize_payload(address_payload)
            runtime_schema["address_validation_errors"] = address_errors
            self.runtime_form_schema = runtime_schema
            self.job.runtime_schema = runtime_schema
            logger.info(
                "Runtime country schema resolved: country={} address_fields={} kyc_fields={} address_errors={}",
                self.country,
                len(runtime_schema.get("address_fields") or []),
                len((runtime_schema.get("kyc") or {}).get("fields") or []),
                len(runtime_schema.get("address_validation_errors") or []),
            )
        except Exception as schema_error:
            if str(schema_error).startswith("RUNTIME_PHONE_VALIDATION_FAILED"):
                raise
            logger.warning("Runtime country schema resolution soft-failed: {}", schema_error)
        return result

    def _phase3_signup_and_2fa(self):
        self._set_stage("Phase 3：短信验证与注册")
        return super()._phase3_signup_and_2fa()

    def _phase4_authorize(self):
        self._set_stage("Phase 4：最终授权")
        return super()._phase4_authorize()

    def _browser_cookie_snapshot(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            for cookie in self.session.client.cookies.jar:
                result.append({
                    "name": str(getattr(cookie, "name", "") or ""),
                    "value": str(getattr(cookie, "value", "") or ""),
                    "domain": str(getattr(cookie, "domain", "") or ".paypal.com"),
                    "path": str(getattr(cookie, "path", "") or "/"),
                    "secure": bool(getattr(cookie, "secure", True)),
                    "expires": getattr(cookie, "expires", None),
                })
        except Exception as exc:
            logger.warning("Exporting PayPal cookies to temporary Chromium failed: {}", exc)
        return result

    def _sync_browser_cookies(self, cookies: list[dict[str, Any]]) -> None:
        euat_name = "AV894Kt2TSumQQrJwe-8mzmyREO"
        euat_value = ""
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or ".paypal.com")
            path = str(cookie.get("path") or "/")
            if name == euat_name:
                if value:
                    euat_value = value
                continue
            if name and value:
                self.session.client.cookies.set(name, value, domain=domain, path=path)
        if euat_value:
            self.session.set_euat_token(euat_value)
        self.session._sync_state_cookies()

    def _before_final_authorize(self, review_url: str) -> None:
        if not self.job.manual_funding:
            return
        locale_timezone = {
            "GB": ("en-GB", "Europe/London"),
            "US": ("en-US", "America/New_York"),
            "BR": ("pt-BR", "America/Sao_Paulo"),
            "JP": ("ja-JP", "Asia/Tokyo"),
            "TH": ("th-TH", "Asia/Bangkok"),
            "ID": ("id-ID", "Asia/Jakarta"),
            "PH": ("en-PH", "Asia/Manila"),
            "TW": ("zh-TW", "Asia/Taipei"),
            "MX": ("es-MX", "America/Mexico_City"),
            "AE": ("en-AE", "Asia/Dubai"),
            "AU": ("en-AU", "Australia/Sydney"),
            "CA": ("en-CA", "America/Toronto"),
        }
        browser_locale, browser_timezone = locale_timezone.get(self.country, ("en-US", "UTC"))
        logger.info("Opening PayPal official review page for manual funding-source setup")
        controller = ManualBrowserController(
            proxy_config=self.proxy_config,
            user_agent=USER_AGENT,
            cookies=self._browser_cookie_snapshot(),
            start_url=review_url,
            locale=browser_locale,
            timezone_id=browser_timezone,
        )
        solved_cookies = self.job.wait_for_browser(
            controller,
            stage="等待在 PayPal 官方页面绑定付款方式",
            prompt="请在下方 PayPal 官方页面完成付款方式绑定，完成后点击‘完成验证并继续’",
        )
        self._sync_browser_cookies(solved_cookies)
        logger.info("PayPal official funding-source page completed; continuing final authorization")

    def _handle_datadome_challenge(self, response, agreement_url: str):
        # The base flow already implements the protocol-only fallback used by
        # paypal-pay-public-nocdk: keep the DataDome response cookie, POST the
        # agreement route with the protocol marker, then continue resolving
        # the PayPal guest-onboarding context. Starting Chromium here caused
        # browser-slot exhaustion and blank frames under concurrency.
        logger.warning(
            "DataDome challenge detected; continuing with protocol fallback without Chromium"
        )
        return None

    def _profile_rotated(self) -> None:
        self.job.set_generated(public_generated_payload(self.user, self.card, self.address))

    def _prompt_operator(self, prompt: str) -> str:
        logger.info(prompt)
        return self.job.wait_for_input(prompt)

    def _confirm_phone_with_retry(self, token: str, signup_url: str):
        """Web version of the CLI input loop."""
        phone_example = {
            "BR": "+55119800133818",
            "GB": "+447700900123",
            "US": "+12025550123",
            "JP": "+819012345678",
            "TH": "+66812345678",
            "ID": "+6281234567890",
            "PH": "+639171234567",
            "TW": "+886912345678",
            "MX": "+525512345678",
            "AE": "+971501234567",
            "AU": "+61412345678",
            "CA": "+14165550123",
        }.get(self.country, "+12025550123")
        while True:
            try:
                auth_id, challenge_id = self._initiate_2fa_phone_confirmation(token, signup_url)
            except Exception as e:
                logger.error("Failed to initiate OTP for {}: {}", self._masked_phone(), e)
                while True:
                    value = self._prompt_operator(
                        f"发送验证码失败。请输入新的手机号重新发送（如 {phone_example}）；输入 q 退出。"
                    )
                    if value.lower() in {"q", "quit", "exit"}:
                        raise RuntimeError("OTP confirmation cancelled by user") from e
                    try:
                        self._update_user_phone(value)
                        break
                    except ValueError as phone_error:
                        logger.warning("手机号无效：{}。请重新输入。", phone_error)
                continue

            logger.info("SMS verification code sent to phone: {}", self._masked_phone())

            while True:
                value = self._prompt_operator(
                    f"请输入6位短信验证码；如需换号，输入新手机号（如 {phone_example} 或 phone:{phone_example}）；输入 q 退出。"
                )

                if value.lower() in {"q", "quit", "exit"}:
                    raise RuntimeError("OTP confirmation cancelled by user")

                if len(value) == 6 and value.isdigit():
                    if self._confirm_2fa_phone_confirmation(
                        token,
                        signup_url,
                        auth_id,
                        challenge_id,
                        value,
                    ):
                        return
                    logger.warning("验证码验证失败。可以继续输入新的6位验证码，或输入新手机号重新发送验证码。")
                    continue

                try:
                    self._update_user_phone(value)
                    logger.info("Re-sending OTP to the new phone...")
                    break
                except ValueError as e:
                    logger.warning("输入既不是6位验证码，也不是有效手机号：{}。请重新输入。", e)


class WebIdentityElevationPayPalFlow(WebPayPalFlow, IdentityElevationPayPalFlow):
    """Web OTP adapter plus pure-protocol identity-elevation state hydration."""
    pass


# ----------------------------- logging -----------------------------


def _job_log_sink(message: Any) -> None:
    record = message.record
    job_id = record["extra"].get("job_id")
    if not job_id:
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return
    level = record["level"].name
    if level == "DEBUG" and not job.debug:
        return
    job.add_log(level, record["message"], record["time"].timestamp())


def _console_log_sink(message: Any) -> None:
    record = message.record
    level = record["level"].name
    ts = record["time"].strftime("%H:%M:%S")
    text = redact_text(record["message"])
    sys.stderr.write(f"{ts} | {level:<8} | {text}\n")


def _full_log_sink(message: Any) -> None:
    """Persist the unmodified loguru message locally for troubleshooting."""
    record = message.record
    write_full_log(
        level=record["level"].name,
        message=record["message"],
        ts=record["time"].timestamp(),
        job_id=str(record["extra"].get("job_id") or ""),
    )


def configure_logging() -> None:
    logger.remove()
    logger.add(_console_log_sink, level="INFO")
    logger.add(_full_log_sink, level="DEBUG")
    logger.add(_job_log_sink, level="DEBUG", filter=lambda r: bool(r["extra"].get("job_id")))


# ----------------------------- runner -----------------------------


def supported_country_codes() -> set[str]:
    try:
        payload = json.loads(
            (ROOT / "data" / "paypal_supported_countries.json").read_text(encoding="utf-8")
        )
        return {
            str(item.get("code") or "").upper()
            for item in payload.get("countries") or []
            if isinstance(item, dict) and item.get("code")
        }
    except Exception:
        return set()


VERIFIED_PROTOCOL_COUNTRIES = {"BR", "GB", "US", "JP", "TH", "ID", "PH", "TW", "MX", "AE", "AU", "CA"}


def create_job(
    owner_device_id: str,
    ba_token: str,
    phone: str,
    debug: bool,
    max_card_attempts: int,
    manual_funding: bool = False,
    agreement_only: bool = False,
    country: str = "BR",
    buyer_mode: str = "identity_elevation",
    proxy_pool: Any = None,
    exclude_public_metrics: bool = False,
) -> WebJob:
    ba_token = extract_ba_token(ba_token)
    phone = re.sub(r"[\s().-]+", "", (phone or "").strip())
    country = str(country or "BR").strip().upper()
    buyer_mode = str(buyer_mode or "identity_elevation").strip().lower()
    if buyer_mode not in {"original", "identity_elevation"}:
        raise ValueError("Buyer 模式参数不正确")
    allowed_countries = supported_country_codes() if ENABLE_DYNAMIC_COUNTRIES else VERIFIED_PROTOCOL_COUNTRIES
    if country not in allowed_countries:
        raise ValueError("PayPal 国家参数不正确")
    if not ba_token:
        raise ValueError("BA Token 不能为空")
    if not BA_TOKEN_RE.fullmatch(ba_token):
        raise ValueError("BA Token 格式不正确")
    if not phone:
        raise ValueError("手机号不能为空")
    if phone and not PHONE_RE.fullmatch(phone):
        raise ValueError("手机号格式不正确")
    phone_digits = phone.lstrip("+")
    if country == "BR" and not phone_digits.startswith("55"):
        raise ValueError("巴西 PayPal 请填写 +55 手机号")
    if country == "GB" and not phone_digits.startswith("44"):
        raise ValueError("英国 PayPal 请填写 +44 手机号")
    if country == "US" and not phone_digits.startswith("1"):
        raise ValueError("美国 PayPal 请填写 +1 手机号")
    if country == "CA" and not phone_digits.startswith("1"):
        raise ValueError("加拿大 PayPal 请填写 +1 手机号")
    if country == "JP" and not phone_digits.startswith("81"):
        raise ValueError("日本 PayPal 请填写 +81 手机号")
    if country == "TH" and not phone_digits.startswith("66"):
        raise ValueError("泰国 PayPal 请填写 +66 手机号")
    if country == "ID" and not phone_digits.startswith("62"):
        raise ValueError("印度尼西亚 PayPal 请填写 +62 手机号")
    if country == "PH" and not phone_digits.startswith("63"):
        raise ValueError("菲律宾 PayPal 请填写 +63 手机号")
    if country == "TW" and not phone_digits.startswith("886"):
        raise ValueError("中国台湾 PayPal 请填写 +886 手机号")
    if country == "MX" and not phone_digits.startswith("52"):
        raise ValueError("墨西哥 PayPal 请填写 +52 手机号")
    if country == "AE" and not phone_digits.startswith("971"):
        raise ValueError("阿联酋 PayPal 请填写 +971 手机号")
    if country == "AU" and not phone_digits.startswith("61"):
        raise ValueError("澳大利亚 PayPal 请填写 +61 手机号")
    if country == "TH" and not re.fullmatch(r"66[689]\d{8}", phone_digits):
        raise ValueError("泰国手机号格式不正确，请填写 +66 后 9 位手机号码")
    if country == "ID" and not re.fullmatch(r"628\d{8,11}", phone_digits):
        raise ValueError("印度尼西亚手机号格式不正确，请填写 +62 8xx 手机号码")
    if country == "PH" and not re.fullmatch(r"639\d{9}", phone_digits):
        raise ValueError("菲律宾手机号格式不正确，请填写 +63 9xx 手机号码")
    if country == "TW" and not re.fullmatch(r"8869\d{8}", phone_digits):
        raise ValueError("中国台湾手机号格式不正确，请填写 +886 9xx 手机号码")
    if country == "MX" and not re.fullmatch(r"52\d{10}", phone_digits):
        raise ValueError("墨西哥手机号格式不正确，请填写 +52 后 10 位手机号码")
    if country == "AE" and not re.fullmatch(r"9715[024568]\d{7}", phone_digits):
        raise ValueError("阿联酋手机号格式不正确，请填写 +971 5x 手机号码")
    if country == "AU" and not re.fullmatch(r"614\d{8}", phone_digits):
        raise ValueError("澳大利亚手机号格式不正确，请填写 +61 4xx xxx xxx")
    if country == "CA" and not re.fullmatch(r"1[2-9]\d{9}", phone_digits):
        raise ValueError("加拿大手机号格式不正确，请填写 +1 后 10 位号码")
    try:
        max_card_attempts = int(max_card_attempts)
    except Exception as exc:
        raise ValueError("最大换卡次数必须是数字") from exc
    max_card_attempts = max(1, min(max_card_attempts, 20))
    # US validate.fi failures happen before member creation, so the core flow
    # may retry only the funding instrument while preserving the verified
    # phone/profile/address.  Later-stage US failures still stop immediately.
    debug = bool(debug) and ALLOW_DEBUG_LOGS
    proxy_entries = parse_proxy_pool(proxy_pool)
    proxy_config = build_ephemeral_proxy_config(proxy_entries, country=country)

    job = WebJob(
        id=uuid.uuid4().hex[:12],
        owner_device_id=owner_device_id,
        ba_token=ba_token,
        phone=phone,
        country=country,
        buyer_mode=buyer_mode,
        debug=debug,
        max_card_attempts=max_card_attempts,
        manual_funding=bool(manual_funding),
        agreement_only=bool(agreement_only),
        exclude_public_metrics=bool(exclude_public_metrics),
        proxy_enabled=proxy_config.enabled,
        proxy_label=proxy_config.label,
        _proxy_config=proxy_config,
        _proxy_pool=list(proxy_entries),
    )
    with JOBS_LOCK:
        prune_jobs_locked()
        total_active = sum(1 for item in JOBS.values() if item.status in ACTIVE_STATUSES)
        duplicate_active = next(
            (
                item for item in JOBS.values()
                if item.ba_token == ba_token
                and item.owner_device_id == owner_device_id
                and item.status in ACTIVE_STATUSES
            ),
            None,
        )
        if duplicate_active is not None:
            # Idempotent reconnect is valid only for the browser that created
            # the task. Returning another browser's job leaks state and makes
            # all subsequent log/cancel requests fail the ownership check.
            return duplicate_active
        foreign_duplicate = next(
            (
                item for item in JOBS.values()
                if item.ba_token == ba_token
                and item.owner_device_id != owner_device_id
                and item.status in ACTIVE_STATUSES
            ),
            None,
        )
        if foreign_duplicate is not None:
            if exclude_public_metrics and foreign_duplicate.exclude_public_metrics:
                # Internal automatic jobs use one isolated device cookie per
                # task. If the owning automatic task has already failed (for
                # example, SMS inventory/balance failure), its protocol job can
                # remain active under the old cookie and become invisible to the
                # retry. Internal retries may reclaim only other internal jobs;
                # public browser jobs retain the normal ownership protection.
                foreign_duplicate.cancel()
            else:
                raise ValueError("This PayPal link is already being processed by another task")
        user_active = sum(
            1
            for item in JOBS.values()
            if item.status in ACTIVE_STATUSES and item.owner_device_id == owner_device_id
        )
        if total_active >= MAX_QUEUED_JOBS:
            raise ValueError("Current protocol-payment queue is full")
        if user_active >= MAX_ACTIVE_JOBS_PER_DEVICE:
            raise ValueError(f"当前浏览器已有 {user_active} 个未完成任务，请等待完成后再启动")
        if len(JOBS) >= MAX_TOTAL_JOBS:
            raise ValueError("历史任务数量已达上限，请稍后再试")
        JOBS[job.id] = job
    thread = threading.Thread(target=run_job, args=(job,), name=f"paypal-web-{job.id}", daemon=True)
    thread.start()
    return job


def run_job(job: WebJob) -> None:
    with logger.contextualize(job_id=job.id):
        try:
            job.set_status("queued", "Waiting for execution slot")
            job.acquire_execution_slot()
            job.check_cancelled()
            job.started_at = now_ts()
            job.set_status("running", "Preparing protocol payment profile")
            proxy_config = select_working_proxy(
                job._proxy_pool,
                job._proxy_config,
                country=job.country,
                cancel_event=job._cancel_event,
            )
            job._proxy_config = proxy_config
            job.check_cancelled()
            user = generate_user(job.phone, country=job.country)
            job.check_cancelled()
            card = generate_card(
                proxy_url=proxy_config.url,
                prefer_local=(job.country in {"AE", "CA"}),
                prefer_remote=(job.country == "BH"),
            )
            job.check_cancelled()
            if job.country in {"AE", "CA"}:
                logger.info("{} signup card source: local unique generator", job.country)
            elif job.country == "BH":
                logger.info("BH signup card source: remote Visa/MasterCard generator")
            address = generate_address(job.country)
            job.check_cancelled()
            job.set_generated(public_generated_payload(user, card, address))

            logger.info("Protocol payment job started")
            logger.info("Preparing PayPal agreement profile")

            flow_class = (
                WebIdentityElevationPayPalFlow
                if job.buyer_mode == "identity_elevation"
                else WebPayPalFlow
            )
            logger.info(
                "Buyer mode: {}",
                "identity_elevation" if job.buyer_mode == "identity_elevation" else "original",
            )
            flow = flow_class(
                ba_token=job.ba_token,
                user=user,
                card=card,
                address=address,
                max_card_attempts=max(job.max_card_attempts, 8 if job.country == "BH" else 1),
                proxy_config=proxy_config,
                job=job,
            )
            job._flow = flow
            result = flow.run()
            job.check_cancelled()
            if ENABLE_PAY153_BRIDGE and isinstance(result, dict) and result.get("status") == "success":
                try:
                    with httpx.Client(timeout=httpx.Timeout(180.0), trust_env=False) as client:
                        context_response = client.post(
                            f"{PAY153_INTERNAL_BASE}/api/grok-trial/braintree-context",
                            json={"billing_token": job.ba_token},
                        )
                        context_data = context_response.json() if context_response.status_code == 200 else {}
                        context = context_data.get("result") if isinstance(context_data, dict) else {}
                        if isinstance(context, dict) and context.get("account_id"):
                            result["billing_country"] = str(context.get("region") or job.country).upper()
                            payer_id = str(result.get("user_id") or "")
                            if not payer_id:
                                raise RuntimeError("Braintree Vault 完成阶段缺少 PayPal payer ID")
                            paypal_return = str(result.get("return_url") or result.get("final_redirect_url") or "")
                            if "/checkoutnow/error" in paypal_return:
                                result.update({
                                    "status": "error",
                                    "error_code": "PAYPAL_FUNDING_SOURCE_UNAVAILABLE",
                                    "error": "PayPal 协议已授权，但账户没有可供 Braintree Vault 使用的有效付款方式",
                                    "settlement_status": "vault_failed",
                                    "paypal_authorized": True,
                                })
                                job.complete(result)
                                return
                            job.set_status("running", "PayPal 已授权；服务器正在生成 Braintree nonce")
                            logger.info("Registered Grok Braintree agreement detected; starting server-side tokenize")
                            complete_response = client.post(
                                f"{PAY153_INTERNAL_BASE}/api/grok-trial/braintree-complete",
                                json={
                                    "account_id": context.get("account_id"),
                                    "region": context.get("region") or job.country,
                                    "billing_token": job.ba_token,
                                    "payer_id": payer_id,
                                    "plan_id": context.get("plan_id") or "supergrok_monthly",
                                    "campaign_id": context.get("campaign_id") or "",
                                    "proxy": proxy_config.url,
                                },
                            )
                            try:
                                complete_data = complete_response.json()
                            except Exception:
                                complete_data = {}
                            if complete_response.status_code != 200 or not complete_data.get("ok"):
                                raise RuntimeError(
                                    "Braintree server completion failed: "
                                    + str(complete_data.get("error") or complete_response.text[:500])
                                )
                            braintree_result = complete_data.get("result") or {}
                            result["braintree"] = braintree_result
                            verification = braintree_result.get("verification") or {}
                            result["grok_subscription_active"] = bool(verification.get("activated"))
                            result["grok_subscription_count"] = int(verification.get("subscription_count") or 0)
                            if verification.get("activated"):
                                result["settlement_status"] = "confirmed"
                                logger.success("Grok Braintree subscription is active")
                            else:
                                result["settlement_status"] = "pending_verification"
                                logger.warning("Braintree nonce submitted; Grok subscription is still syncing")
                except Exception as bridge_error:
                    logger.error("Braintree completion bridge failed: {}", redact_text(bridge_error))
                    result.update({
                        "status": "error",
                        "error_code": "BRAINTREE_VAULT_FAILED",
                        "error": str(bridge_error),
                        "settlement_status": "vault_failed",
                        "paypal_authorized": True,
                    })
                    job.complete(result)
                    return
            job.complete(result)
        except BaseException as exc:
            if job._cancel_event.is_set():
                logger.info("Protocol payment job cancelled")
                job.mark_cancelled()
            else:
                logger.error("Protocol payment job failed: {}", redact_text(exc))
                job.fail(exc)
        finally:
            try:
                record_protocol_metric(job)
            except Exception as metric_error:
                logger.warning("Protocol metrics write failed: {}", metric_error)
            job._flow = None
            job._proxy_config = None
            job._proxy_pool = []
            job.release_execution_slot()


# ----------------------------- HTTP server -----------------------------


class WebHandler(BaseHTTPRequestHandler):
    server_version = "PayPalWebUI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter stdlib server logs
        try:
            text = fmt % args
        except Exception:
            text = fmt
        logger.debug("HTTP {}", redact_text(text))

    def client_key(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        host = forwarded or (self.client_address[0] if self.client_address else "unknown")
        return f"{host}:{self.get_device_id()}"

    def check_rate_limit(self, bucket: str, *, limit: int, window_seconds: int) -> bool:
        key = self.client_key()
        if client_rate_limit(bucket, key, limit=limit, window_seconds=window_seconds):
            return True
        self.send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
        return False

    def is_internal_auto_channel(self) -> bool:
        """Trust only direct loopback calls, never Nginx-forwarded browser calls."""
        peer = self.client_address[0] if self.client_address else ""
        forwarded = self.headers.get("X-Forwarded-For", "").strip()
        marker = self.headers.get("X-Internal-Auto-Channel", "").strip()
        return peer in {"127.0.0.1", "::1"} and not forwarded and marker == "1"

    def validate_post_request(self) -> bool:
        host = self.headers.get("Host", "")
        for header_name in ("Origin", "Referer"):
            raw = self.headers.get(header_name, "")
            if not raw:
                continue
            parsed = urlparse(raw)
            if parsed.netloc and parsed.netloc != host:
                self.send_error_json(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
                return False

        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
            return False
        content_type = self.headers.get("Content-Type", "")
        if content_length > 0 and "application/json" not in content_type.lower():
            self.send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type 必须是 application/json")
            return False
        return True

    def get_device_id(self) -> str:
        cached = getattr(self, "_device_id", "")
        if cached:
            return cached
        cookies = parse_cookie_header(self.headers.get("Cookie", ""))
        device_id = cookies.get(DEVICE_COOKIE_NAME, "").strip()
        if not DEVICE_ID_RE.fullmatch(device_id):
            device_id = uuid.uuid4().hex
            self._set_device_cookie = device_id
        self._device_id = device_id
        return device_id

    def get_authorized_job(self, job_id: str) -> WebJob | None:
        job = get_job(job_id)
        if not job or job.owner_device_id != self.get_device_id():
            self.send_error_json(HTTPStatus.NOT_FOUND, "任务不存在")
            return None
        return job

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            return self.send_json({
                "ok": True,
                "service": "paypal-agreement-protocol",
                "source_commit": "4719066ec6fd56b57a5bd9599758366836c9dc0a",
                "time": now_ts(),
            })
        if path == "/api/stats":
            return self.send_json(protocol_metrics_public())
        if path == "/api/supported-countries":
            country_path = Path(__file__).resolve().parent / "data" / "paypal_supported_countries.json"
            try:
                payload = json.loads(country_path.read_text(encoding="utf-8"))
            except Exception as exc:
                return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"country catalog read failed: {exc}")
            payload["dynamic_countries_enabled"] = ENABLE_DYNAMIC_COUNTRIES
            return self.send_json(payload)
        if path == "/api/country-fields":
            catalog_path = Path(__file__).resolve().parent / "data" / "country_discovery" / "country_field_catalog.json"
            try:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            except Exception as exc:
                return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"country field catalog read failed: {exc}")
            query = self.parse_query(parsed.query)
            country = str(query.get("country") or "").strip().upper()
            if country:
                item = catalog.get(country)
                if not item:
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "country field metadata not found")
                return self.send_json({"country": country, "fields": item})
            return self.send_json({"countries": catalog})
        if path == "/api/jobs":
            device_id = self.get_device_id()
            with JOBS_LOCK:
                prune_jobs_locked()
                jobs = sorted(
                    [job for job in JOBS.values() if job.owner_device_id == device_id],
                    key=lambda j: j.created_at,
                    reverse=True,
                )
            return self.send_json({"jobs": [j.to_dict(include_logs=False) for j in jobs]})
        if path.startswith("/api/jobs/") and path.endswith("/browser/frame"):
            parts = path.split("/")
            job_id = parts[3] if len(parts) > 3 else ""
            job = self.get_authorized_job(job_id)
            if not job:
                return
            frame = job.browser_frame()
            if not frame:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Cache-Control", "no-store")
                self.send_security_headers()
                self.end_headers()
                return
            return self.send_binary(frame, "image/jpeg")
        if path.startswith("/api/jobs/"):
            job_id = path.split("/", 3)[3]
            job = self.get_authorized_job(job_id)
            if not job:
                return
            query = self.parse_query(parsed.query)
            try:
                log_offset = int(query.get("log_offset", "0") or 0)
            except Exception:
                log_offset = 0
            return self.send_json(job.to_dict(include_logs=True, log_offset=log_offset))
        if path.startswith("/api/"):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        return self.serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self.validate_post_request():
            return
        if path == "/api/jobs":
            internal_auto = self.is_internal_auto_channel()
            if not internal_auto and not self.check_rate_limit("job_create", limit=20, window_seconds=600):
                return
            try:
                data = self.read_json()
                job = create_job(
                    owner_device_id=self.get_device_id(),
                    ba_token=data.get("ba_token") or data.get("paypal_url", ""),
                    phone=data.get("phone", ""),
                    country=data.get("country") or data.get("paypal_country") or "BR",
                    buyer_mode=data.get("buyer_mode") or "identity_elevation",
                    debug=False,
                    max_card_attempts=5,
                    # Braintree link generation is now independent from the
                    # protocol-payment job. Ignore stale browser payloads that
                    # still contain manual_funding=true, otherwise the task is
                    # paused on the PayPal review page and authorize can end in
                    # BUYER_NOT_SET.
                    manual_funding=False,
                    agreement_only=bool(data.get("agreement_only")),
                    proxy_pool=data.get("proxies") or data.get("proxy_pool"),
                    exclude_public_metrics=internal_auto,
                )
                return self.send_json({"job": job.to_dict(include_logs=False)}, status=HTTPStatus.CREATED)
            except Exception as exc:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        if path.startswith("/api/jobs/") and path.endswith("/browser/action"):
            parts = path.split("/")
            job_id = parts[3] if len(parts) > 3 else ""
            job = self.get_authorized_job(job_id)
            if not job:
                return
            try:
                data = self.read_json()
                job.browser_action(data)
                return self.send_json({"ok": True, "job": job.to_dict(include_logs=False)})
            except Exception as exc:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        if path.startswith("/api/jobs/") and path.endswith("/captcha"):
            parts = path.split("/")
            job_id = parts[3] if len(parts) > 3 else ""
            job = self.get_authorized_job(job_id)
            if not job:
                return
            try:
                data = self.read_json()
                value = str(data.get("value", "")).strip()
                job.submit_captcha(value)
                job.add_log("INFO", "Manual CAPTCHA result submitted from the web page.")
                return self.send_json({"ok": True, "job": job.to_dict(include_logs=False)})
            except Exception as exc:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            parts = path.split("/")
            job_id = parts[3] if len(parts) > 3 else ""
            job = self.get_authorized_job(job_id)
            if not job:
                return
            job.cancel()
            return self.send_json({"ok": True, "job": job.to_dict(include_logs=False)})

        if path.startswith("/api/jobs/") and path.endswith("/otp"):
            parts = path.split("/")
            job_id = parts[3] if len(parts) > 3 else ""
            job = self.get_authorized_job(job_id)
            if not job:
                return
            try:
                data = self.read_json()
                value = str(data.get("value", "")).strip()
                job.submit_input(value)
                job.add_log("INFO", "已从网页提交验证码/手机号。")
                return self.send_json({"ok": True, "job": job.to_dict(include_logs=False)})
            except Exception as exc:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        return self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")

    @staticmethod
    def parse_query(query: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in query.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            result[unquote(key)] = unquote(value)
        return result

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise ValueError("请求体太大")
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON 必须是对象")
        return data

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            rel = path.removeprefix("/static/")
            file_path = STATIC_DIR / rel
        else:
            file_path = STATIC_DIR / "index.html"

        try:
            resolved = file_path.resolve()
            resolved.relative_to(STATIC_DIR.resolve())
        except Exception:
            return self.send_error_json(HTTPStatus.FORBIDDEN, "非法路径")

        if not resolved.exists() or not resolved.is_file():
            return self.send_error_json(HTTPStatus.NOT_FOUND, "文件不存在")

        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        data = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if resolved.name == "index.html" else "public, max-age=3600")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://js.braintreegateway.com https://www.paypal.com https://www.paypalobjects.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: https://*.paypal.com https://*.paypalobjects.com https://*.braintreegateway.com; "
            "connect-src 'self' https://*.braintreegateway.com https://*.braintree-api.com https://*.paypal.com; "
            "frame-src https://*.paypal.com https://*.braintreegateway.com https://*.braintree-api.com; "
            "worker-src 'self' blob:; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self' https://*.paypal.com",
        )

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        device_cookie = getattr(self, "_set_device_cookie", "")
        if device_cookie:
            cookie_attrs = [
                f"{DEVICE_COOKIE_NAME}={device_cookie}",
                "Path=/",
                f"Max-Age={DEVICE_COOKIE_MAX_AGE}",
                "SameSite=Strict",
                "HttpOnly",
            ]
            if COOKIE_SECURE:
                cookie_attrs.append("Secure")
            self.send_header(
                "Set-Cookie",
                "; ".join(cookie_attrs),
            )
        self.end_headers()
        self.wfile.write(body)

    def send_binary(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(
        self,
        status: HTTPStatus,
        message: str,
        *,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"ok": False, "error": message}
        if code:
            payload["code"] = code
        if extra:
            payload.update(extra)
        self.send_json(payload, status=status)


def get_job(job_id: str) -> WebJob | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


class WebThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True



def main() -> None:
    parser = argparse.ArgumentParser(description="PayPal Billing Agreement Web UI Public Release")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    args = parser.parse_args()

    configure_logging()
    STATIC_DIR.mkdir(exist_ok=True)

    if PRODUCTION_MODE and not COOKIE_SECURE:
        logger.warning("生产模式建议设置 PAYPAL_WEB_COOKIE_SECURE=1，并通过 HTTPS 反向代理访问。")
    if not ALLOW_DEBUG_LOGS:
        logger.info("DEBUG 日志已在网页端关闭；设置 PAYPAL_WEB_ALLOW_DEBUG_LOGS=1 才允许显示。")
    if FULL_LOGS_ENABLED:
        logger.info("完整协议日志已写入本地文件：{}（网页端仍保持脱敏）", FULL_LOG_PATH)

    server = WebThreadingHTTPServer((args.host, args.port), WebHandler)
    url_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    logger.info("Web UI running: http://{}:{}", url_host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Web UI...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
