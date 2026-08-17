from __future__ import annotations

import random
from typing import Callable
import uuid
from typing import Any

from ..checkout import (
    chatgpt_success_return_url,
    merge_checkout_payload,
    openai_checkout_email,
    update_checkout,
)
from ..config import (
    DEFAULT_TIMEOUT,
    PROVIDER_POLL_TIMEOUT_SECONDS,
    STRIPE_RUNTIME_VERSION,
    STRIPE_VERSION_BASE,
    STRIPE_VERSION_FULL,
    normalize_payment_method,
    processor_entity_for_country,
)
from ..errors import ProtocolError, ProviderRequiresApproval
from ..logging_utils import emit_log, safe_log_text
from ..models import CheckoutData, ExtractionConfig, StripeContext
from ..providers import provider_redirect_config
from ..stripe_common import (
    STRIPE_CLIENT_BETAS,
    cs_billing_address,
    cs_elements_client_params,
    cs_stripe_headers,
    ensure_payment_method_offered,
    expected_amount,
    extract_checkout_totals,
    extract_redirect_to_url,
    find_setup_intent,
    find_submission_attempt,
    is_paypal_ba_approval_url,
    resolve_external_redirect,
    stripe_additional_elements_params,
    stripe_context,
    stripe_deferred_intent_params,
    stripe_elements_options_params,
    stripe_key,
    stripe_provider_poll,
    stripe_confirm_return_url,
)
from ..transport import response_json, safe_close, set_proxy_url, stage_http_request


