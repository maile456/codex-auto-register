from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from backend.hero_sms_service import HeroSmsClient, HeroSmsError


def client(responses: list[httpx.Response]) -> HeroSmsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    return HeroSmsClient("TEST_API_KEY", transport=httpx.MockTransport(handler))


def test_acquire_is_fixed_to_japan_paypal_and_enforces_max_price() -> None:
    captured: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(parse_qs(request.url.query.decode()))
        return httpx.Response(200, json={
            "activationId": "activation-1",
            "phoneNumber": "819012345678",
            "activationCost": 0.42,
        })

    service = HeroSmsClient("TEST_API_KEY", transport=httpx.MockTransport(handler))
    activation = asyncio.run(service.acquire_paypal_japan(0.5))

    assert captured["action"] == ["getNumberV2"]
    assert captured["service"] == ["ts"]
    assert captured["country"] == ["182"]
    assert captured["maxPrice"] == ["0.5"]
    assert "api_key" in captured
    assert activation.phone == "+819012345678"
    assert activation.price == 0.42


def test_status_parses_wait_and_received_code() -> None:
    service = client([
        httpx.Response(200, text="STATUS_WAIT_CODE"),
        httpx.Response(200, text="STATUS_OK:123456"),
    ])
    waiting = asyncio.run(service.status("activation-1"))
    received = asyncio.run(service.status("activation-1"))
    assert waiting.state == "waiting"
    assert received.state == "received"
    assert received.code == "123456"


def test_country_catalog_and_selected_country_are_dynamic() -> None:
    captured: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        if query.get("action") == ["getCountries"]:
            return httpx.Response(200, json={
                "62": {"id": 62, "eng": "Turkey", "visible": 1},
                "182": {"id": 182, "eng": "Japan", "visible": 1},
            })
        captured.update(query)
        return httpx.Response(200, json={
            "activationId": "activation-tr",
            "phoneNumber": "905551234567",
            "activationCost": 0.3,
        })

    service = HeroSmsClient("TEST_API_KEY", transport=httpx.MockTransport(handler))
    countries = asyncio.run(service.countries())
    activation = asyncio.run(service.acquire_paypal(62, 0.5))

    assert [(item.id, item.name) for item in countries] == [(182, "Japan"), (62, "Turkey")]
    assert captured["country"] == ["62"]
    assert activation.phone == "+905551234567"


def test_business_errors_are_stable_and_do_not_expose_key() -> None:
    service = client([httpx.Response(200, text="NO_NUMBERS")])
    with pytest.raises(HeroSmsError) as failed:
        asyncio.run(service.acquire_paypal_japan(0.2))
    assert failed.value.code == "herosms_no_numbers"
    assert "TEST_API_KEY" not in str(failed.value)


def test_balance_and_activation_status_actions() -> None:
    service = client([
        httpx.Response(200, text="ACCESS_BALANCE:12.34"),
        httpx.Response(200, text="ACCESS_CANCEL"),
        httpx.Response(200, text="ACCESS_ACTIVATION"),
    ])
    assert asyncio.run(service.balance()) == 12.34
    assert asyncio.run(service.cancel("activation-1")) == "ACCESS_CANCEL"
    assert asyncio.run(service.complete("activation-1")) == "ACCESS_ACTIVATION"
