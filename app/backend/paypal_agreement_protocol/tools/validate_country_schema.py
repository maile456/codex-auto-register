#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paypal.country_schema import country_schema, required_address_fields, validate_address


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("country")
    parser.add_argument("--address-json", default="")
    args = parser.parse_args()
    code = args.country.upper()
    schema = country_schema(code)
    payload = {
        "country": code,
        "locale": schema.get("locale"),
        "currency": schema.get("currency"),
        "calling_code": schema.get("calling_code"),
        "required_address_fields": required_address_fields(code),
    }
    if args.address_json:
        address = json.loads(args.address_json)
        payload["address_errors"] = validate_address(code, address)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
