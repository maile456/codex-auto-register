"""Resolve and normalize country form metadata during a protocol task."""
from __future__ import annotations
import re
from typing import Any
from paypal.graphql import GRIFFIN_METADATA_QUERY


def normalize_locale_metadata(country: str, language: str, result: Any) -> dict[str, Any]:
    obj = result[0] if isinstance(result, list) and result else result
    obj = obj if isinstance(obj, dict) else {}
    metadata = (obj.get("data") or {}).get("localeMetadata") or {}
    layout = (metadata.get("address") or {}).get("layout") or []
    address_fields = []
    for item in layout:
        if not isinstance(item, dict):
            continue
        address_fields.append({
            "paypal_name": item.get("name"),
            "required": bool(item.get("isRequired")),
            "max_length": item.get("maxLength"),
            "min_length": item.get("minLength"),
            "pattern": item.get("regex"),
        })
    phone = metadata.get("phone") or {}
    return {
        "country": country.upper(),
        "language": language,
        "currency": metadata.get("currencyCode"),
        "address_fields": address_fields,
        "phone_mask": (phone.get("masks") or {}).get("mobile"),
        "phone_pattern": (phone.get("patterns") or {}).get("default"),
        "source": "paypal_runtime_metadata",
    }


def resolve_runtime_country_schema(session, country: str, language: str = "en") -> dict[str, Any]:
    code = str(country or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise ValueError("country must be a two-letter code")
    result = session.graphql(
        "GriffinMetadataQuery",
        GRIFFIN_METADATA_QUERY,
        {"countryCode": code, "languageCode": language, "shippingCountryCode": code},
    )
    schema = normalize_locale_metadata(code, language, result)
    if not schema["address_fields"]:
        raise RuntimeError(f"PayPal returned no address metadata for {code}")
    return schema


def infer_dynamic_kyc(html: str) -> dict[str, Any]:
    """Report country-specific form controls observed in signup page data.

    These are observations, not generated identity values.  A field is marked
    required only when a nearby page-config fragment explicitly says so.
    """
    text = str(html or "")
    specs = {
        "date_of_birth": ("dateOfBirth", "DateOfBirth"),
        "nationality": ("nationality", "Nationality"),
        "identity_document_type": ("identityDocumentType", "IdentityDocumentType"),
        "identity_document_number": ("identityDocumentNumber", "IdentityDocumentNumber"),
        "occupation": ("occupation", "Occupation"),
        "middle_name": ("middleName", "MiddleName"),
        "place_of_birth": ("placeOfBirth", "PlaceOfBirth"),
        "secondary_identity_document": ("secondaryIdentityDocument", "SecondaryIdentityDocument"),
        "country_specific_first_name": ("countrySpecificFirstName",),
        "country_specific_last_name": ("countrySpecificLastName",),
        "collected_consents": ("collectedConsents", "CollectedConsent"),
    }
    fields = []
    for key, tokens in specs.items():
        positions = [text.find(token) for token in tokens if text.find(token) >= 0]
        if not positions:
            continue
        pos = min(positions)
        window = text[max(0, pos - 240): pos + 360]
        required = bool(re.search(r'\\?["\']?(?:isRequired|required)\\?["\']?\s*[:=]\s*true', window, re.I))
        fields.append({"name": key, "required": required, "source": "signup_page"})
    return {"fields": fields, "source": "signup_page_observation"}


def validate_runtime_address(schema: dict[str, Any], address: dict[str, Any]) -> list[str]:
    aliases = {"postcode": "postalCode", "postal_code": "postalCode"}
    errors: list[str] = []
    for item in schema.get("address_fields") or []:
        name = str(item.get("paypal_name") or "")
        key = aliases.get(name, name)
        value = str(address.get(key) or address.get(name) or "").strip()
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
                errors.append(f"{name}: invalid pattern")
    return errors


def augment_dynamic_kyc_from_errors(schema: dict[str, Any], errors: Any) -> list[str]:
    """Promote KYC field names explicitly returned by signup validation."""
    mapping = {
        "dateofbirth": "date_of_birth",
        "nationality": "nationality",
        "identitydocumenttype": "identity_document_type",
        "identitydocumentnumber": "identity_document_number",
        "identitydocument": "identity_document_number",
        "occupation": "occupation",
        "middlename": "middle_name",
        "placeofbirth": "place_of_birth",
        "secondaryidentitydocument": "secondary_identity_document",
        "countryspecificfirstname": "country_specific_first_name",
        "countryspecificlastname": "country_specific_last_name",
        "collectedconsents": "collected_consents",
    }
    tokens: set[str] = set()
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                tokens.add(str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            tokens.add(value)
    walk(errors)
    discovered = set()
    for token in tokens:
        compact = re.sub(r"[^a-z0-9]", "", token.lower())
        for raw, normalized in mapping.items():
            if raw == compact or raw in compact:
                discovered.add(normalized)
    kyc = schema.setdefault("kyc", {"fields": [], "source": "signup_error"})
    fields = kyc.setdefault("fields", [])
    existing = {str(item.get("name")) for item in fields if isinstance(item, dict)}
    added = []
    for name in sorted(discovered - existing):
        fields.append({"name": name, "required": True, "source": "signup_error"})
        added.append(name)
    return added


def validate_runtime_phone(schema: dict[str, Any], phone_local: str, phone_full: str) -> str:
    pattern = str(schema.get("phone_pattern") or "").strip()
    if not pattern:
        return ""
    local = re.sub(r"\D", "", str(phone_local or ""))
    full = re.sub(r"\D", "", str(phone_full or ""))
    candidates = [local, full]
    if local and not local.startswith("0"):
        candidates.append("0" + local)
    try:
        if any(re.fullmatch(pattern, value) for value in candidates if value):
            return ""
    except re.error:
        return "invalid runtime phone pattern"
    return "phone does not match PayPal runtime country pattern"
