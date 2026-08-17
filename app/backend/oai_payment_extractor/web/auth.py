from __future__ import annotations

import hmac
import json
from typing import Any

from flask import jsonify, request


PASSWORD_HEADER = "X-Workbench-Password"
PASSWORD_STORAGE_KEY = "payment_link_extractor.workbench_password"
WEBSOCKET_AUTH_TIMEOUT_SECONDS = 10


def password_matches(candidate: Any, expected: str) -> bool:
    if not expected:
        return True
    if not isinstance(candidate, str):
        return False
    return hmac.compare_digest(candidate, expected)


def unauthorized_response() -> Any:
    return jsonify({"ok": False, "error": "unauthorized"}), 401


def request_is_authorized(expected: str) -> bool:
    return password_matches(request.headers.get(PASSWORD_HEADER, ""), expected)


def websocket_auth_message(raw: Any, expected: str) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(message, dict)
        and message.get("type") == "auth"
        and password_matches(message.get("password"), expected)
    )
