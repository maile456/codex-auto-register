from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


CheckoutType = Literal["oaics", "cs"]


class CheckoutTypeCheckError(RuntimeError):
    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class CheckoutTypeResult:
    checkout_type: CheckoutType
    checked_at: datetime


def checkout_type_from_result(result: dict[str, Any]) -> CheckoutType | None:
    session_id = str(
        result.get("checkoutSessionId") or result.get("checkout_session_id") or ""
    ).strip().casefold()
    if session_id.startswith("oaics_"):
        return "oaics"
    if session_id.startswith("cs_"):
        return "cs"
    session_kind = str(
        result.get("sessionKind") or result.get("session_kind") or ""
    ).strip().casefold()
    if session_kind in {"oaics", "openai_custom_checkout"}:
        return "oaics"
    if session_kind in {"stripe_cs", "stripe_checkout"}:
        return "cs"
    return None


def parse_checkout_type_response(payload: Any) -> CheckoutTypeResult:
    if not isinstance(payload, dict):
        raise CheckoutTypeCheckError("checkout_type_response_invalid")

    def find_session_id(value: Any, depth: int = 0) -> str:
        if depth > 8:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("oaics_", "cs_")):
                return text
            return ""
        if isinstance(value, dict):
            for key in ("checkout_session_id", "session_id", "id"):
                found = find_session_id(value.get(key), depth + 1)
                if found:
                    return found
            for nested in value.values():
                found = find_session_id(nested, depth + 1)
                if found:
                    return found
        if isinstance(value, list):
            for nested in value:
                found = find_session_id(nested, depth + 1)
                if found:
                    return found
        return ""

    checkout_type = checkout_type_from_result(
        {"checkout_session_id": find_session_id(payload)}
    )
    if checkout_type is None:
        raise CheckoutTypeCheckError("checkout_session_id_missing")
    return CheckoutTypeResult(checkout_type, datetime.now(timezone.utc))
