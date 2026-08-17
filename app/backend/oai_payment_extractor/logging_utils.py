from __future__ import annotations

import os
import re
import sys
import threading
from typing import Any, Callable
from urllib.parse import urlsplit

from loguru import logger


LogFn = Callable[[str], None]
_CONFIG_LOCK = threading.Lock()
_CONFIGURED = False


def configure_logging(
    *,
    level: str = "INFO",
    log_file: str = "",
    serialize: bool = False,
    force: bool = False,
) -> None:
    """Configure Loguru for CLI and threaded web tasks."""
    global _CONFIGURED
    with _CONFIG_LOCK:
        if _CONFIGURED and not force:
            return
        handlers: list[dict[str, Any]] = [
            {
                "sink": sys.stderr,
                "level": level.upper(),
                "enqueue": os.getenv("OPLL_LOG_ENQUEUE", "true").lower()
                in {"1", "true", "yes", "on"},
                "serialize": serialize,
                "format": "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | {extra[component]} | "
                "task={extra[task_id]} | {message}",
            }
        ]
        if log_file:
            handlers.append(
                {
                    "sink": log_file,
                    "level": level.upper(),
                    "enqueue": os.getenv("OPLL_LOG_ENQUEUE", "true").lower()
                    in {"1", "true", "yes", "on"},
                    "serialize": serialize,
                    "rotation": "10 MB",
                    "retention": "14 days",
                    "encoding": "utf-8",
                    "format": "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                    "{extra[component]} | task={extra[task_id]} | {message}",
                }
            )
        logger.configure(
            handlers=handlers,
            extra={"component": "app", "task_id": "-"},
        )
        _CONFIGURED = True


def log_context(**context: Any):
    """Return a logger carrying safe, searchable context fields."""
    return logger.bind(**context)


def stage_logger(enabled: bool, **context: Any) -> LogFn | None:
    if not enabled:
        return None

    bound_logger = logger.bind(component="payment", **context)

    def log(message: str) -> None:
        bound_logger.info(message)

    return log


def emit_log(log: LogFn | None, message: str) -> None:
    if log:
        log(message)


def safe_log_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"([a-z][a-z0-9+.-]*://)([^/@\s]+)@", r"\1***@", text, flags=re.I)
    text = re.sub(r"(Bearer\s+)[^\s,;]+", r"\1***", text, flags=re.I)
    return text if len(text) <= limit else text[:limit] + "..."


def compact_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path[:80]}"
    except Exception:
        return str(url or "")[:120]
