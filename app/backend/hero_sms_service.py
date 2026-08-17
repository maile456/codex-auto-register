from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


HEROSMS_ENDPOINT = "https://hero-sms.com/stubs/handler_api.php"
HEROSMS_JAPAN_COUNTRY = 182
HEROSMS_PAYPAL_SERVICE = "ts"


class HeroSmsError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class HeroSmsActivation:
    activation_id: str
    phone: str
    price: float | None


@dataclass(frozen=True)
class HeroSmsStatus:
    state: str
    code: str = ""
    raw_status: str = ""


@dataclass(frozen=True)
class HeroSmsCountry:
    id: int
    name: str


class HeroSmsClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        endpoint: str = HEROSMS_ENDPOINT,
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("HEROSMS_API_KEY", "")).strip()
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _request(self, action: str, **params: Any) -> tuple[str, Any | None]:
        if not self.api_key:
            raise HeroSmsError("herosms_api_key_missing", "HeroSMS API Key 未配置", 422)
        query = {"api_key": self.api_key, "action": action, **params}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                trust_env=True,
                transport=self.transport,
            ) as client:
                response = await client.get(self.endpoint, params=query)
        except httpx.HTTPError as exc:
            raise HeroSmsError("herosms_network_error", "HeroSMS 网络请求失败") from exc

        text = response.text.strip()
        data: Any | None = None
        try:
            data = response.json()
        except ValueError:
            pass
        if response.status_code >= 400:
            detail = self._error_message(data, text)
            raise HeroSmsError("herosms_http_error", detail, 502)
        return text, data

    @staticmethod
    def _error_message(data: Any | None, text: str) -> str:
        if isinstance(data, dict):
            value = data.get("message") or data.get("error") or data.get("title") or data.get("detail")
            if value:
                return f"HeroSMS: {str(value)[:240]}"
        return f"HeroSMS: {(text or '请求失败')[:240]}"

    @staticmethod
    def _business_error(text: str, data: Any | None) -> HeroSmsError | None:
        known = {
            "BAD_KEY": ("herosms_bad_key", "HeroSMS API Key 无效", 401),
            "NO_BALANCE": ("herosms_no_balance", "HeroSMS 余额不足", 409),
            "NO_NUMBERS": ("herosms_no_numbers", "HeroSMS 暂无符合价格的 PayPal 号码", 409),
            "BAD_SERVICE": ("herosms_bad_service", "HeroSMS 不支持 PayPal 服务代码", 502),
            "BAD_COUNTRY": ("herosms_bad_country", "HeroSMS 不支持所选国家", 502),
            "MAX_PRICE_EXCEEDED": ("herosms_max_price", "HeroSMS 当前号码价格超过最高价格", 409),
        }
        upper = text.strip().upper()
        if isinstance(data, dict):
            candidate = str(data.get("error") or data.get("code") or data.get("message") or "").upper()
            upper = candidate or upper
        for marker, (code, message, status) in known.items():
            if marker in upper:
                return HeroSmsError(code, message, status)
        if upper.startswith(("ERROR", "BAD_", "NO_")):
            return HeroSmsError("herosms_api_error", f"HeroSMS: {text[:180]}")
        return None

    async def balance(self) -> float:
        text, data = await self._request("getBalance")
        error = self._business_error(text, data)
        if error:
            raise error
        value = text.partition(":")[2] if text.startswith("ACCESS_BALANCE:") else ""
        if isinstance(data, dict):
            value = str(data.get("balance") or value)
        try:
            return float(value)
        except ValueError as exc:
            raise HeroSmsError("herosms_invalid_balance", "HeroSMS 返回了无效余额") from exc

    async def countries(self) -> list[HeroSmsCountry]:
        text, data = await self._request("getCountries")
        error = self._business_error(text, data)
        if error:
            raise error
        if not isinstance(data, dict):
            raise HeroSmsError("herosms_invalid_countries", "HeroSMS 返回了无效国家目录")
        countries: list[HeroSmsCountry] = []
        for key, value in data.items():
            if not isinstance(value, dict) or not bool(value.get("visible", True)):
                continue
            try:
                country_id = int(value.get("id", key))
            except (TypeError, ValueError):
                continue
            name = str(value.get("eng") or value.get("rus") or country_id).strip()
            if name:
                countries.append(HeroSmsCountry(country_id, name))
        return sorted(countries, key=lambda item: (item.name.casefold(), item.id))

    async def acquire_paypal(
        self,
        country_id: int,
        max_price: float,
    ) -> HeroSmsActivation:
        text, data = await self._request(
            "getNumberV2",
            service=HEROSMS_PAYPAL_SERVICE,
            country=int(country_id),
            maxPrice=f"{max_price:.4f}".rstrip("0").rstrip("."),
        )
        error = self._business_error(text, data)
        if error:
            raise error

        activation_id = phone = ""
        price: float | None = None
        if isinstance(data, dict):
            activation_id = str(data.get("activationId") or data.get("activation_id") or data.get("id") or "")
            phone = str(data.get("phoneNumber") or data.get("phone") or "")
            raw_price = data.get("activationCost") or data.get("activation_cost") or data.get("price")
            try:
                price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                price = None
        elif text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":")
            if len(parts) >= 3:
                activation_id, phone = parts[1], parts[2]

        digits = re.sub(r"\D", "", phone)
        if not activation_id or len(digits) < 7 or len(digits) > 15:
            raise HeroSmsError("herosms_invalid_number", "HeroSMS 未返回有效手机号")
        if price is not None and price > max_price + 0.000001:
            raise HeroSmsError("herosms_price_exceeded", "HeroSMS 返回价格超过设置的最高价格")
        return HeroSmsActivation(activation_id=activation_id, phone="+" + digits, price=price)

    async def acquire_paypal_japan(self, max_price: float) -> HeroSmsActivation:
        return await self.acquire_paypal(HEROSMS_JAPAN_COUNTRY, max_price)

    async def status(self, activation_id: str) -> HeroSmsStatus:
        text, data = await self._request("getStatus", id=activation_id)
        error = self._business_error(text, data)
        if error:
            raise error
        raw = text.strip()
        if raw.startswith("STATUS_OK:"):
            return HeroSmsStatus("received", raw.partition(":")[2].strip(), "STATUS_OK")
        mapping = {
            "STATUS_WAIT_CODE": "waiting",
            "STATUS_WAIT_RETRY": "waiting",
            "STATUS_WAIT_RESEND": "waiting",
            "STATUS_CANCEL": "cancelled",
        }
        state = mapping.get(raw, "unknown")
        return HeroSmsStatus(state, raw_status=raw[:80])

    async def set_status(self, activation_id: str, status: int) -> str:
        text, data = await self._request("setStatus", id=activation_id, status=status)
        error = self._business_error(text, data)
        if error:
            raise error
        return text[:120]

    async def cancel(self, activation_id: str) -> str:
        return await self.set_status(activation_id, 8)

    async def complete(self, activation_id: str) -> str:
        return await self.set_status(activation_id, 6)
