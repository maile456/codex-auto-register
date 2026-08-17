from __future__ import annotations

import base64
import json
from typing import Any


def normalize_access_token(raw: str) -> str:
    token = str(raw or "").strip()
    if token.startswith("{") or token.startswith("["):
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            return token

        def find(item: Any) -> str:
            if isinstance(item, dict):
                for key in ("accessToken", "access_token", "token"):
                    found = str(item.get(key) or "").strip()
                    if found:
                        return found
                for nested in item.values():
                    found = find(nested)
                    if found:
                        return found
            elif isinstance(item, list):
                for nested in item:
                    found = find(nested)
                    if found:
                        return found
            return ""

        return find(value) or token
    return token


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def account_email(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict) and "@" in str(profile.get("email") or ""):
        return str(profile["email"]).strip()
    for key in ("email", "preferred_username", "upn"):
        value = str(payload.get(key) or "").strip()
        if "@" in value:
            return value
    return ""


def account_name(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        name = str(profile.get("name") or "").strip()
        if name:
            return name
    return str(payload.get("name") or "").strip()