def cs_elements_session(
    stripe: Any,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
    *,
    reuse_session: bool = False,
) -> dict[str, Any]:
    methods = payment_method_types(init_payload) or ["card"]
    amount = ctx.get("checkout_amount") or "0"
    try:
        amount = str(int(amount))
    except (TypeError, ValueError):
        amount = "0"
    params: dict[str, str] = {
        "client_betas[0]": STRIPE_CLIENT_BETAS[0],
        "client_betas[1]": STRIPE_CLIENT_BETAS[1],
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": ctx["stripe_js_id"],
        "locale": ctx.get("locale") or "en-GB",
        "type": "deferred_intent",
        "checkout_session_id": checkout["cs_id"],
    }
    params.update(stripe_deferred_intent_params(amount, str(ctx.get("currency") or "GBP"), methods))
    if reuse_session and ctx.get("elements_session_id"):
        params["session_id"] = str(ctx["elements_session_id"])
    payment_method_configuration = first_value_by_key(init_payload, "payment_method_configuration")
    configuration_id = (
        payment_method_configuration.get("id")
        if isinstance(payment_method_configuration, dict)
        else payment_method_configuration
    )
    if configuration_id:
        params["deferred_intent[payment_method_configuration][id]"] = str(configuration_id)
    try:
        response = stage_http_request(
            stripe,
            "Stripe Elements session",
            "GET",
            "https://api.stripe.com/v1/elements/sessions",
            log,
            params=params,
            headers=cs_stripe_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        emit_log(log, f"Stripe Elements session exception: {type(exc).__name__}")
        return {}
    if response.status_code >= 400:
        emit_log(log, f"Stripe Elements session failed: {response.text[:300]}")
        return {}
    payload = response_json(response, "Stripe Elements session")
    real_session_id = payload.get("session_id") or payload.get("id")
    if real_session_id:
        ctx["elements_session_id"] = str(real_session_id)
    if payload.get("config_id"):
        ctx["elements_session_config_id"] = str(payload["config_id"])
    offered = payment_method_types(payload)
    if offered:
        ctx["payment_method_types"] = offered
    return payload


def stripe_init(config: ExtractionConfig, checkout: CheckoutData, log: Any | None, stripe: Any) -> tuple[dict[str, Any], str]:
    stripe_js_id = str(uuid.uuid4())
    common = {
        "browser_locale": config_locale(config),
        "browser_timezone": config_timezone(config),
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": config_locale(config),
        "elements_session_client[is_aggregation_expected]": "false",
        "key": stripe_key(checkout),
    }
    common.update(stripe_elements_options_params())
    last_response = None
    for version in (STRIPE_VERSION_BASE, STRIPE_VERSION_FULL):
        body = dict(common)
        body["_stripe_version"] = version
        if version == STRIPE_VERSION_FULL:
            body["elements_session_client[client_betas][0]"] = STRIPE_CLIENT_BETAS[0]
            body["elements_session_client[client_betas][1]"] = STRIPE_CLIENT_BETAS[1]
        response = stage_http_request(
            stripe,
            "Stripe payment_pages init",
            "POST",
            f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}/init",
            log,
            data=body,
            headers=cs_stripe_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        last_response = response
        if response.status_code < 400:
            return response_json(response, "Stripe init"), stripe_js_id
        if response.status_code == 400 and "beta" in (response.text or "").lower():
            continue
        break
    assert last_response is not None
    raise ProtocolError(last_response.status_code, f"Stripe init failed: {last_response.text[:500]}")


def cs_update_tax_region(
    stripe: Any,
    checkout: CheckoutData,
    ctx: StripeContext,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    data = cs_elements_client_params(ctx)
    data.update(stripe_additional_elements_params("client_attribution_metadata"))
    data.update({"key": stripe_key(checkout), "_stripe_version": STRIPE_VERSION_FULL})
    for field in ("country", "line1", "city", "postal_code", "state"):
        value = str(billing.get(field) or "").strip()
        if value:
            data[f"tax_region[{field}]"] = value
    response = stage_http_request(
        stripe,
        "Stripe tax_region",
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}",
        log,
        data=data,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        emit_log(log, f"Stripe tax_region failed: {response.text[:300]}")
        return {}
    payload = response_json(response, "Stripe tax_region")
    amount = (payload.get("total_summary") or {}).get("total")
    if amount is None:
        amount = (payload.get("total_summary") or {}).get("due")
    if amount is not None:
        ctx["checkout_amount"] = amount
    return payload


def cs_snapshot_billing(
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
) -> None:
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    try:
        response = chatgpt.post(
            "https://chatgpt.com/backend-api/payments/checkout/snapshot",
            json={
                "snapshot": {
                    "billing_address": {
                        "name": billing.get("name", ""),
                        "address": cs_billing_address(billing),
                    }
                }
            },
            headers={
                "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
                "x-openai-target-path": "/backend-api/payments/checkout/snapshot",
                "x-openai-target-route": "/backend-api/payments/checkout/snapshot",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code >= 400:
            emit_log(log, f"ChatGPT checkout/snapshot failed: {response.text[:300]}")
    except Exception as exc:
        emit_log(log, f"ChatGPT checkout/snapshot exception: {type(exc).__name__}")


def cs_checkout_taxes(
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
        "billing_address": cs_billing_address(billing, country=config.country.upper()),
    }
    response = stage_http_request(
        chatgpt,
        "ChatGPT cs_live checkout/taxes",
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
        raise ProtocolError(response.status_code, f"cs_live checkout/taxes failed: {response.text[:500]}")
    payload = response_json(response, "cs_live checkout/taxes")
    merge_checkout_payload(checkout, payload)
    return payload


def stripe_create_payment_method(
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    ctx: StripeContext,
    payment_method: str,
    log: Any | None,
) -> str:
    runtime = STRIPE_RUNTIME_VERSION
    body = {
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[phone]": billing["phone"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "type": payment_method,
        "payment_user_agent": f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": checkout["cs_id"],
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx["config_id"],
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    response = stage_http_request(
        stripe,
        "Stripe payment_methods",
        "POST",
        "https://api.stripe.com/v1/payment_methods",
        log,
        data=body,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe payment_methods failed: {response.text[:500]}")
    payment_method_id = str(response_json(response, "Stripe payment_methods").get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise ProtocolError(502, "Stripe payment_methods response missing pm_ id")
    return payment_method_id


def stripe_confirm_cs_live(
    stripe: Any,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    hosted_url: str,
    payment_method: str,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    runtime = STRIPE_RUNTIME_VERSION
    amount = ctx.get("checkout_amount") or expected_amount(init_payload)
    elements_session_id = str(ctx.get("elements_session_id") or "")
    elements_session_config_id = str(ctx.get("elements_session_config_id") or "")
    checkout_config_id = str(ctx.get("config_id") or "")
    body = {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "version": runtime,
        "expected_amount": str(amount),
        "expected_payment_method_type": payment_method,
        "return_url": stripe_confirm_return_url(checkout, hosted_url),
        "_stripe_version": STRIPE_VERSION_FULL,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_session_id]": checkout["cs_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[checkout_config_id]": checkout_config_id,
        "link_brand": "link",
        "key": stripe_key(checkout),
    }
    body.update(cs_elements_client_params(ctx))
    body.update(stripe_additional_elements_params("client_attribution_metadata"))
    body.update(
        {
            "payment_method_data[type]": payment_method,
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": billing["email"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[payment_user_agent]": f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent",
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(random.randint(45000, 120000)),
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx["stripe_js_id"],
            "payment_method_data[client_attribution_metadata][checkout_session_id]": checkout["cs_id"],
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][elements_session_id]": elements_session_id,
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": elements_session_config_id,
            "payment_method_data[client_attribution_metadata][checkout_config_id]": checkout_config_id,
        }
    )
    body.update(stripe_additional_elements_params("payment_method_data[client_attribution_metadata]"))
    if billing.get("state"):
        body["payment_method_data[billing_details][address][state]"] = billing["state"]
    consent_collection = init_payload.get("consent_collection") or {}
    if consent_collection.get("terms_of_service") not in (None, "", "none"):
        body["consent[terms_of_service]"] = "accepted"
    response = stage_http_request(
        stripe,
        "Stripe payment_pages confirm",
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}/confirm",
        log,
        data=body,
        headers=cs_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe confirm failed: {response.text[:500]}")
    return response_json(response, "Stripe confirm")


def chatgpt_approve(chatgpt: Any, checkout: CheckoutData, log: Any | None) -> None:
    path = "/backend-api/payments/checkout/approve"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    try:
        stage_http_request(
            chatgpt,
            "ChatGPT sentinel ping",
            "POST",
            "https://chatgpt.com/backend-api/sentinel/ping",
            log,
            json={},
            headers={"Referer": "https://chatgpt.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception:
        pass
    response = stage_http_request(
        chatgpt,
        "ChatGPT checkout approve",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={"checkout_session_id": checkout["cs_id"], "processor_entity": processor},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"ChatGPT approve failed: {response.text[:500]}")
    result = str(response_json(response, "ChatGPT approve").get("result") or "")
    if result == "blocked":
        raise ProtocolError(409, "ChatGPT manual approval blocked")
    if result != "approved":
        raise ProtocolError(502, f"ChatGPT approve returned unexpected result: {result or '?'}")


def provider_redirect_after_confirm(
    chatgpt: Any,
    stripe: Any,
    checkout: CheckoutData,
    confirm_payload: dict[str, Any],
    payment_method: str,
    log: Any | None,
    ctx: StripeContext | None = None,
) -> str:
    redirect = extract_redirect_to_url(confirm_payload)
    if redirect:
        return redirect
    setup_intent = find_setup_intent(confirm_payload)
    if setup_intent:
        redirect = extract_redirect_to_url(setup_intent)
        if redirect:
            return redirect
    submission = find_submission_attempt(confirm_payload)
    if str(submission.get("state") or "") == "requires_approval":
        chatgpt_approve(chatgpt, checkout, log)
        return stripe_provider_poll(stripe, checkout, payment_method, PROVIDER_POLL_TIMEOUT_SECONDS, log, ctx)
    try:
        return stripe_provider_poll(stripe, checkout, payment_method, PROVIDER_POLL_TIMEOUT_SECONDS, log, ctx)
    except ProviderRequiresApproval:
        chatgpt_approve(chatgpt, checkout, log)
        return stripe_provider_poll(stripe, checkout, payment_method, PROVIDER_POLL_TIMEOUT_SECONDS, log, ctx)


def extract_cs_live_provider(
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
    init_payload, stripe_js_id = stripe_init(config, checkout, log, stripe)
    totals = extract_checkout_totals(init_payload)
    checkout["payable_amount_minor"] = totals.get("due")
    checkout["currency"] = str(totals.get("currency") or checkout.get("currency") or "GBP").upper()
    ensure_payment_method_offered(init_payload, payment_method, "cs_live Stripe init")
    hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not hosted_url:
        raise ProtocolError(502, "cs_live Stripe init missing stripe_hosted_url")
    ctx = stripe_context(init_payload, checkout, stripe_js_id)
    initial_elements_amount = str(ctx.get("checkout_amount") or "0")
    if stage_callback:
        stage_callback("elements_session")
    elements_payload = cs_elements_session(stripe, checkout, init_payload, ctx, log)
    if elements_payload:
        ensure_payment_method_offered(elements_payload, payment_method, "cs_live Elements session")
    if stage_callback:
        stage_callback("taxes")
    cs_update_tax_region(stripe, checkout, ctx, billing, log)
    checkout["payable_amount_minor"] = ctx.get("checkout_amount")
    cs_checkout_taxes(config, chatgpt, checkout, billing, log)
    cs_snapshot_billing(chatgpt, checkout, billing, log)
    final_elements_amount = str(ctx.get("checkout_amount") or "0")
    if final_elements_amount != initial_elements_amount:
        refreshed_elements = cs_elements_session(stripe, checkout, init_payload, ctx, log, reuse_session=True)
        if refreshed_elements:
            ensure_payment_method_offered(refreshed_elements, payment_method, "cs_live refreshed Elements session")
    else:
        emit_log(log, f"CS Elements refresh skipped: amount unchanged ({final_elements_amount})")
    if stage_callback:
        stage_callback("payment_confirmation")
    payment_method_id = stripe_create_payment_method(stripe, checkout, billing, ctx, payment_method, log)
    confirm_payload = stripe_confirm_cs_live(
        stripe, checkout, init_payload, ctx, hosted_url, payment_method, billing, log
    )
    stripe_redirect = provider_redirect_after_confirm(
        chatgpt, stripe, checkout, confirm_payload, payment_method, log, ctx
    )
    if stage_callback:
        stage_callback("redirect_resolution")
    provider_config = provider_redirect_config(payment_method)
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
        "payment_method_id": payment_method_id,
        "stripe_redirect_url": stripe_redirect,
        "provider_url": url,
        str(provider_config["result_field"]): url,
    }


def payment_method_types(payload: Any) -> list[str]:
    from ..stripe_common import payment_method_types as _payment_method_types

    return _payment_method_types(payload)


def first_value_by_key(payload: Any, key: str) -> Any:
    from ..checkout import first_value_by_key as _first_value_by_key

    return _first_value_by_key(payload, key)


def config_currency(config: ExtractionConfig) -> str:
    from ..config import country_config

    return country_config(config.country)[1]


def config_locale(config: ExtractionConfig) -> str:
    from ..config import country_config

    return country_config(config.country)[2]


def config_timezone(config: ExtractionConfig) -> str:
    from ..config import country_config

    return country_config(config.country)[3]
