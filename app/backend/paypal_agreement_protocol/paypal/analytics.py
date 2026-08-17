"""PayPal Analytics, XO Logger, and Datadog RUM stubs.

These systems send telemetry/tracking data alongside the main flow.
They are not strictly required for the protocol but help avoid detection.
"""
import time
import uuid
import random
from loguru import logger
from config import USER_AGENT, SCREEN, VIEWPORT


def send_xo_logger(session, event_data: dict):
    """Send client event log to PayPal XO Logger."""
    headers = {
        "Content-Type": "application/json",
        "x-app-name": "checkoutuinodeweb",
        "Origin": "https://www.paypal.com",
    }
    try:
        session.post(
            "https://www.paypal.com/xoplatform/logger/api/logger/",
            json=event_data,
            headers=headers,
        )
    except Exception as e:
        logger.warning(f"XO Logger failed: {e}")


def send_analytics_ts(session, page_name: str, ba_token: str,
                      ec_token: str = "", user_id: str = "",
                      event: str = "im"):
    """Send PayPal Analytics tracking pixel (t.paypal.com/ts)."""
    ts = int(time.time() * 1000)
    country = str(getattr(session, "country", "BR") or "BR").upper()
    locale = str(getattr(session, "locale", "pt_BR") or "pt_BR")
    timezone_minutes = {
        "BR": "-180",
        "GB": "0",
        "US": "-300",
        "JP": "540",
        "TH": "420",
        "ID": "420",
        "AE": "240",
        "AU": "600",
        "CA": "-300",
    }.get(country, "0")
    params = {
        "v": "1.15.0",
        "t": str(ts),
        "g": timezone_minutes,
        "pgrp": "main:billing:hagrid",
        "page": page_name,
        "pgtf": "Nodejs",
        "s": "ci",
        "env": "live",
        "comp": "checkoutuinodeweb",
        "tsrce": "checkoutuinodeweb",
        "cu": "1",
        "ef_policy": "ccpa",
        "c_prefs": "T=1,P=1,F=1,type=explicit_banner",
        "pxpguid": uuid.uuid4().hex,
        "pgst": str(ts - random.randint(2000, 5000)),
        "calc": uuid.uuid4().hex[:13],
        "rsta": locale,
        "ccpg": country,
        "cnac": country,
        "flnm": "Hagrid",
        "e": event,
        "fpti_sdk_name": "pa-js",
        "cd": str(SCREEN["colorDepth"]),
        "sw": str(SCREEN["width"]),
        "sh": str(SCREEN["height"]),
        "bw": str(VIEWPORT["width"]),
        "bh": str(VIEWPORT["height"]),
        "ce": "1",
    }
    if ec_token:
        params["fltk"] = ec_token
    if user_id:
        params["cust"] = user_id
        params["party_id"] = user_id
        params["acnt"] = "personal"
        params["aver"] = "unverified"
        params["rstr"] = "unrestricted"

    try:
        session.get("https://t.paypal.com/ts", params=params)
    except Exception as e:
        logger.warning(f"Analytics ts failed: {e}")


def send_observability_emit(session, ba_token: str):
    """Send observability emit (trpc endpoint)."""
    try:
        session.post(
            f"https://www.paypal.com/pay/api/trpc/observability.handleClientEmit?token={ba_token}",
            content=b"",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.paypal.com",
            },
        )
    except Exception as e:
        logger.warning(f"Observability emit failed: {e}")


def send_weasley_log(session, ec_token: str, signup_url: str, event_names: list[str],
                     country: str = "BR", lang: str = "pt",
                     extra_payload: dict | None = None):
    """Send checkoutweb/weasley client logger events in browser-like order."""
    if not ec_token or not event_names:
        return

    now = int(time.time() * 1000)
    locale = f"{lang}_{country}"
    events = []
    for i, name in enumerate(event_names):
        payload = {
            "clientCountry": country,
            "clientLocale": locale,
            "clientTimestamp": now + i,
            "timestamp": str(now + i),
            "token": ec_token,
        }
        if extra_payload:
            payload.update(extra_payload)
        events.append({"level": "info", "event": name, "payload": payload})

    body = {
        "events": events,
        "meta": {
            "integrationData": {
                "contextId": ec_token,
                "contextType": ec_token,
                "integrationMethod": "FULLPAGE",
                "integrationType": "EC",
            }
        },
        "tracking": [],
        "metrics": [],
    }
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.paypal.com",
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        "X-App-Name": "checkoutuinodeweb_weasley",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        session.post(
            "https://www.paypal.com/xoplatform/logger/api/logger/",
            json=body,
            headers=headers,
        )
    except Exception as e:
        logger.debug(f"Weasley logger failed: {e}")
