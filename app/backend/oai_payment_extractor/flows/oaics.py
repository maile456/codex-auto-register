from __future__ import annotations

import random
from typing import Callable
import uuid
from typing import Any

from ..checkout import (
    chatgpt_success_return_url,
    first_value_by_key,
    merge_checkout_payload,
    openai_checkout_email,
)
from ..config import (
    DEFAULT_TIMEOUT,
    OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION,
    STRIPE_VERSION_BASE,
    STRIPE_VERSION_FULL,
    normalize_payment_method,
    processor_entity_for_country,
)
from ..errors import ProtocolError
from ..models import CheckoutData, ExtractionConfig, StripeContext
from ..providers import provider_redirect_config
from ..stripe_common import (
    cs_stripe_headers,
    ensure_payment_method_offered,
    extract_redirect_to_url,
    openai_stripe_headers,
    is_paypal_ba_approval_url,
    resolve_external_redirect,
    stripe_additional_elements_params,
    stripe_context,
    stripe_deferred_intent_params,
    stripe_key,
)
from ..transport import response_json, stage_http_request


def openai_checkout_init_payload(checkout: CheckoutData) -> dict[str, Any]:
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    subtotal = total.get("subtotal") if isinstance(total.get("subtotal"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    return {
        "currency": str(state.get("currency") or checkout.get("currency") or "GBP").lower(),
        "payment_method_types": checkout.get("payment_method_types") or [],
        "custom_payment_methods": checkout.get("custom_payment_methods") or [],
        "total_summary": {
            "due": due.get("minorUnitsAmount"),
            "subtotal": subtotal.get("minorUnitsAmount"),
            "total": due.get("minorUnitsAmount"),
        },
    }


def openai_elements_session(
    stripe: Any,
    config: ExtractionConfig,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
    *,
    reuse_session: bool = False,
) -> dict[str, Any]:
    customer_secret = str(checkout.get("customer_session_client_secret") or "").strip()
    if not customer_secret:
        raise ProtocolError(502, "oaics checkout missing customer_session_client_secret")
    methods = payment_method_types(init_payload)
    ctx["payment_method_types"] = methods
    params = {
        "customer_session_client_secret": customer_secret,
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": ctx["stripe_js_id"],
        "locale": config_locale(config),
        "type": "deferred_intent",
    }
    params.update(stripe_deferred_intent_params(expected_amount(init_payload), config_currency(config), methods))
    if reuse_session and ctx.get("elements_session_id"):
        params["session_id"] = str(ctx["elements_session_id"])
    custom = init_payload.get("custom_payment_methods")
    if isinstance(custom, list):
        for index, item in enumerate(custom):
            custom_id = item.get("id") if isinstance(item, dict) else item
            if custom_id:
                params[f"custom_payment_methods[{index}]"] = str(custom_id)
    response = stage_http_request(
        stripe,
        "Stripe Elements session",
        "GET",
        "https://api.stripe.com/v1/elements/sessions",
        log,
        params=params,
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe Elements session failed: {response.text[:500]}")
    payload = response_json(response, "Stripe Elements session")
    if payload.get("session_id"):
        ctx["elements_session_id"] = str(payload["session_id"])
    if payload.get("config_id"):
        ctx["elements_session_config_id"] = str(payload["config_id"])
        ctx["config_id"] = str(payload["config_id"])
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    customer_session = customer.get("customer_session") if isinstance(customer.get("customer_session"), dict) else {}
    if customer_session.get("customer"):
        ctx["customer_id"] = str(customer_session["customer"])
    return payload


def openai_checkout_taxes(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/taxes"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    body = {
        "checkout_session_id": checkout["cs_id"],
        "checkout_email": openai_checkout_email(checkout) or billing["email"],
        "billing_country": config.country.upper(),
        "billing_name": billing["name"],
        "currency": config_currency(config).lower(),
        "processor_entity": processor,
        "billing_address": {
            "line1": billing["line1"],
            "city": billing["city"],
            "country": config.country.upper(),
            "postal_code": billing["postal_code"],
            "state": billing["state"],
        },
    }
    response = stage_http_request(
        chatgpt,
        "ChatGPT oaics checkout/taxes",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json=body,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"oaics checkout/taxes failed: {response.text[:500]}")
    payload = response_json(response, "oaics checkout/taxes")
    merge_checkout_payload(checkout, payload)
    return payload


def add_attribution(body: dict[str, str], ctx: StripeContext, prefix: str) -> None:
    values = {
        "client_session_id": ctx.get("stripe_js_id", ""),
        "merchant_integration_source": "elements",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
        "elements_session_id": ctx.get("elements_session_id", ""),
        "elements_session_config_id": ctx.get("elements_session_config_id", ""),
    }
    for key, value in values.items():
        body[f"{prefix}[{key}]"] = str(value)


def openai_confirmation_token(
    stripe: Any,
    config: ExtractionConfig,
    checkout: CheckoutData,
    billing: dict[str, str],
    ctx: StripeContext,
    payment_method: str,
    log: Any | None,
) -> str:
    runtime = OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION
    body = {
        "payment_method_data[type]": payment_method,
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][country]": config.country.upper(),
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[billing_details][phone]": billing["phone"],
        "payment_method_data[payment_user_agent]": f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(45000, 85000)),
        "payment_method_data[guid]": ctx["guid"],
        "payment_method_data[muid]": ctx["muid"],
        "payment_method_data[sid]": ctx["sid"],
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": config_currency(config).lower(),
        "client_context[mode]": "subscription",
        "set_as_default_payment_method": "false",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    add_attribution(body, ctx, "payment_method_data[client_attribution_metadata]")
    add_attribution(body, ctx, "client_attribution_metadata")
    body.update(stripe_additional_elements_params("payment_method_data[client_attribution_metadata]"))
    body.update(stripe_additional_elements_params("client_attribution_metadata"))
    for index, method in enumerate(ctx.get("payment_method_types") or []):
        body[f"client_context[payment_method_types][{index}]"] = str(method)
    if ctx.get("customer_id"):
        body["client_context[customer]"] = str(ctx["customer_id"])
    if config.stripe_hcaptcha_token:
        body["payment_method_data[radar_options][hcaptcha_token]"] = config.stripe_hcaptcha_token
    response = stage_http_request(
        stripe,
        "Stripe confirmation token",
        "POST",
        "https://api.stripe.com/v1/confirmation_tokens",
        log,
        data=body,
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe confirmation token failed: {response.text[:500]}")
    token = str(response_json(response, "Stripe confirmation token").get("id") or "")
    if not token.startswith("ctoken_"):
        raise ProtocolError(502, "Stripe confirmation token response missing ctoken_ id")
    return token


def openai_checkout_confirm(
    chatgpt: Any,
    checkout: CheckoutData,
    confirmation_token: str,
    payment_method: str,
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/confirm"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    response = stage_http_request(
        chatgpt,
        "ChatGPT oaics checkout/confirm",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={
            "checkout_session_id": checkout["cs_id"],
            "confirm_token": confirmation_token,
            "selected_payment_method_type": payment_method,
        },
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"oaics checkout/confirm failed for {payment_method}: {response.text[:500]}")
    payload = response_json(response, "oaics checkout/confirm")
    if str(payload.get("status") or "").lower() != "success" or not payload.get("client_secret"):
        raise ProtocolError(409, f"oaics checkout/confirm rejected for {payment_method} or missing client_secret")
    return payload


def openai_intent_confirm(
    stripe: Any,
    checkout: CheckoutData,
    confirmation_token: str,
    confirm_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
) -> dict[str, Any]:
    client_secret = str(confirm_payload.get("client_secret") or "").strip()
    if "_secret_" not in client_secret:
        raise ProtocolError(502, "oaics checkout/confirm returned invalid client_secret")
    intent_id = client_secret.split("_secret_", 1)[0]
    if intent_id.startswith("pi_"):
        label = "PaymentIntent"
        endpoint = f"https://api.stripe.com/v1/payment_intents/{intent_id}/confirm"
    elif intent_id.startswith("seti_"):
        label = "SetupIntent"
        endpoint = f"https://api.stripe.com/v1/setup_intents/{intent_id}/confirm"
    else:
        raise ProtocolError(502, "oaics client_secret has unsupported intent type")
    return_url = str(
        confirm_payload.get("confirm_return_url")
        or checkout.get("confirm_return_url")
        or chatgpt_success_return_url(checkout)
    )
    response = stage_http_request(
        stripe,
        f"Stripe {label} confirm",
        "POST",
        endpoint,
        log,
        data={
            "return_url": return_url,
            "confirmation_token": confirmation_token,
            "key": stripe_key(checkout),
            "_stripe_version": STRIPE_VERSION_FULL,
            "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
            "client_attribution_metadata[merchant_integration_source]": "l1",
            "client_secret": client_secret,
        },
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe {label} confirm failed: {response.text[:500]}")
    return response_json(response, f"Stripe {label} confirm")


def extract_oaics_provider(
    config: ExtractionConfig,
    chatgpt: Any,
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
    *,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, str]:
    payment_method = normalize_payment_method(config.payment_method)
    init_payload = openai_checkout_init_payload(checkout)
    ensure_payment_method_offered(init_payload, payment_method, "oaics checkout")
    ctx = stripe_context(init_payload, checkout)
    ctx.update(
        {
            "runtime_version": OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION,
            "guid": str(uuid.uuid4()) + uuid.uuid4().hex[:6],
            "muid": str(uuid.uuid4()) + uuid.uuid4().hex[:6],
            "sid": str(uuid.uuid4()) + uuid.uuid4().hex[:6],
        }
    )
    if stage_callback:
        stage_callback("elements_session")
    openai_elements_session(stripe, config, checkout, init_payload, ctx, log)
    if stage_callback:
        stage_callback("taxes")
    openai_checkout_taxes(config, chatgpt, checkout, billing, log)
    refreshed = openai_checkout_init_payload(checkout)
    ensure_payment_method_offered(refreshed, payment_method, "oaics taxes refresh")
    ctx["checkout_amount"] = expected_amount(refreshed)
    ctx["currency"] = config_currency(config).lower()
    session = checkout.get("checkout_session")
    if isinstance(session, dict) and session.get("customer"):
        ctx["customer_id"] = str(session["customer"])
    refreshed_elements = openai_elements_session(
        stripe, config, checkout, refreshed, ctx, log, reuse_session=True
    )
    ensure_payment_method_offered(refreshed_elements, payment_method, "oaics refreshed Elements session")
    if stage_callback:
        stage_callback("payment_confirmation")
    confirmation_token = openai_confirmation_token(
        stripe, config, checkout, billing, ctx, payment_method, log
    )
    confirm_payload = openai_checkout_confirm(
        chatgpt, checkout, confirmation_token, payment_method, log
    )
    intent_payload = openai_intent_confirm(
        stripe, checkout, confirmation_token, confirm_payload, ctx, log
    )
    stripe_redirect = extract_redirect_to_url(intent_payload)
    if not stripe_redirect:
        raise ProtocolError(502, "oaics intent response missing redirect_to_url")
    provider_config = provider_redirect_config(payment_method)
    if stage_callback:
        stage_callback("redirect_resolution")
    provider_url = resolve_external_redirect(
        stripe,
        stripe_redirect,
        preferred_hosts=provider_config["preferred_hosts"],
        log=log,
    )
    if payment_method == "paypal" and not is_paypal_ba_approval_url(provider_url):
        raise ProtocolError(502, "PayPal BA 链解析失败：Stripe 中转地址未返回 agreements/approve?ba_token=BA- 链接")
    url = provider_url or stripe_redirect
    return {
        "payment_method_id": str(intent_payload.get("payment_method") or ""),
        "stripe_redirect_url": stripe_redirect,
        "provider_url": url,
        str(provider_config["result_field"]): url,
    }


def payment_method_types(payload: Any) -> list[str]:
    from ..stripe_common import payment_method_types as _payment_method_types

    return _payment_method_types(payload)


def expected_amount(payload: Any) -> str:
    from ..stripe_common import expected_amount as _expected_amount

    return _expected_amount(payload)


def config_currency(config: ExtractionConfig) -> str:
    from ..config import country_config

    return country_config(config.country)[1]


def config_locale(config: ExtractionConfig) -> str:
    from ..config import country_config

    return country_config(config.country)[2]
