from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from .application import extract_payment_link
from .config import SUPPORTED_COUNTRIES
from .web.env import load_configured_env, load_env_file
from .logging_utils import configure_logging, safe_log_text
from .models import ExtractionConfig

SUPPORTED_PAYMENT_METHODS = ("paypal", "gopay", "gcash")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract PayPal, GoPay, or GCash links from cs_* and oaics_* checkouts."
    )
    parser.add_argument("--env-file", help="path to a .env file")
    parser.add_argument("--at", default=os.getenv("OPLL_AT", ""), help="OpenAI AT; prefer OPLL_AT env")
    parser.add_argument(
        "--checkout-proxy",
        default=os.getenv("OPLL_CHECKOUT_PROXY", ""),
        help="checkout + Stripe/provider proxy",
    )
    parser.add_argument(
        "--update-proxy",
        default=os.getenv("OPLL_UPDATE_PROXY", ""),
        help="checkout/update promo proxy (long_link)",
    )
    parser.add_argument(
        "--stripe-hcaptcha-token",
        default=os.getenv("OPLL_STRIPE_HCAPTCHA_TOKEN", ""),
        help="optional current Stripe Elements passive captcha token",
    )
    parser.add_argument("--quiet", action="store_true", help="only print final JSON")
    parser.add_argument(
        "--country",
        type=str.upper,
        choices=SUPPORTED_COUNTRIES,
        default=os.getenv("OPLL_COUNTRY", "GB").upper(),
        help="billing country",
    )
    parser.add_argument(
        "--payment-method",
        choices=SUPPORTED_PAYMENT_METHODS,
        default=os.getenv("OPLL_PAYMENT_METHOD", "paypal"),
        help="payment method (default: paypal)",
    )
    parser.add_argument(
        "--update-checkout",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("OPLL_UPDATE_CHECKOUT", True),
        help="run checkout/update before payment extraction",
    )
    return parser.parse_args()


def main() -> int:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--env-file")
    bootstrap_args, _ = bootstrap.parse_known_args()
    if bootstrap_args.env_file:
        load_env_file(bootstrap_args.env_file, required=True)
        os.environ["OPLL_ENV_FILE"] = bootstrap_args.env_file
    else:
        load_configured_env()
    args = parse_args()
    configure_logging(
        level=os.getenv("OPLL_LOG_LEVEL", "INFO"),
        log_file=os.getenv("OPLL_LOG_FILE", ""),
        serialize=os.getenv("OPLL_LOG_JSON", "false").lower() in {"1", "true", "yes"},
    )
    token = str(args.at or "").strip() or getpass.getpass("OpenAI AT: ").strip()
    try:
        result = extract_payment_link(
            ExtractionConfig(
                access_token=token,
                checkout_proxy=args.checkout_proxy,
                update_proxy=args.update_proxy,
                stripe_hcaptcha_token=args.stripe_hcaptcha_token,
                country=args.country,
                payment_method=args.payment_method,
                apply_checkout_update=args.update_checkout,
                verbose=not args.quiet,
            )
        )
    except Exception as exc:
        error = getattr(exc, "detail", None) or str(exc)
        print(
            json.dumps({"ok": False, "error": safe_log_text(error, 800)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
