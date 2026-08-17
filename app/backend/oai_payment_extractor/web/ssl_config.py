from __future__ import annotations

from pathlib import Path
from typing import Mapping


def ssl_context_from_config(config: Mapping[str, object]) -> tuple[str, str] | None:
    """Return a Flask SSL context tuple when certificate files are configured."""
    cert_value = str(config.get("SSL_CERT_FILE", "") or "").strip()
    key_value = str(config.get("SSL_KEY_FILE", "") or "").strip()

    if not cert_value and not key_value:
        return None
    if not cert_value or not key_value:
        raise RuntimeError(
            "OPLL_SSL_CERT_FILE and OPLL_SSL_KEY_FILE must be configured together"
        )

    cert_file = Path(cert_value).expanduser()
    key_file = Path(key_value).expanduser()
    if not cert_file.is_file():
        raise RuntimeError(f"SSL certificate file not found: {cert_file}")
    if not key_file.is_file():
        raise RuntimeError(f"SSL key file not found: {key_file}")
    return str(cert_file), str(key_file)
