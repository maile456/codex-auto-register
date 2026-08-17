#!/usr/bin/env python3
"""PayPal Billing Agreement approval automation.

Usage:
    python main.py --ba-token BA-xxx --phone +5591980133818
"""
import argparse
import json
import sys
from loguru import logger

from paypal.models import generate_user, generate_card, generate_address
from paypal.flow import PayPalFlow
from paypal.proxy import build_proxy_config
from paypal.session import sanitize_for_log


def main():
    parser = argparse.ArgumentParser(
        description="PayPal Billing Agreement Approval Automation"
    )
    parser.add_argument(
        "--ba-token", required=True,
        help="Billing Agreement token (e.g. BA-3AX328361P111131W)"
    )
    parser.add_argument(
        "--phone", required=True,
        help="Phone number with country code (e.g. +5591980133818)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--max-card-attempts",
        type=int,
        default=5,
        help="Max SignUpNewMember retries with fresh generated Visa/MasterCard when addCard fails",
    )
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--proxy",
        dest="proxy_enabled",
        action="store_true",
        default=None,
        help="Enable configured 1024proxy outbound proxy for this run",
    )
    proxy_group.add_argument(
        "--no-proxy",
        dest="proxy_enabled",
        action="store_false",
        help="Disable outbound proxy for this run",
    )
    parser.add_argument(
        "--proxy-index",
        type=int,
        default=None,
        help="Use a specific configured proxy index (0-based). Default: random when proxy is enabled",
    )

    args = parser.parse_args()

    logger.remove()
    if args.debug:
        logger.add(sys.stderr, level="DEBUG")
    else:
        logger.add(sys.stderr, level="INFO")

    proxy_config = build_proxy_config(enabled=args.proxy_enabled, index=args.proxy_index)
    proxy_config.prepare()

    user = generate_user(args.phone)
    card = generate_card(proxy_url=proxy_config.url)
    address = generate_address()

    logger.info(f"User: {user.first_name} {user.last_name}")
    logger.info("Email: {}", sanitize_for_log({"email": user.email})["email"])
    logger.info("Phone: {}", sanitize_for_log({"phone": user.phone})["phone"])
    logger.info("CPF: <redacted>")
    logger.info("DOB: <redacted>")
    logger.info(
        "Card: {} exp={} cvv=<redacted>",
        sanitize_for_log({"cardNumber": card.number})["cardNumber"],
        card.expiry,
    )
    logger.info("Address generated: {}, {}-{}", address.district, address.city, address.state)
    logger.debug("Outbound transport ready")

    flow = PayPalFlow(
        ba_token=args.ba_token,
        user=user,
        card=card,
        address=address,
        max_card_attempts=args.max_card_attempts,
        proxy_config=proxy_config,
    )

    result = flow.run()

    print("\n" + "=" * 60)
    print("RESULT:")
    print(json.dumps(sanitize_for_log(result), indent=2, ensure_ascii=False))
    print("=" * 60)

    if result.get("status") == "success":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
