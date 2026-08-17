"""Schema helpers for country-specific PayPal form metadata.

This module is intentionally side-effect free: it reads the discovered locale
catalog, exposes required fields and validates normalized address payloads.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "country_discovery" / "country_field_catalog.json"


@lru_cache(maxsize=1)
def load_country_catalog() -> dict[str, dict[str, Any]]:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("country field catalog must be an object")
    return {str(key).upper(): value for key, value in raw.items() if isinstance(value, dict)}


def country_schema(country: str) -> dict[str, Any]:
    code = str(country or "").strip().upper()
    schema = load_country_catalog().get(code)
    if schema is None:
        raise KeyError(code)
    return schema


def required_address_fields(country: str) -> tuple[str, ...]:
    schema = country_schema(country)
    return tuple(
        str(item.get("paypal_name"))
        for item in schema.get("address_fields", [])
        if item.get("required") and item.get("paypal_name")
    )


def validate_address(country: str, address: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors for a normalized address.

    Expected keys follow PayPal's locale metadata: line1, line2, city, state,
    and postcode. Unknown keys are ignored.
    """
    schema = country_schema(country)
    errors: list[str] = []
    for item in schema.get("address_fields", []):
        name = str(item.get("paypal_name") or "")
        if not name:
            continue
        value = str(address.get(name) or "").strip()
        if item.get("required") and not value:
            errors.append(f"{name}: required")
            continue
        if not value:
            continue
        max_length = item.get("max_length")
        min_length = item.get("min_length")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{name}: max_length={max_length}")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{name}: min_length={min_length}")
        pattern = item.get("pattern")
        if pattern:
            try:
                if re.fullmatch(str(pattern), value) is None:
                    errors.append(f"{name}: pattern mismatch")
            except re.error:
                errors.append(f"{name}: invalid catalog pattern")
    return errors


def public_country_summaries() -> list[dict[str, Any]]:
    rows = []
    for country, schema in sorted(load_country_catalog().items()):
        rows.append({
            "country": country,
            "locale": schema.get("locale"),
            "currency": schema.get("currency"),
            "calling_code": schema.get("calling_code"),
            "required_address_fields": list(required_address_fields(country)),
        })
    return rows
