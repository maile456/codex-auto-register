from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


class TotpSecretError(ValueError):
    pass


def normalize_totp_secret(value: str) -> str:
    normalized = "".join(str(value or "").split()).upper()
    if not normalized:
        raise TotpSecretError("TOTP Secret 为空")
    try:
        padding = "=" * (-len(normalized) % 8)
        base64.b32decode(normalized + padding, casefold=True)
    except (ValueError, TypeError) as exc:
        raise TotpSecretError("TOTP Secret 不是有效的 Base32") from exc
    return normalized.rstrip("=")


def generate_totp(
    secret: str,
    *,
    timestamp: float | None = None,
    period: int = 30,
    digits: int = 6,
) -> str:
    if period < 1 or digits < 6 or digits > 10:
        raise ValueError("TOTP 参数无效")
    normalized = normalize_totp_secret(secret)
    padding = "=" * (-len(normalized) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    counter = int((time.time() if timestamp is None else timestamp) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)
