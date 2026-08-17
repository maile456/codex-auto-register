from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .chatgpt_plan import normalize_access_token, token_claims


MAX_INPUT_CHARS = 2_000_000
MAX_TOKEN_CHARS = 16_384
MAX_TOKENS = 500
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessTokenExtractInput(ToolModel):
    rawText: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)


class ExtractedAccessToken(ToolModel):
    token: str
    preview: str
    email: str | None = None
    accountId: str | None = None
    planType: str | None = None
    expiresAt: datetime | None = None
    expired: bool | None = None


class AccessTokenExtractResult(ToolModel):
    count: int
    items: list[ExtractedAccessToken]


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _looks_like_access_token(value: str) -> bool:
    token = normalize_access_token(value)
    if not token or len(token) > MAX_TOKEN_CHARS:
        return False
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _email_from_value(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if "email" in str(key).casefold() and isinstance(item, str):
                match = EMAIL_PATTERN.search(item)
                if match:
                    return match.group(0)
        for item in value.values():
            found = _email_from_value(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _email_from_value(item)
            if found:
                return found
    elif isinstance(value, str):
        match = EMAIL_PATTERN.search(value)
        if match:
            return match.group(0)
    return None


def extract_access_tokens(raw_text: str) -> AccessTokenExtractResult:
    raw = str(raw_text or "").strip()
    if not raw:
        return AccessTokenExtractResult(count=0, items=[])

    found: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def add(value: Any, email: str | None = None) -> None:
        token = normalize_access_token(str(value or ""))
        if not _looks_like_access_token(token) or token in seen:
            return
        seen.add(token)
        found.append((token, email))

    def walk(value: Any, inherited_email: str | None = None) -> None:
        if len(found) >= MAX_TOKENS:
            return
        if isinstance(value, dict):
            email = _email_from_value(value) or inherited_email
            for key in ("accessToken", "access_token", "token"):
                if key in value:
                    add(value[key], email)
            for item in value.values():
                walk(item, email)
        elif isinstance(value, list):
            for item in value:
                walk(item, inherited_email)
        elif isinstance(value, str):
            add(value, inherited_email)
            stripped = value.strip()
            if stripped[:1] in {"{", "["}:
                try:
                    walk(json.loads(stripped), inherited_email)
                except json.JSONDecodeError:
                    pass

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        walk(parsed)
    else:
        for block in re.split(r"(?m)^\s*---\s*$", raw):
            block = block.strip()
            if not block:
                continue
            try:
                walk(json.loads(block))
            except json.JSONDecodeError:
                for line in block.splitlines():
                    add(line)

    for match in re.finditer(
        r'"(?:accessToken|access_token|token)"\s*:\s*"([^"\\]+)"', raw
    ):
        add(match.group(1), _email_from_value(raw))

    items: list[ExtractedAccessToken] = []
    for token, source_email in found[:MAX_TOKENS]:
        payload = _decode_jwt_payload(token)
        claims = token_claims(token)
        email = source_email or _email_from_value(payload)
        items.append(
            ExtractedAccessToken(
                token=token,
                preview=(f"{token[:10]}...{token[-10:]}" if len(token) > 24 else token),
                email=email,
                accountId=claims.account_id,
                planType=claims.claim_plan_type,
                expiresAt=claims.expires_at,
                expired=claims.expired,
            )
        )
    return AccessTokenExtractResult(count=len(items), items=items)
