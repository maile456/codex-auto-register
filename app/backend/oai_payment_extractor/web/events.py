from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Iterable

from ..logging_utils import safe_log_text


EVENT_HISTORY_SIZE = 500


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_text(value: Any, secrets: Iterable[str] = ()) -> str:
    text = safe_log_text(value, limit=1200)
    for secret in secrets:
        secret = str(secret or "")
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"(https?://[^\s:/]+:)[^@\s]+@", r"\1***@", text, flags=re.I)
    return text


def make_event(task_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": event_type,
        "task_id": task_id,
        "timestamp": utc_timestamp(),
        "data": data or {},
    }
