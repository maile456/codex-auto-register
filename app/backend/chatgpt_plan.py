from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote
from uuid import uuid4


ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNTS_CHECK_URL = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}"
PLAN_RESPONSE_MAX_BYTES = 524_288


class PlanCheckError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        attempt_count: int = 1,
        elapsed_ms: int = 0,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.attempt_count = max(1, attempt_count)
        self.elapsed_ms = max(0, elapsed_ms)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    account_id: str | None
    claim_plan_type: str | None
    expires_at: datetime | None
    expired: bool | None


@dataclass(frozen=True, slots=True)
class AccountPlanResult:
    checked_at: datetime
    account_id: str | None
    current_plan_type: str | None
    subscription_plan: str | None
    has_active_subscription: bool
    expires_at: datetime | None
    renews_at: datetime | None
    plus_trial_eligible: bool
    plus_trial_campaign_id: str | None
    http_status: int = 200
    attempt_count: int = 1
    elapsed_ms: int = 0


def normalize_access_token(value: str) -> str:
    token = str(value or "").strip().strip('"').strip("'")
    if token.casefold().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.casefold().startswith("bearer "):
        token = token[7:].strip()
    return token


def token_claims(value: str) -> TokenClaims:
    token = normalize_access_token(value)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return TokenClaims(None, None, None, None)
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except Exception:
        return TokenClaims(None, None, None, None)
    if not isinstance(payload, dict):
        return TokenClaims(None, None, None, None)
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        auth = {}
    raw_exp = payload.get("exp")
    expires_at = None
    expired = None
    if isinstance(raw_exp, (int, float)) and not isinstance(raw_exp, bool):
        try:
            expires_at = datetime.fromtimestamp(float(raw_exp), tz=timezone.utc)
            expired = datetime.now(timezone.utc) >= expires_at
        except (OverflowError, OSError, ValueError):
            pass
    account_id = auth.get("chatgpt_account_id")
    plan_type = auth.get("chatgpt_plan_type")
    return TokenClaims(
        str(account_id) if account_id else None,
        str(plan_type) if plan_type else None,
        expires_at,
        expired,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_accounts_check(
    data: dict[str, Any],
    *,
    access_token: str = "",
    checked_at: datetime | None = None,
) -> AccountPlanResult:
    claims = token_claims(access_token)
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise PlanCheckError("plan_response_accounts_missing")

    item: dict[str, Any] | None = None
    account_key: str | None = None
    if claims.account_id and isinstance(accounts.get(claims.account_id), dict):
        item = accounts[claims.account_id]
        account_key = claims.account_id
    elif isinstance(accounts.get("default"), dict):
        item = accounts["default"]
        account_key = "default"
    else:
        for key, value in accounts.items():
            if key != "default" and isinstance(value, dict):
                item = value
                account_key = str(key)
                break
    if item is None:
        raise PlanCheckError("plan_response_account_missing")

    account = item.get("account")
    entitlement = item.get("entitlement")
    campaigns = item.get("eligible_promo_campaigns")
    if not isinstance(account, dict):
        account = {}
    if not isinstance(entitlement, dict):
        entitlement = {}
    if not isinstance(campaigns, dict):
        campaigns = {}
    plus_campaign = campaigns.get("plus")
    if not isinstance(plus_campaign, dict):
        plus_campaign = None

    raw_plan_type = account.get("plan_type") or claims.claim_plan_type
    raw_subscription_plan = entitlement.get("subscription_plan")
    plan_type = str(raw_plan_type) if raw_plan_type else None
    subscription_plan = (
        str(raw_subscription_plan) if raw_subscription_plan else None
    )
    is_free = (plan_type or "").casefold() == "free" or (
        subscription_plan or ""
    ).casefold() == "chatgptfreeplan"
    account_id = account.get("account_id")
    campaign_id = plus_campaign.get("id") if plus_campaign else None
    return AccountPlanResult(
        checked_at=(checked_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
        account_id=(
            str(account_id)
            if account_id
            else claims.account_id or account_key
        ),
        current_plan_type=plan_type,
        subscription_plan=subscription_plan,
        has_active_subscription=bool(
            entitlement.get("has_active_subscription")
        ),
        expires_at=_parse_datetime(entitlement.get("expires_at")),
        renews_at=_parse_datetime(entitlement.get("renews_at")),
        plus_trial_eligible=bool(is_free and plus_campaign),
        plus_trial_campaign_id=str(campaign_id) if campaign_id else None,
    )


def plan_request_headers(
    access_token: str,
    *,
    language: str = "zh-CN",
    device_id: str | None = None,
) -> dict[str, str]:
    token = normalize_access_token(access_token)
    if not token:
        raise PlanCheckError("access_token_missing")
    return {
        "accept": "application/json",
        "authorization": f"Bearer {token}",
        "oai-device-id": str(device_id or uuid4()),
        "oai-language": str(language or "zh-CN"),
        "referer": "https://chatgpt.com/",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_PATH,
    }


def _retryable_status(status: int) -> bool:
    return status in {408, 409, 425, 429} or status >= 500


def check_account_plan_curl(
    access_token: str,
    *,
    proxy_url: str,
    timezone_offset_min: str = "-480",
    timeout_seconds: float = 15.0,
    max_attempts: int = 2,
    retry_delay_seconds: float = 1.5,
    session_factory: Callable[[], Any] | None = None,
) -> AccountPlanResult:
    token = normalize_access_token(access_token)
    if not token:
        raise PlanCheckError("access_token_missing")
    claims = token_claims(token)
    if claims.expired is True:
        raise PlanCheckError("access_token_expired")
    if not str(proxy_url or "").strip():
        raise PlanCheckError("plan_proxy_required")

    attempts = max(1, min(4, int(max_attempts)))
    timeout_value = max(1.0, min(60.0, float(timeout_seconds)))
    started = time.monotonic()
    last_error: PlanCheckError | None = None
    url = (
        f"{ACCOUNTS_CHECK_URL}?timezone_offset_min="
        f"{quote(str(timezone_offset_min))}"
    )
    for attempt in range(1, attempts + 1):
        session = None
        response = None
        try:
            if session_factory is None:
                from curl_cffi.requests import Session

                session = Session(impersonate="chrome")
            else:
                session = session_factory()
            session.proxies = {"http": proxy_url, "https": proxy_url}
            response = session.get(
                url,
                headers=plan_request_headers(token),
                allow_redirects=False,
                timeout=timeout_value,
            )
            status = int(response.status_code)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if status == 401:
                raise PlanCheckError(
                    "access_token_unauthorized",
                    http_status=status,
                    attempt_count=attempt,
                    elapsed_ms=elapsed_ms,
                )
            if not 200 <= status < 300:
                raise PlanCheckError(
                    "plan_http_failed",
                    http_status=status,
                    retryable=_retryable_status(status),
                    attempt_count=attempt,
                    elapsed_ms=elapsed_ms,
                )
            raw_body = bytes(response.content or b"")
            if len(raw_body) > PLAN_RESPONSE_MAX_BYTES:
                raise PlanCheckError(
                    "plan_response_too_large",
                    http_status=status,
                    attempt_count=attempt,
                    elapsed_ms=elapsed_ms,
                )
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise PlanCheckError(
                    "plan_response_invalid",
                    http_status=status,
                    attempt_count=attempt,
                    elapsed_ms=elapsed_ms,
                ) from None
            finally:
                raw_body = b""
            if not isinstance(payload, dict):
                raise PlanCheckError(
                    "plan_response_invalid",
                    http_status=status,
                    attempt_count=attempt,
                    elapsed_ms=elapsed_ms,
                )
            parsed = parse_accounts_check(payload, access_token=token)
            return replace(
                parsed,
                http_status=status,
                attempt_count=attempt,
                elapsed_ms=elapsed_ms,
            )
        except PlanCheckError as exc:
            last_error = exc
        except Exception:
            last_error = PlanCheckError(
                "plan_request_failed",
                retryable=True,
                attempt_count=attempt,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

        if last_error is None or not last_error.retryable or attempt >= attempts:
            break
        delay = max(0.0, min(30.0, retry_delay_seconds * attempt))
        retry_after = None
        if response is not None:
            try:
                retry_after = response.headers.get("retry-after")
            except Exception:
                retry_after = None
        if retry_after is not None:
            try:
                delay = max(0.0, min(30.0, float(retry_after)))
            except (TypeError, ValueError):
                pass
        if delay:
            time.sleep(delay)

    raise last_error or PlanCheckError("plan_request_failed")
