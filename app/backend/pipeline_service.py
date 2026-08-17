from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from .errors import MongoUnavailableError, ResourceNotFoundError
from .checkout_type import checkout_type_from_result
from .hero_sms_service import HeroSmsActivation, HeroSmsClient, HeroSmsError
from .mailbox_client import direct_mailbox_access_url
from .payment_extractor_service import (
    PaymentExtractorService,
    PaymentExtractorServiceError,
    PaymentExtractorTaskCreate,
)
from .oai_payment_extractor.config import SUPPORTED_COUNTRIES
from .paypal_agreement_service import PaypalAgreementService
from .paid_mail_service import PaidMailCheckError, check_paid_confirmation
from .resource_service import MongoResourceStore, normalize_country_code


DEFAULT_PIPELINE_COUNTRY = "JP"
TERMINAL_PAYMENT_STATES = {"completed", "failed", "cancelled"}
MAIL_CONFIRMATION_INTERVAL = timedelta(seconds=20)
MAIL_CONFIRMATION_WINDOW = timedelta(minutes=10)
JST = timezone(timedelta(hours=9), name="JST")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PipelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineSettingsUpdate(PipelineModel):
    enabled: bool = False
    extractionConcurrency: int = Field(default=1, ge=1, le=10)
    paymentConcurrency: int = Field(default=1, ge=1, le=5)
    extractionFailureRetries: int = Field(default=2, ge=0, le=10)
    paymentFailureRetries: int = Field(default=2, ge=0, le=10)
    country: str = Field(default=DEFAULT_PIPELINE_COUNTRY, min_length=2, max_length=2)
    checkoutProxy: str = Field(default="", max_length=200_000)
    updateProxy: str = Field(default="", max_length=200_000)
    protocolProxy: str = Field(default="", max_length=200_000)
    checkoutProxyCountry: str = Field(default="", max_length=2)
    updateProxyCountry: str = Field(default="", max_length=2)
    protocolProxyCountry: str = Field(default="", max_length=2)
    checkoutProxyGroup: str = Field(default="", max_length=64)
    updateProxyGroup: str = Field(default="", max_length=64)
    protocolProxyGroup: str = Field(default="", max_length=64)
    applyCheckoutUpdate: bool = True
    heroSmsEnabled: bool | None = None
    autoPaymentEnabled: bool = False
    heroSmsMaxPrice: float | None = Field(default=None, gt=0, le=100)
    heroSmsChangeNumberRetries: int | None = Field(default=None, ge=0, le=10)
    heroSmsNumberWaitSeconds: int | None = Field(default=None, ge=30, le=1200)
    heroSmsCountryId: int | None = Field(default=None, ge=0, le=9999)
    agreementAutoSmsEnabled: bool | None = None

    @field_validator("country")
    @classmethod
    def normalize_billing_country(cls, value: str) -> str:
        normalized = str(value or "").strip().upper()
        if normalized not in SUPPORTED_COUNTRIES:
            raise ValueError(
                "账单国家必须是提炼模块支持的国家：" + ", ".join(SUPPORTED_COUNTRIES)
            )
        return normalized

    @field_validator("checkoutProxy", "updateProxy", "protocolProxy")
    @classmethod
    def trim_proxy_pool(cls, value: str) -> str:
        return "\n".join(line.strip() for line in value.splitlines() if line.strip())

    @field_validator("checkoutProxyCountry", "updateProxyCountry", "protocolProxyCountry")
    @classmethod
    def normalize_proxy_country(cls, value: str) -> str:
        normalized = str(value or "").strip().upper()
        if not normalized:
            return ""
        if normalize_country_code(normalized) == "ZZ":
            raise ValueError("代理国家必须是两位国家码")
        return normalized

    @field_validator("checkoutProxyGroup", "updateProxyGroup", "protocolProxyGroup")
    @classmethod
    def normalize_proxy_group(cls, value: str) -> str:
        return " ".join(str(value or "").split())


class HeroSmsSettingsUpdate(PipelineModel):
    enabled: bool = False
    countryId: int = Field(default=182, ge=0, le=9999)
    maxPrice: float = Field(default=1.0, gt=0, le=100)
    changeNumberRetries: int = Field(default=2, ge=0, le=10)
    numberWaitSeconds: int = Field(default=120, ge=30, le=1200)
    agreementAutoSmsEnabled: bool = False


class PipelineBatchInput(PipelineModel):
    ids: list[str] = Field(default_factory=list, max_length=100)


class PipelinePaidExportInput(PipelineModel):
    ids: list[str] = Field(default_factory=list, max_length=10_000)
    query: str = Field(default="", max_length=320)
    exportState: Literal["all", "exported", "unexported"] = "all"

    @field_validator("query")
    @classmethod
    def trim_query(cls, value: str) -> str:
        return value.strip()


class PipelinePaidExportStatusInput(PipelineModel):
    ids: list[str] = Field(min_length=1, max_length=10_000)
    exported: bool


class PipelinePaidMailCheckInput(PipelineModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class SmsReceiverSettingsUpdate(PipelineModel):
    enabled: bool = False
    autoSubmit: bool = False
    baseUrl: str = Field(default="", max_length=2048)
    mailboxPublicBaseUrl: str = Field(default="", max_length=2048)

    @field_validator("baseUrl", "mailboxPublicBaseUrl")
    @classmethod
    def normalize_server_url(cls, value: str) -> str:
        normalized = str(value or "").strip().rstrip("/")
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("服务器地址必须是完整的 http:// 或 https:// URL")
        if parsed.query or parsed.fragment:
            raise ValueError("服务器地址不能包含查询参数或锚点")
        return normalized


class SmsReceiverBatchInput(PipelineModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class PipelinePaymentInput(PipelineModel):
    phone: str = Field(default="", max_length=24)
    protocolProxy: str = Field(default="", max_length=200_000)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized = re.sub(r"[\s().-]+", "", value)
        if not normalized:
            return ""
        if not re.fullmatch(r"\+?81\d{9,11}", normalized):
            raise ValueError("日本 PP 手机号必须使用 +81 国际格式")
        return normalized

    @field_validator("protocolProxy")
    @classmethod
    def trim_protocol_proxy(cls, value: str) -> str:
        return "\n".join(line.strip() for line in value.splitlines() if line.strip())


class PipelineOtpInput(PipelineModel):
    value: str = Field(min_length=1, max_length=32)

    @field_validator("value")
    @classmethod
    def trim_value(cls, value: str) -> str:
        return value.strip()


class PipelineServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AccountPipelineService:
    def __init__(
        self,
        resources: MongoResourceStore,
        extractor: PaymentExtractorService,
        agreement: PaypalAgreementService,
        hero_sms: HeroSmsClient | None = None,
        mail_checker: Any = check_paid_confirmation,
        *,
        poll_seconds: float = 1.5,
    ) -> None:
        self.resources = resources
        self.manager = resources.manager
        self.extractor = extractor
        self.agreement = agreement
        self.hero_sms = hero_sms or HeroSmsClient()
        self.mail_checker = mail_checker
        self.poll_seconds = max(0.25, poll_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._agreement_hero_tasks: set[asyncio.Task[None]] = set()
        self._sms_receiver_tasks: set[asyncio.Task[None]] = set()

    @property
    def items(self) -> Any:
        return self.manager.database["account_pipeline"]

    @property
    def settings_collection(self) -> Any:
        return self.manager.database["pipeline_settings"]

    async def _guard(self, awaitable: Any) -> Any:
        try:
            self.manager.require_online()
        except Exception:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise
        try:
            return await awaitable
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 当前不可用，请检查本机服务") from exc

    async def ensure_indexes(self) -> None:
        await self._guard(
            self.items.create_index(
                [("accountId", ASCENDING)], unique=True, name="pipeline_account_unique"
            )
        )
        await self._guard(
            self.items.create_index(
                [("updatedAt", DESCENDING)], name="pipeline_updated_desc"
            )
        )
        await self._guard(
            self.items.create_index(
                [("stage", ASCENDING), ("updatedAt", DESCENDING)],
                name="pipeline_stage_updated",
            )
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        try:
            await self.ensure_indexes()
        except MongoUnavailableError:
            pass
        self._task = asyncio.create_task(self._run_loop(), name="account-pipeline")

    async def stop(self) -> None:
        self._stop.set()
        for task in tuple(self._agreement_hero_tasks):
            task.cancel()
        if self._agreement_hero_tasks:
            await asyncio.gather(*self._agreement_hero_tasks, return_exceptions=True)
            self._agreement_hero_tasks.clear()
        for task in tuple(self._sms_receiver_tasks):
            task.cancel()
        if self._sms_receiver_tasks:
            await asyncio.gather(*self._sms_receiver_tasks, return_exceptions=True)
            self._sms_receiver_tasks.clear()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.manager.online:
                    await self.tick()
            except (MongoUnavailableError, PaymentExtractorServiceError, httpx.HTTPError):
                pass
            except Exception:
                # One malformed external response must not stop later records.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def settings(self) -> dict[str, Any]:
        document = await self._guard(self.settings_collection.find_one({"_id": "default"}))
        return self._public_settings(document or {})

    async def sms_receiver_settings(self) -> dict[str, Any]:
        document = await self._guard(self.settings_collection.find_one({"_id": "default"})) or {}
        return {
            "enabled": bool(document.get("smsReceiverEnabled", False)),
            "autoSubmit": bool(document.get("smsReceiverAutoSubmit", False)),
            "baseUrl": str(document.get("smsReceiverBaseUrl") or ""),
            "mailboxPublicBaseUrl": str(document.get("smsReceiverMailboxPublicBaseUrl") or ""),
            "updatedAt": document.get("smsReceiverUpdatedAt"),
        }

    async def update_sms_receiver_settings(
        self, payload: SmsReceiverSettingsUpdate
    ) -> dict[str, Any]:
        if payload.enabled and not payload.baseUrl:
            raise PipelineServiceError("sms_receiver_base_url_required", "启用接码机对接前必须填写服务器 API 地址")
        if payload.autoSubmit and not payload.enabled:
            raise PipelineServiceError("sms_receiver_disabled", "开启自动送出前必须先启用接码机对接")
        now = utc_now()
        await self._guard(
            self.settings_collection.update_one(
                {"_id": "default"},
                {
                    "$set": {
                        "smsReceiverEnabled": payload.enabled,
                        "smsReceiverAutoSubmit": payload.autoSubmit,
                        "smsReceiverBaseUrl": payload.baseUrl,
                        "smsReceiverMailboxPublicBaseUrl": payload.mailboxPublicBaseUrl,
                        "smsReceiverUpdatedAt": now,
                    }
                },
                upsert=True,
            )
        )
        return await self.sms_receiver_settings()

    @staticmethod
    def _sms_receiver_endpoint(base_url: str, path: str) -> str:
        return base_url.rstrip("/") + "/" + path.lstrip("/")

    @staticmethod
    def _public_mailbox_url(access_url: str, public_base_url: str) -> str:
        parsed = urlsplit(access_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PipelineServiceError("mailbox_url_invalid", "成品接码 URL 格式无效")
        is_local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not is_local:
            return access_url.strip()
        if not public_base_url:
            raise PipelineServiceError(
                "mailbox_public_base_url_required",
                "本地接码 URL 需要先配置服务器邮箱地址",
            )
        public = urlsplit(public_base_url)
        prefix = public.path.rstrip("/")
        path = prefix + "/" + parsed.path.lstrip("/")
        return urlunsplit((public.scheme, public.netloc, path, parsed.query, ""))

    async def test_sms_receiver(self) -> dict[str, Any]:
        settings = await self.sms_receiver_settings()
        base_url = str(settings.get("baseUrl") or "")
        if not base_url:
            raise PipelineServiceError("sms_receiver_base_url_required", "请先填写接码机服务器 API 地址")
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(self._sms_receiver_endpoint(base_url, "/health"))
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PipelineServiceError(
                "sms_receiver_unreachable",
                f"接码机服务器连接失败：{type(exc).__name__}",
                502,
            ) from exc
        return {
            "ok": bool(payload.get("ok")) if isinstance(payload, dict) else True,
            "service": str(payload.get("service") or "") if isinstance(payload, dict) else "",
            "baseUrl": base_url,
        }

    async def _sms_receiver_documents(
        self, ids: list[str]
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        items = await self._guard(
            self.items.find(
                {"_id": {"$in": ids}, "stage": "paid"},
                {"accountId": 1, "email": 1},
            ).to_list(length=100)
        )
        account_ids = [str(item.get("accountId") or "") for item in items]
        accounts = await self._guard(
            self.resources.accounts.find(
                {"_id": {"$in": account_ids}},
                {"email": 1, "emailAccessUrl": 1},
            ).to_list(length=100)
        )
        account_map = {str(item.get("_id") or ""): item for item in accounts}
        return [
            (item, account_map.get(str(item.get("accountId") or ""), {}))
            for item in items
        ]

    async def submit_paid_to_sms_receiver(
        self, payload: SmsReceiverBatchInput
    ) -> dict[str, Any]:
        settings = await self.sms_receiver_settings()
        if not settings.get("enabled") or not settings.get("baseUrl"):
            raise PipelineServiceError("sms_receiver_disabled", "请先保存并启用接码机服务器配置")
        documents = await self._sms_receiver_documents(payload.ids)
        semaphore = asyncio.Semaphore(3)
        base_url = str(settings["baseUrl"])
        public_base = str(settings.get("mailboxPublicBaseUrl") or "")

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            async def submit_one(
                item: dict[str, Any], account: dict[str, Any]
            ) -> dict[str, Any]:
                item_id = str(item.get("_id") or "")
                email = str(item.get("email") or account.get("email") or "").strip()
                now = utc_now()
                try:
                    access_url = direct_mailbox_access_url(
                        str(account.get("emailAccessUrl") or ""), email
                    )
                    public_url = self._public_mailbox_url(access_url, public_base)
                    async with semaphore:
                        imported = await client.post(
                            self._sms_receiver_endpoint(base_url, "/api/accounts/import"),
                            json={"source": "code_url", "text": f"{email}----{public_url}"},
                        )
                        imported.raise_for_status()
                        response = await client.post(
                            self._sms_receiver_endpoint(
                                base_url, "/api/v1/integration/accounts/submit"
                            ),
                            json={"email": email},
                        )
                        response.raise_for_status()
                        result = response.json()
                    account_status = result.get("account") if isinstance(result, dict) else {}
                    account_status = account_status if isinstance(account_status, dict) else {}
                    task = account_status.get("task") if isinstance(account_status.get("task"), dict) else {}
                    updates = {
                        "smsReceiverState": str(account_status.get("state") or "queued"),
                        "smsReceiverCredentialReady": bool(account_status.get("credential_ready")),
                        "smsReceiverPhoneVerified": bool(account_status.get("phone_verified")),
                        "smsReceiverTaskId": str(task.get("id") or "") or None,
                        "smsReceiverSubmittedAt": now,
                        "smsReceiverUpdatedAt": now,
                        "smsReceiverError": None,
                        "smsReceiverMailboxUrl": public_url,
                        "updatedAt": now,
                    }
                    await self._guard(self.items.update_one({"_id": item_id}, {"$set": updates}))
                    return {"id": item_id, "ok": True, "state": updates["smsReceiverState"]}
                except PipelineServiceError as exc:
                    error = exc.message
                except httpx.HTTPStatusError as exc:
                    error = f"接码机返回 HTTP {exc.response.status_code}"
                except (httpx.HTTPError, ValueError) as exc:
                    error = f"接码机连接失败：{type(exc).__name__}"
                await self._guard(
                    self.items.update_one(
                        {"_id": item_id},
                        {
                            "$set": {
                                "smsReceiverState": "failed",
                                "smsReceiverError": error[:300],
                                "smsReceiverUpdatedAt": now,
                                "updatedAt": now,
                            }
                        },
                    )
                )
                return {"id": item_id, "ok": False, "state": "failed", "error": error[:300]}

            results = await asyncio.gather(
                *(submit_one(item, account) for item, account in documents)
            )
        return {
            "requested": len(payload.ids),
            "processed": len(results),
            "submitted": sum(bool(item["ok"]) for item in results),
            "failed": sum(not bool(item["ok"]) for item in results),
            "items": results,
        }

    async def refresh_sms_receiver_status(
        self, payload: SmsReceiverBatchInput
    ) -> dict[str, Any]:
        settings = await self.sms_receiver_settings()
        if not settings.get("enabled") or not settings.get("baseUrl"):
            raise PipelineServiceError("sms_receiver_disabled", "请先保存并启用接码机服务器配置")
        documents = await self._sms_receiver_documents(payload.ids)
        base_url = str(settings["baseUrl"])
        semaphore = asyncio.Semaphore(5)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            async def refresh_one(item: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
                item_id = str(item.get("_id") or "")
                email = str(item.get("email") or account.get("email") or "").strip()
                now = utc_now()
                try:
                    async with semaphore:
                        response = await client.get(
                            self._sms_receiver_endpoint(
                                base_url, "/api/v1/integration/accounts/status"
                            ),
                            params={"email": email},
                        )
                        response.raise_for_status()
                        result = response.json()
                    account_status = result.get("account") if isinstance(result, dict) else {}
                    account_status = account_status if isinstance(account_status, dict) else {}
                    task = account_status.get("task") if isinstance(account_status.get("task"), dict) else {}
                    state = str(account_status.get("state") or "idle")
                    updates = {
                        "smsReceiverState": state,
                        "smsReceiverCredentialReady": bool(account_status.get("credential_ready")),
                        "smsReceiverPhoneVerified": bool(account_status.get("phone_verified")),
                        "smsReceiverTaskId": str(task.get("id") or "") or None,
                        "smsReceiverUpdatedAt": now,
                        "smsReceiverError": None,
                        "updatedAt": now,
                    }
                    await self._guard(self.items.update_one({"_id": item_id}, {"$set": updates}))
                    return {"id": item_id, "ok": True, "state": state}
                except httpx.HTTPStatusError as exc:
                    error = f"接码机返回 HTTP {exc.response.status_code}"
                except (httpx.HTTPError, ValueError) as exc:
                    error = f"接码机连接失败：{type(exc).__name__}"
                await self._guard(
                    self.items.update_one(
                        {"_id": item_id},
                        {"$set": {"smsReceiverError": error[:300], "smsReceiverUpdatedAt": now}},
                    )
                )
                return {"id": item_id, "ok": False, "state": "failed", "error": error[:300]}

            results = await asyncio.gather(
                *(refresh_one(item, account) for item, account in documents)
            )
        return {
            "requested": len(payload.ids),
            "processed": len(results),
            "ready": sum(item.get("state") == "ready" for item in results),
            "failed": sum(not bool(item["ok"]) for item in results),
            "items": results,
        }

    async def _auto_submit_paid_to_sms_receiver(self, item_id: str) -> None:
        try:
            settings = await self.sms_receiver_settings()
            if settings.get("enabled") and settings.get("autoSubmit"):
                await self.submit_paid_to_sms_receiver(SmsReceiverBatchInput(ids=[item_id]))
        except Exception:
            return

    def _public_settings(self, document: dict[str, Any]) -> dict[str, Any]:
        country = str(document.get("country") or DEFAULT_PIPELINE_COUNTRY).strip().upper()
        if country not in SUPPORTED_COUNTRIES:
            country = DEFAULT_PIPELINE_COUNTRY
        return {
            "enabled": bool(document.get("enabled", False)),
            "extractionConcurrency": int(document.get("extractionConcurrency") or 1),
            "paymentConcurrency": int(document.get("paymentConcurrency") or 1),
            "extractionFailureRetries": int(
                document.get("extractionFailureRetries")
                if document.get("extractionFailureRetries") is not None
                else 2
            ),
            "paymentFailureRetries": int(
                document.get("paymentFailureRetries")
                if document.get("paymentFailureRetries") is not None
                else 2
            ),
            "country": country,
            "checkoutProxy": str(document.get("checkoutProxy") or ""),
            "updateProxy": str(document.get("updateProxy") or ""),
            "protocolProxy": str(document.get("protocolProxy") or ""),
            "checkoutProxyCountry": str(document.get("checkoutProxyCountry") or ""),
            "updateProxyCountry": str(document.get("updateProxyCountry") or ""),
            "protocolProxyCountry": str(document.get("protocolProxyCountry") or ""),
            "checkoutProxyGroup": str(document.get("checkoutProxyGroup") or ""),
            "updateProxyGroup": str(document.get("updateProxyGroup") or ""),
            "protocolProxyGroup": str(document.get("protocolProxyGroup") or ""),
            "applyCheckoutUpdate": bool(document.get("applyCheckoutUpdate", True)),
            "heroSmsEnabled": bool(document.get("heroSmsEnabled", False)),
            "autoPaymentEnabled": bool(document.get("autoPaymentEnabled", False)),
            "heroSmsMaxPrice": float(document.get("heroSmsMaxPrice") or 1.0),
            "heroSmsChangeNumberRetries": int(
                document.get("heroSmsChangeNumberRetries")
                if document.get("heroSmsChangeNumberRetries") is not None
                else 2
            ),
            "heroSmsNumberWaitSeconds": int(document.get("heroSmsNumberWaitSeconds") or 120),
            "heroSmsCountryId": int(document.get("heroSmsCountryId") or 182),
            "agreementAutoSmsEnabled": bool(document.get("agreementAutoSmsEnabled", False)),
            "heroSmsApiKeyConfigured": self.hero_sms.configured,
            "updatedAt": document.get("updatedAt"),
        }

    async def update_settings(self, payload: PipelineSettingsUpdate) -> dict[str, Any]:
        current = await self._guard(
            self.settings_collection.find_one({"_id": "default"})
        ) or {}
        hero_enabled = (
            payload.heroSmsEnabled
            if payload.heroSmsEnabled is not None
            else bool(current.get("heroSmsEnabled", False))
        )
        if payload.enabled and not (payload.checkoutProxyCountry or payload.checkoutProxy):
            raise PipelineServiceError("checkout_proxy_required", "启用自动提链前必须配置 Checkout 代理")
        if payload.enabled and payload.applyCheckoutUpdate and not (
            payload.updateProxyCountry or payload.updateProxy
        ):
            raise PipelineServiceError("update_proxy_required", "启用 Checkout Update 时必须配置 Update 代理")
        if hero_enabled and not self.hero_sms.configured:
            raise PipelineServiceError("herosms_api_key_missing", "HeroSMS API Key 未配置")
        if payload.autoPaymentEnabled and not hero_enabled:
            raise PipelineServiceError("herosms_required", "自动支付必须启用 HeroSMS")
        if payload.autoPaymentEnabled and not (
            payload.protocolProxyCountry or payload.protocolProxy
        ):
            raise PipelineServiceError("protocol_proxy_required", "自动支付必须配置协议支付代理")
        now = utc_now()
        document = {
            "enabled": payload.enabled,
            "extractionConcurrency": payload.extractionConcurrency,
            "paymentConcurrency": payload.paymentConcurrency,
            "extractionFailureRetries": payload.extractionFailureRetries,
            "paymentFailureRetries": payload.paymentFailureRetries,
            "country": payload.country,
            "checkoutProxy": payload.checkoutProxy,
            "updateProxy": payload.updateProxy,
            "protocolProxy": payload.protocolProxy,
            "checkoutProxyCountry": payload.checkoutProxyCountry,
            "updateProxyCountry": payload.updateProxyCountry,
            "protocolProxyCountry": payload.protocolProxyCountry,
            "checkoutProxyGroup": payload.checkoutProxyGroup,
            "updateProxyGroup": payload.updateProxyGroup,
            "protocolProxyGroup": payload.protocolProxyGroup,
            "applyCheckoutUpdate": payload.applyCheckoutUpdate,
            "autoPaymentEnabled": payload.autoPaymentEnabled,
            "updatedAt": now,
        }
        optional_hero_updates = {
            "heroSmsEnabled": payload.heroSmsEnabled,
            "heroSmsMaxPrice": payload.heroSmsMaxPrice,
            "heroSmsChangeNumberRetries": payload.heroSmsChangeNumberRetries,
            "heroSmsNumberWaitSeconds": payload.heroSmsNumberWaitSeconds,
            "heroSmsCountryId": payload.heroSmsCountryId,
            "agreementAutoSmsEnabled": payload.agreementAutoSmsEnabled,
        }
        document.update(
            {key: value for key, value in optional_hero_updates.items() if value is not None}
        )
        await self._guard(
            self.settings_collection.update_one(
                {"_id": "default"}, {"$set": document}, upsert=True
            )
        )
        return self._public_settings({**current, **document})

    async def sync_eligible(self) -> dict[str, int]:
        now = utc_now()
        cursor = self.resources.accounts.find(
            {
                "promotionEligible": True,
                "accessTokenConfigured": True,
                "accessToken": {"$type": "string", "$ne": ""},
                "accessTokenExpiresAt": {"$gt": now},
            },
            {
                "email": 1,
                "createdAt": 1,
                "accountType": 1,
                "promotionCampaignId": 1,
                "planCheckedAt": 1,
                "accessTokenExpiresAt": 1,
            },
        )
        accounts = await self._guard(cursor.to_list(length=None))
        inserted = 0
        for account in accounts:
            account_id = str(account["_id"])
            result = await self._guard(
                self.items.update_one(
                    {"accountId": account_id},
                    {
                        "$setOnInsert": {
                            "_id": uuid.uuid4().hex,
                            "accountId": account_id,
                            "stage": "eligible",
                            "extractionStatus": "pending",
                            "extractionRetryCount": 0,
                            "paymentStatus": "pending",
                            "paymentRetryCount": 0,
                            "createdAt": now,
                            "updatedAt": now,
                        },
                        "$set": {
                            "email": account.get("email", ""),
                            "accountCreatedAt": account.get("createdAt"),
                            "accountType": account.get("accountType", "free"),
                            "promotionEligible": True,
                            "promotionCampaignId": account.get("promotionCampaignId"),
                            "planCheckedAt": account.get("planCheckedAt"),
                            "accessTokenConfigured": True,
                            "accessTokenExpiresAt": account.get("accessTokenExpiresAt"),
                        },
                    },
                    upsert=True,
                )
            )
            inserted += int(result.upserted_id is not None)
        return {"eligible": len(accounts), "inserted": inserted}

    async def list_items(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        stage: str = "",
        query: str = "",
        export_state: str = "all",
        settlement_state: str = "all",
    ) -> dict[str, Any]:
        mongo_query: dict[str, Any] = {}
        if stage:
            mongo_query["stage"] = stage
        if stage == "paid" and export_state == "exported":
            mongo_query["exportCount"] = {"$gt": 0}
        elif stage == "paid" and export_state == "unexported":
            mongo_query["$or"] = [
                {"exportCount": {"$exists": False}},
                {"exportCount": 0},
            ]
        if stage == "paid" and settlement_state != "all":
            settlement_values = {
                "waiting": ["waiting", "unchecked"],
                "confirmed": ["confirmed"],
                "review": ["review", "not_found"],
                "failed": ["failed"],
            }
            if settlement_state in settlement_values:
                mongo_query["mailConfirmationStatus"] = {
                    "$in": settlement_values[settlement_state]
                }
        if query.strip():
            mongo_query["email"] = {"$regex": re.escape(query.strip()), "$options": "i"}
        total = await self._guard(self.items.count_documents(mongo_query))
        cursor = (
            self.items.find(mongo_query, {"paymentDeviceId": 0})
            .sort("updatedAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self._guard(cursor.to_list(length=page_size))
        account_ids = [str(item.get("accountId") or "") for item in documents]
        account_cursor = self.resources.accounts.find(
            {"_id": {"$in": account_ids}},
            {
                "chatgptPassword": 1,
                "totpSecret": 1,
                "emailAccessUrl": 1,
            },
        )
        account_documents = await self._guard(account_cursor.to_list(length=page_size))
        accounts_by_id = {str(item.get("_id") or ""): item for item in account_documents}
        counts: dict[str, int] = {}
        aggregate = await self._guard(
            self.items.aggregate([{"$group": {"_id": "$stage", "count": {"$sum": 1}}}])
        )
        for item in await self._guard(aggregate.to_list(length=None)):
            counts[str(item.get("_id") or "unknown")] = int(item.get("count") or 0)
        return {
            "items": [
                self._public_item(item, accounts_by_id.get(str(item.get("accountId") or "")))
                for item in documents
            ],
            "total": int(total),
            "page": page,
            "pageSize": page_size,
            "counts": counts,
        }

    async def paid_stats(self, *, days: int = 14) -> dict[str, Any]:
        chart_days = max(7, min(days, 31))
        now_jst = utc_now().astimezone(JST)
        first_day = now_jst.date() - timedelta(days=chart_days - 1)
        cursor = self.items.find(
            {"stage": "paid"},
            {"paidAt": 1, "heroSmsPrice": 1, "exportCount": 1, "mailConfirmationStatus": 1},
        )
        paid_items = await self._guard(cursor.to_list(length=None))
        terminal_total = await self._guard(
            self.items.count_documents({"stage": {"$in": ["paid", "payment_failed"]}})
        )
        daily_counts = {
            first_day + timedelta(days=offset): 0 for offset in range(chart_days)
        }
        today = 0
        last_seven_days = 0
        prices: list[float] = []
        for item in paid_items:
            paid_at = item.get("paidAt")
            if isinstance(paid_at, datetime):
                if paid_at.tzinfo is None:
                    paid_at = paid_at.replace(tzinfo=timezone.utc)
                paid_day = paid_at.astimezone(JST).date()
                if paid_day == now_jst.date():
                    today += 1
                if paid_day >= now_jst.date() - timedelta(days=6):
                    last_seven_days += 1
                if paid_day in daily_counts:
                    daily_counts[paid_day] += 1
            price = item.get("heroSmsPrice")
            if isinstance(price, (int, float)) and not isinstance(price, bool):
                prices.append(float(price))
        total = len(paid_items)
        exported = sum(int(item.get("exportCount") or 0) > 0 for item in paid_items)
        mail_confirmed = sum(item.get("mailConfirmationStatus") == "confirmed" for item in paid_items)
        return {
            "total": total,
            "today": today,
            "last7Days": last_seven_days,
            "terminalTotal": int(terminal_total),
            "failed": max(0, int(terminal_total) - total),
            "successRate": round((total / terminal_total * 100) if terminal_total else 0, 1),
            "averageHeroSmsPrice": round(sum(prices) / len(prices), 4) if prices else None,
            "exported": exported,
            "unexported": max(0, total - exported),
            "mailConfirmed": mail_confirmed,
            "daily": [
                {"date": day.isoformat(), "count": count}
                for day, count in daily_counts.items()
            ],
        }

    async def export_paid(self, payload: PipelinePaidExportInput) -> dict[str, Any]:
        query: dict[str, Any] = {"stage": "paid"}
        if payload.ids:
            query["_id"] = {"$in": payload.ids}
        if payload.query:
            query["email"] = {"$regex": re.escape(payload.query), "$options": "i"}
        if payload.exportState == "exported":
            query["exportCount"] = {"$gt": 0}
        elif payload.exportState == "unexported":
            query["$or"] = [{"exportCount": {"$exists": False}}, {"exportCount": 0}]
        cursor = self.items.find(query, {"accountId": 1, "email": 1, "paidAt": 1}).sort(
            "paidAt", DESCENDING
        )
        documents = await self._guard(cursor.to_list(length=None))
        account_ids = [str(item.get("accountId") or "") for item in documents]
        account_cursor = self.resources.accounts.find(
            {"_id": {"$in": account_ids}},
            {"email": 1, "emailAccessUrl": 1},
        )
        accounts = await self._guard(account_cursor.to_list(length=None))
        access_urls = {
            str(item.get("_id") or ""): direct_mailbox_access_url(
                str(item.get("emailAccessUrl") or ""),
                str(item.get("email") or ""),
            )
            for item in accounts
        }
        lines: list[str] = []
        exported_ids: list[str] = []
        skipped_missing_url = 0
        for item in documents:
            email = str(item.get("email") or "").strip()
            access_url = access_urls.get(str(item.get("accountId") or ""), "").strip()
            if not email or not access_url:
                skipped_missing_url += 1
                continue
            lines.append(f"{email}----{access_url}")
            exported_ids.append(str(item.get("_id") or ""))
        now = utc_now()
        if exported_ids:
            await self._guard(
                self.items.update_many(
                    {
                        "_id": {"$in": exported_ids},
                        "$or": [
                            {"exportCount": {"$exists": False}},
                            {"exportCount": 0},
                        ],
                    },
                    {"$set": {"firstExportedAt": now}},
                )
            )
            await self._guard(
                self.items.update_many(
                    {"_id": {"$in": exported_ids}},
                    {
                        "$inc": {"exportCount": 1},
                        "$set": {"lastExportedAt": now, "updatedAt": now},
                    },
                )
            )
        timestamp = utc_now().astimezone(JST).strftime("%Y%m%d-%H%M%S")
        return {
            "content": "\n".join(lines),
            "filename": f"paid-accounts-{len(lines)}-mail-links-{timestamp}.txt",
            "count": len(lines),
            "skippedMissingUrlCount": skipped_missing_url,
        }

    async def mark_paid_export_status(
        self, payload: PipelinePaidExportStatusInput
    ) -> dict[str, int]:
        now = utc_now()
        query = {"_id": {"$in": payload.ids}, "stage": "paid"}
        if payload.exported:
            await self._guard(
                self.items.update_many(
                    {
                        **query,
                        "$or": [
                            {"exportCount": {"$exists": False}},
                            {"exportCount": 0},
                        ],
                    },
                    {
                        "$set": {
                            "exportCount": 1,
                            "firstExportedAt": now,
                            "lastExportedAt": now,
                            "updatedAt": now,
                        }
                    },
                )
            )
            result = await self._guard(
                self.items.update_many(
                    {**query, "exportCount": {"$gt": 0}},
                    {"$set": {"lastExportedAt": now, "updatedAt": now}},
                )
            )
        else:
            result = await self._guard(
                self.items.update_many(
                    query,
                    {
                        "$set": {
                            "exportCount": 0,
                            "firstExportedAt": None,
                            "lastExportedAt": None,
                            "updatedAt": now,
                        }
                    },
                )
            )
        return {"updated": int(result.matched_count)}

    async def check_paid_mail(self, payload: PipelinePaidMailCheckInput) -> dict[str, Any]:
        cursor = self.items.find(
            {"_id": {"$in": payload.ids}, "stage": "paid"},
            {
                "accountId": 1,
                "email": 1,
                "paidAt": 1,
                "mailConfirmationStatus": 1,
                "mailConfirmationSubject": 1,
                "mailConfirmationReceivedAt": 1,
                "mailConfirmationOrderId": 1,
            },
        )
        documents = await self._guard(cursor.to_list(length=100))
        account_ids = [str(item.get("accountId") or "") for item in documents]
        accounts = await self._guard(
            self.resources.accounts.find(
                {"_id": {"$in": account_ids}}, {"emailAccessUrl": 1}
            ).to_list(length=100)
        )
        urls = {
            str(item.get("_id") or ""): str(item.get("emailAccessUrl") or "")
            for item in accounts
        }
        semaphore = asyncio.Semaphore(3)
        reset_at = utc_now()
        reset_deadline = reset_at + MAIL_CONFIRMATION_WINDOW

        async def check_one(item: dict[str, Any]) -> dict[str, str]:
            item_id = str(item.get("_id") or "")
            url = urls.get(str(item.get("accountId") or ""), "").strip()
            now = utc_now()
            paid_at = item.get("paidAt")
            if not isinstance(paid_at, datetime):
                paid_at = now
            elif paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            if not url:
                updates = {
                    "mailConfirmationStatus": "failed",
                    "mailConfirmationError": "mailbox_url_missing",
                    "mailConfirmationCheckedAt": now,
                    "mailConfirmationNextCheckAt": None,
                    "mailConfirmationDeadline": reset_deadline,
                    "updatedAt": now,
                }
            else:
                try:
                    async with semaphore:
                        result = await asyncio.to_thread(
                            self.mail_checker,
                            url,
                            str(item.get("email") or ""),
                            paid_at,
                        )
                    confirmed = result.status == "confirmed"
                    already_confirmed = item.get("mailConfirmationStatus") == "confirmed"
                    updates = {
                        "mailConfirmationStatus": "confirmed" if confirmed or already_confirmed else "waiting",
                        "mailConfirmationSubject": (
                            result.subject if confirmed or not already_confirmed
                            else item.get("mailConfirmationSubject")
                        ),
                        "mailConfirmationReceivedAt": (
                            result.received_at if confirmed or not already_confirmed
                            else item.get("mailConfirmationReceivedAt")
                        ),
                        "mailConfirmationOrderId": (
                            result.order_id if confirmed or not already_confirmed
                            else item.get("mailConfirmationOrderId")
                        ),
                        "mailConfirmationError": None if confirmed or already_confirmed else result.error_code,
                        "mailConfirmationCheckedAt": now,
                        "mailConfirmationStartedAt": reset_at,
                        "mailConfirmationDeadline": reset_deadline,
                        "mailConfirmationNextCheckAt": None if confirmed or already_confirmed else now + MAIL_CONFIRMATION_INTERVAL,
                        "updatedAt": now,
                    }
                except PaidMailCheckError as exc:
                    updates = {
                        "mailConfirmationStatus": "failed",
                        "mailConfirmationError": str(exc)[:300],
                        "mailConfirmationCheckedAt": now,
                        "mailConfirmationStartedAt": reset_at,
                        "mailConfirmationDeadline": reset_deadline,
                        "mailConfirmationNextCheckAt": now + MAIL_CONFIRMATION_INTERVAL,
                        "updatedAt": now,
                    }
                except Exception as exc:
                    updates = {
                        "mailConfirmationStatus": "failed",
                        "mailConfirmationError": type(exc).__name__,
                        "mailConfirmationCheckedAt": now,
                        "mailConfirmationStartedAt": reset_at,
                        "mailConfirmationDeadline": reset_deadline,
                        "mailConfirmationNextCheckAt": now + MAIL_CONFIRMATION_INTERVAL,
                        "updatedAt": now,
                    }
            updates["mailConfirmationAttempt"] = 1
            await self._guard(self.items.update_one({"_id": item_id}, {"$set": updates}))
            return {"id": item_id, "status": str(updates["mailConfirmationStatus"])}

        results = await asyncio.gather(*(check_one(item) for item in documents))
        return {
            "requested": len(payload.ids),
            "checked": len(results),
            "confirmed": sum(item["status"] == "confirmed" for item in results),
            "waiting": sum(item["status"] == "waiting" for item in results),
            "review": sum(item["status"] == "review" for item in results),
            "notFound": sum(item["status"] in {"waiting", "review"} for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "items": results,
        }

    async def _reconcile_paid_confirmations(self) -> None:
        now = utc_now()
        recent_cutoff = now - timedelta(minutes=30)
        recent_cursor = self.items.find(
            {
                "stage": "paid",
                "paidAt": {"$gte": recent_cutoff},
                "$or": [
                    {"mailConfirmationStatus": {"$exists": False}},
                    {"mailConfirmationStatus": "unchecked"},
                ],
            },
            {"paidAt": 1},
        ).limit(100)
        for item in await self._guard(recent_cursor.to_list(length=100)):
            paid_at = item.get("paidAt")
            if not isinstance(paid_at, datetime):
                paid_at = now
            elif paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            await self._guard(
                self.items.update_one(
                    {"_id": item["_id"], "mailConfirmationStatus": {"$in": [None, "unchecked"]}},
                    {
                        "$set": {
                            "mailConfirmationStatus": "waiting",
                            "mailConfirmationStartedAt": paid_at,
                            "mailConfirmationDeadline": paid_at + MAIL_CONFIRMATION_WINDOW,
                            "mailConfirmationNextCheckAt": now,
                            "mailConfirmationAttempt": 0,
                            "mailConfirmationError": None,
                            "updatedAt": now,
                        }
                    },
                )
            )

        cursor = self.items.find(
            {
                "stage": "paid",
                "mailConfirmationStatus": {"$in": ["waiting", "failed"]},
                "mailConfirmationNextCheckAt": {"$lte": now},
            },
            {"accountId": 1, "email": 1, "paidAt": 1, "mailConfirmationDeadline": 1},
        ).sort("mailConfirmationNextCheckAt", ASCENDING).limit(12)
        documents = await self._guard(cursor.to_list(length=12))
        if not documents:
            return
        account_ids = [str(item.get("accountId") or "") for item in documents]
        accounts = await self._guard(
            self.resources.accounts.find(
                {"_id": {"$in": account_ids}}, {"emailAccessUrl": 1}
            ).to_list(length=12)
        )
        urls = {
            str(item.get("_id") or ""): str(item.get("emailAccessUrl") or "")
            for item in accounts
        }
        semaphore = asyncio.Semaphore(3)

        async def reconcile_one(item: dict[str, Any]) -> None:
            item_id = str(item.get("_id") or "")
            url = urls.get(str(item.get("accountId") or ""), "").strip()
            checked_at = utc_now()
            paid_at = item.get("paidAt")
            if not isinstance(paid_at, datetime):
                paid_at = checked_at
            elif paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            deadline = item.get("mailConfirmationDeadline")
            if not isinstance(deadline, datetime):
                deadline = paid_at + MAIL_CONFIRMATION_WINDOW
            elif deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            updates: dict[str, Any]
            if not url:
                updates = {
                    "mailConfirmationStatus": "failed",
                    "mailConfirmationError": "mailbox_url_missing",
                    "mailConfirmationNextCheckAt": None,
                }
            else:
                try:
                    async with semaphore:
                        result = await asyncio.to_thread(
                            self.mail_checker,
                            url,
                            str(item.get("email") or ""),
                            paid_at,
                        )
                    if result.status == "confirmed":
                        updates = {
                            "mailConfirmationStatus": "confirmed",
                            "mailConfirmationSubject": result.subject,
                            "mailConfirmationReceivedAt": result.received_at,
                            "mailConfirmationOrderId": result.order_id,
                            "mailConfirmationError": None,
                            "mailConfirmationNextCheckAt": None,
                        }
                    else:
                        timed_out = checked_at >= deadline
                        updates = {
                            "mailConfirmationStatus": "review" if timed_out else "waiting",
                            "mailConfirmationSubject": result.subject,
                            "mailConfirmationError": "confirmation_timeout" if timed_out else result.error_code,
                            "mailConfirmationNextCheckAt": None if timed_out else checked_at + MAIL_CONFIRMATION_INTERVAL,
                        }
                except PaidMailCheckError as exc:
                    updates = {
                        "mailConfirmationStatus": "failed",
                        "mailConfirmationError": str(exc)[:300],
                        "mailConfirmationNextCheckAt": None if checked_at >= deadline else checked_at + MAIL_CONFIRMATION_INTERVAL,
                    }
                except Exception as exc:
                    updates = {
                        "mailConfirmationStatus": "failed",
                        "mailConfirmationError": type(exc).__name__,
                        "mailConfirmationNextCheckAt": None if checked_at >= deadline else checked_at + MAIL_CONFIRMATION_INTERVAL,
                    }
            updates.update(
                {
                    "mailConfirmationCheckedAt": checked_at,
                    "mailConfirmationDeadline": deadline,
                    "updatedAt": checked_at,
                }
            )
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {"$set": updates, "$inc": {"mailConfirmationAttempt": 1}},
                )
            )

        await asyncio.gather(*(reconcile_one(item) for item in documents))

    def _public_item(
        self,
        item: dict[str, Any],
        account: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = account or {}
        return {
            "id": str(item.get("_id") or ""),
            "accountId": str(item.get("accountId") or ""),
            "email": str(item.get("email") or ""),
            "chatgptPassword": str(account.get("chatgptPassword") or ""),
            "totpSecret": str(account.get("totpSecret") or ""),
            "emailAccessUrl": direct_mailbox_access_url(
                str(account.get("emailAccessUrl") or ""),
                str(account.get("email") or item.get("email") or ""),
            ),
            "accountCreatedAt": item.get("accountCreatedAt"),
            "accountType": item.get("accountType", "free"),
            "promotionEligible": item.get("promotionEligible") is True,
            "promotionCampaignId": item.get("promotionCampaignId"),
            "planCheckedAt": item.get("planCheckedAt"),
            "accessTokenConfigured": item.get("accessTokenConfigured") is True,
            "accessTokenExpiresAt": item.get("accessTokenExpiresAt"),
            "stage": item.get("stage", "eligible"),
            "extractionStatus": item.get("extractionStatus", "pending"),
            "extractionRetryCount": int(item.get("extractionRetryCount") or 0),
            "extractorTaskId": item.get("extractorTaskId"),
            "extractionError": item.get("extractionError"),
            "billingCountry": item.get("billingCountry"),
            "extractedAt": item.get("extractedAt"),
            "checkoutType": item.get("checkoutType") or account.get("checkoutType"),
            "checkoutTypeCheckedAt": item.get("checkoutTypeCheckedAt") or account.get("checkoutTypeCheckedAt"),
            "paymentLink": str(item.get("paymentLink") or ""),
            "paymentLinkConfigured": bool(item.get("paymentLink")),
            "paymentStatus": item.get("paymentStatus", "pending"),
            "paymentRetryCount": int(item.get("paymentRetryCount") or 0),
            "paymentJobId": item.get("paymentJobId"),
            "paymentError": item.get("paymentError"),
            "paymentPhonePreview": item.get("paymentPhonePreview"),
            "paidAt": item.get("paidAt"),
            "paymentSummary": item.get("paymentSummary"),
            "exportCount": int(item.get("exportCount") or 0),
            "firstExportedAt": item.get("firstExportedAt"),
            "lastExportedAt": item.get("lastExportedAt"),
            "mailConfirmationStatus": item.get("mailConfirmationStatus", "unchecked"),
            "mailConfirmationSubject": item.get("mailConfirmationSubject"),
            "mailConfirmationReceivedAt": item.get("mailConfirmationReceivedAt"),
            "mailConfirmationCheckedAt": item.get("mailConfirmationCheckedAt"),
            "mailConfirmationError": item.get("mailConfirmationError"),
            "mailConfirmationOrderId": item.get("mailConfirmationOrderId"),
            "mailConfirmationAttempt": int(item.get("mailConfirmationAttempt") or 0),
            "mailConfirmationStartedAt": item.get("mailConfirmationStartedAt"),
            "mailConfirmationDeadline": item.get("mailConfirmationDeadline"),
            "mailConfirmationNextCheckAt": item.get("mailConfirmationNextCheckAt"),
            "smsReceiverState": item.get("smsReceiverState", "idle"),
            "smsReceiverCredentialReady": bool(item.get("smsReceiverCredentialReady")),
            "smsReceiverPhoneVerified": bool(item.get("smsReceiverPhoneVerified")),
            "smsReceiverTaskId": item.get("smsReceiverTaskId"),
            "smsReceiverSubmittedAt": item.get("smsReceiverSubmittedAt"),
            "smsReceiverUpdatedAt": item.get("smsReceiverUpdatedAt"),
            "smsReceiverError": item.get("smsReceiverError"),
            "heroSmsManaged": bool(item.get("heroSmsActivationId")),
            "heroSmsStatus": item.get("heroSmsStatus"),
            "heroSmsAttempt": int(item.get("heroSmsAttempt") or 0),
            "heroSmsPrice": item.get("heroSmsPrice"),
            "heroSmsWaitDeadline": item.get("heroSmsWaitDeadline"),
            "heroSmsError": item.get("heroSmsError"),
            "createdAt": item.get("createdAt"),
            "updatedAt": item.get("updatedAt"),
        }

    async def _queue_failed_retries(self, settings: dict[str, Any]) -> None:
        now = utc_now()
        extraction_limit = int(settings.get("extractionFailureRetries") or 0)
        if extraction_limit > 0:
            cursor = self.items.find({"stage": "extraction_failed"}).sort(
                "updatedAt", ASCENDING
            )
            for item in await self._guard(cursor.to_list(length=100)):
                retry_count = int(item.get("extractionRetryCount") or 0)
                if retry_count >= extraction_limit:
                    continue
                await self._guard(
                    self.items.update_one(
                        {"_id": item["_id"], "stage": "extraction_failed"},
                        {
                            "$set": {
                                "stage": "eligible",
                                "extractionStatus": "pending",
                                "extractorTaskId": None,
                                "extractionLastError": item.get("extractionError"),
                                "updatedAt": now,
                            },
                            "$inc": {"extractionRetryCount": 1},
                        },
                    )
                )

        payment_limit = int(settings.get("paymentFailureRetries") or 0)
        if not (
            payment_limit > 0
            and settings.get("autoPaymentEnabled")
            and settings.get("heroSmsEnabled")
        ):
            return
        cursor = self.items.find(
            {
                "stage": "payment_failed",
                "extractionStatus": "succeeded",
                "paymentLink": {"$type": "string", "$ne": ""},
            }
        ).sort("updatedAt", ASCENDING)
        for item in await self._guard(cursor.to_list(length=100)):
            retry_count = int(item.get("paymentRetryCount") or 0)
            if retry_count >= payment_limit:
                continue
            await self._guard(
                self.items.update_one(
                    {"_id": item["_id"], "stage": "payment_failed"},
                    {
                        "$set": {
                            "stage": "payment_ready",
                            "paymentStatus": "pending",
                            "paymentJobId": None,
                            "paymentDeviceId": None,
                            "paymentLastError": item.get("paymentError"),
                            "heroSmsActivationId": None,
                            "heroSmsStatus": None,
                            "heroSmsAttempt": 0,
                            "heroSmsPrice": None,
                            "heroSmsWaitDeadline": None,
                            "heroSmsError": None,
                            "updatedAt": now,
                        },
                        "$inc": {"paymentRetryCount": 1},
                    },
                )
            )

    async def tick(self) -> None:
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            await self.sync_eligible()
            await self._reconcile_extractions()
            await self._reconcile_payments()
            await self._reconcile_paid_confirmations()
            settings = await self.settings()
            if not settings["enabled"]:
                return
            await self._queue_failed_retries(settings)

            active = await self._guard(
                self.items.count_documents({"extractionStatus": {"$in": ["queued", "running"]}})
            )
            extraction_slots = max(0, int(settings["extractionConcurrency"]) - active)
            if extraction_slots:
                candidates = await self._guard(
                    self.items.find(
                        {
                            "stage": "eligible",
                            "extractionStatus": "pending",
                            "promotionEligible": True,
                        }
                    )
                    .sort("createdAt", ASCENDING)
                    .limit(extraction_slots)
                    .to_list(length=extraction_slots)
                )
                for candidate in candidates:
                    await self._start_extraction(candidate, settings)

            if settings["autoPaymentEnabled"] and settings["heroSmsEnabled"]:
                active_payments = await self._guard(
                    self.items.count_documents(
                        {"paymentStatus": {"$in": ["queued", "running", "awaiting_otp", "awaiting_captcha"]}}
                    )
                )
                payment_slots = max(0, int(settings["paymentConcurrency"]) - active_payments)
                if payment_slots:
                    ready_items = await self._guard(
                        self.items.find(
                            {"stage": "payment_ready", "paymentStatus": "pending"}
                        )
                        .sort("extractedAt", ASCENDING)
                        .limit(payment_slots)
                        .to_list(length=payment_slots)
                    )
                    for ready in ready_items:
                        await self.start_payment(str(ready["_id"]), PipelinePaymentInput())

    async def start_extractions(self, ids: list[str]) -> dict[str, int]:
        settings = await self.settings()
        if not (settings.get("checkoutProxyCountry") or settings.get("checkoutProxy")):
            raise PipelineServiceError("checkout_proxy_required", "请先保存 Checkout 代理")
        query: dict[str, Any] = {
            "promotionEligible": True,
            "stage": "eligible",
            "extractionStatus": "pending",
        }
        if ids:
            query["_id"] = {"$in": ids}
        cursor = self.items.find(query).sort("createdAt", ASCENDING)
        documents = await self._guard(cursor.to_list(length=100))
        active = await self._guard(
            self.items.count_documents({"extractionStatus": {"$in": ["queued", "running"]}})
        )
        available_slots = max(0, int(settings["extractionConcurrency"]) - active)
        started = 0
        for item in documents[:available_slots]:
            if item.get("extractionStatus") in {"queued", "running"}:
                continue
            if await self._start_extraction(item, settings):
                started += 1
        return {
            "requested": len(documents),
            "started": started,
            "deferred": max(0, len(documents) - started),
        }

    async def _account_secret(self, account_id: str) -> dict[str, Any]:
        account = await self._guard(
            self.resources.accounts.find_one(
                {"_id": account_id},
                {
                    "email": 1,
                    "promotionEligible": 1,
                    "accessToken": 1,
                    "accessTokenConfigured": 1,
                    "accessTokenExpiresAt": 1,
                },
            )
        )
        if not account:
            raise ResourceNotFoundError("流水线账号不存在")
        if account.get("promotionEligible") is not True:
            raise PipelineServiceError("account_not_eligible", "账号不具备 Plus 试用资格")
        if not account.get("accessTokenConfigured") or not account.get("accessToken"):
            raise PipelineServiceError("access_token_missing", "账号没有可用 Access Token")
        expires_at = account.get("accessTokenExpiresAt")
        if not isinstance(expires_at, datetime) or expires_at <= utc_now():
            raise PipelineServiceError("access_token_expired", "账号 Access Token 已过期")
        return account

    async def _start_extraction(self, item: dict[str, Any], settings: dict[str, Any]) -> bool:
        item_id = str(item["_id"])
        try:
            account = await self._account_secret(str(item["accountId"]))
            billing_country = str(
                settings.get("country") or DEFAULT_PIPELINE_COUNTRY
            ).strip().upper()
            if billing_country not in SUPPORTED_COUNTRIES:
                billing_country = DEFAULT_PIPELINE_COUNTRY
            checkout_proxy = await self._effective_proxy_pool(settings, "checkout")
            update_proxy = (
                await self._effective_proxy_pool(settings, "update")
                if bool(settings["applyCheckoutUpdate"])
                else ""
            )
            payload = PaymentExtractorTaskCreate(
                accessToken=str(account["accessToken"]),
                checkoutProxy=checkout_proxy,
                updateProxy=update_proxy,
                country=billing_country,
                paymentMethod="paypal",
                applyCheckoutUpdate=bool(settings["applyCheckoutUpdate"]),
                checkoutMode="auto",
            )
            snapshot = await asyncio.to_thread(self.extractor.create, payload)
            task_id = str(snapshot.get("taskId") or "")
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {
                        "$set": {
                            "stage": "extracting",
                            "extractionStatus": str(snapshot.get("status") or "queued"),
                            "extractorTaskId": task_id,
                            "billingCountry": billing_country,
                            "extractionError": None,
                            "updatedAt": utc_now(),
                        }
                    },
                )
            )
            return True
        except (PipelineServiceError, PaymentExtractorServiceError) as exc:
            await self._mark_extraction_failed(item_id, getattr(exc, "code", "extraction_start_failed"), str(exc))
            return False

    async def _mark_extraction_failed(self, item_id: str, code: str, message: str) -> None:
        await self._guard(
            self.items.update_one(
                {"_id": item_id},
                {
                    "$set": {
                        "stage": "extraction_failed",
                        "extractionStatus": "failed",
                        "extractionError": {"code": code, "message": message[:500]},
                        "updatedAt": utc_now(),
                    }
                },
            )
        )

    async def _reconcile_extractions(self) -> None:
        cursor = self.items.find({"extractionStatus": {"$in": ["queued", "running", "cancel_requested"]}})
        for item in await self._guard(cursor.to_list(length=100)):
            item_id = str(item["_id"])
            task_id = str(item.get("extractorTaskId") or "")
            try:
                snapshot = await asyncio.to_thread(self.extractor.get, task_id)
            except PaymentExtractorServiceError as exc:
                await self._mark_extraction_failed(item_id, exc.code, exc.message)
                continue
            status = str(snapshot.get("status") or "running")
            if status == "succeeded":
                result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
                checkout_type = checkout_type_from_result(result)
                payment_link = str(
                    result.get("paypalUrl")
                    or result.get("providerUrl")
                    or result.get("stripeRedirectUrl")
                    or ""
                )
                if not payment_link:
                    await self._mark_extraction_failed(item_id, "paypal_link_missing", "提链成功但没有返回 PayPal BA 链")
                    continue
                checked_at = utc_now()
                updates: dict[str, Any] = {
                    "stage": "payment_ready",
                    "extractionStatus": "succeeded",
                    "paymentStatus": "pending",
                    "paymentLink": payment_link,
                    "extractedAt": checked_at,
                    "extractionError": None,
                    "updatedAt": checked_at,
                }
                result_country = str(
                    result.get("billingCountry")
                    or result.get("billing_country")
                    or item.get("billingCountry")
                    or ""
                ).strip().upper()
                if result_country in SUPPORTED_COUNTRIES:
                    updates["billingCountry"] = result_country
                if checkout_type:
                    updates["checkoutType"] = checkout_type
                    updates["checkoutTypeCheckedAt"] = checked_at
                await self._guard(
                    self.items.update_one(
                        {"_id": item_id},
                        {"$set": updates},
                    )
                )
                if checkout_type:
                    await self._guard(
                        self.resources.accounts.update_one(
                            {"_id": str(item.get("accountId") or "")},
                            {
                                "$set": {
                                    "checkoutType": checkout_type,
                                    "checkoutTypeCheckedAt": checked_at,
                                }
                            },
                        )
                    )
            elif status in {"failed", "cancelled"}:
                await self._mark_extraction_failed(
                    item_id,
                    "extractor_" + status,
                    str(snapshot.get("error") or f"提链任务{status}")[:500],
                )
            else:
                await self._guard(
                    self.items.update_one(
                        {"_id": item_id},
                        {"$set": {"extractionStatus": status, "updatedAt": utc_now()}},
                    )
                )

    async def _protocol_request(
        self,
        method: str,
        path: str,
        device_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Cookie": f"paypal_web_device_id={device_id}"}
        async with httpx.AsyncClient(timeout=15, trust_env=False, headers=headers) as client:
            response = await client.request(method, self.agreement.base_url + path, json=payload)
        try:
            data = response.json()
        except Exception as exc:
            raise PipelineServiceError("protocol_invalid_response", "协议支付服务返回了无效响应", 502) from exc
        if response.status_code >= 400:
            raise PipelineServiceError(
                "protocol_request_failed",
                str(data.get("error") or f"协议支付 HTTP {response.status_code}"),
                response.status_code,
            )
        return data

    async def _effective_proxy_pool(
        self,
        settings: dict[str, Any],
        kind: Literal["checkout", "update", "protocol"],
    ) -> str:
        country_key = f"{kind}ProxyCountry"
        group_key = f"{kind}ProxyGroup"
        proxy_key = f"{kind}Proxy"
        country = str(settings.get(country_key) or "").strip().upper()
        group = " ".join(str(settings.get(group_key) or "").split())
        if country:
            urls = (
                await self.resources.enabled_proxy_urls(country, group=group)
                if group
                else await self.resources.enabled_proxy_urls(country)
            )
            if not urls:
                raise PipelineServiceError(
                    f"{kind}_proxy_country_empty",
                    f"所选 {country} / {group or '全部分组'} 代理池没有已启用代理",
                    422,
                )
            return "\n".join(urls)
        return str(settings.get(proxy_key) or "").strip()

    async def hero_sms_settings(self) -> dict[str, Any]:
        settings = await self.settings()
        return {
            "enabled": bool(settings["heroSmsEnabled"]),
            "countryId": int(settings["heroSmsCountryId"]),
            "maxPrice": float(settings["heroSmsMaxPrice"]),
            "changeNumberRetries": int(settings["heroSmsChangeNumberRetries"]),
            "numberWaitSeconds": int(settings["heroSmsNumberWaitSeconds"]),
            "agreementAutoSmsEnabled": bool(settings["agreementAutoSmsEnabled"]),
            "pipelineAutoPaymentEnabled": bool(settings["autoPaymentEnabled"]),
            "apiKeyConfigured": self.hero_sms.configured,
            "updatedAt": settings.get("updatedAt"),
        }

    async def update_hero_sms_settings(
        self,
        payload: HeroSmsSettingsUpdate,
    ) -> dict[str, Any]:
        if payload.enabled and not self.hero_sms.configured:
            raise PipelineServiceError("herosms_api_key_missing", "HeroSMS API Key 未配置")
        if payload.agreementAutoSmsEnabled and not payload.enabled:
            raise PipelineServiceError("herosms_required", "协议自动接码必须先启用 HeroSMS")
        updates: dict[str, Any] = {
            "heroSmsEnabled": payload.enabled,
            "heroSmsCountryId": payload.countryId,
            "heroSmsMaxPrice": payload.maxPrice,
            "heroSmsChangeNumberRetries": payload.changeNumberRetries,
            "heroSmsNumberWaitSeconds": payload.numberWaitSeconds,
            "agreementAutoSmsEnabled": payload.agreementAutoSmsEnabled,
            "updatedAt": utc_now(),
        }
        if not payload.enabled:
            updates["autoPaymentEnabled"] = False
            updates["agreementAutoSmsEnabled"] = False
        await self._guard(
            self.settings_collection.update_one(
                {"_id": "default"}, {"$set": updates}, upsert=True
            )
        )
        return await self.hero_sms_settings()

    async def hero_sms_countries(self) -> list[dict[str, Any]]:
        try:
            countries = await self.hero_sms.countries()
        except HeroSmsError as exc:
            raise PipelineServiceError(exc.code, exc.message, exc.status_code) from exc
        return [{"id": item.id, "name": item.name} for item in countries]

    async def prepare_agreement_hero_sms(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        settings = await self.settings()
        if not settings.get("agreementAutoSmsEnabled") or str(payload.get("phone") or "").strip():
            return payload, None
        if not settings.get("heroSmsEnabled"):
            raise PipelineServiceError("herosms_required", "协议自动接码需要先启用 HeroSMS")
        try:
            activation = await self.hero_sms.acquire_paypal(
                int(settings.get("heroSmsCountryId") or 182),
                float(settings.get("heroSmsMaxPrice") or 1.0),
            )
        except HeroSmsError as exc:
            raise PipelineServiceError(exc.code, exc.message, exc.status_code) from exc
        prepared = dict(payload)
        prepared["phone"] = activation.phone
        if not prepared.get("proxies") and not prepared.get("proxy_pool"):
            proxies = await self._effective_proxy_pool(settings, "protocol")
            if proxies:
                prepared["proxies"] = proxies.splitlines()
        return prepared, {
            "activation": activation,
            "settings": settings,
        }

    async def cancel_prepared_agreement_hero_sms(
        self,
        context: dict[str, Any] | None,
    ) -> None:
        activation = (context or {}).get("activation")
        if not isinstance(activation, HeroSmsActivation):
            return
        try:
            await self.hero_sms.cancel(activation.activation_id)
        except HeroSmsError:
            pass

    def track_agreement_hero_sms(
        self,
        job_id: str,
        cookie: str,
        context: dict[str, Any] | None,
    ) -> None:
        if not job_id or context is None:
            return
        task = asyncio.create_task(
            self._agreement_hero_sms_loop(job_id, cookie, context),
            name=f"agreement-herosms-{job_id[:8]}",
        )
        self._agreement_hero_tasks.add(task)
        task.add_done_callback(self._agreement_hero_tasks.discard)

    async def _agreement_sidecar_request(
        self,
        method: str,
        path: str,
        cookie: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Cookie": cookie} if cookie else {}
        async with httpx.AsyncClient(
            timeout=15,
            trust_env=False,
            headers=headers,
        ) as client:
            response = await client.request(
                method,
                self.agreement.base_url + path,
                json=payload,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise PipelineServiceError(
                "protocol_invalid_response",
                "协议支付服务返回了无效响应",
                502,
            ) from exc
        if response.status_code >= 400:
            raise PipelineServiceError(
                "protocol_request_failed",
                str(data.get("error") or f"协议支付 HTTP {response.status_code}"),
                response.status_code,
            )
        return data

    async def _agreement_hero_sms_loop(
        self,
        job_id: str,
        cookie: str,
        context: dict[str, Any],
    ) -> None:
        settings = context["settings"]
        activation = context["activation"]
        attempt = 1
        deadline = utc_now() + timedelta(
            seconds=int(settings.get("heroSmsNumberWaitSeconds") or 120)
        )
        code_submitted_at: datetime | None = None
        try:
            while True:
                job_data = await self._agreement_sidecar_request(
                    "GET", f"/api/jobs/{job_id}", cookie
                )
                job = job_data.get("job") if isinstance(job_data.get("job"), dict) else job_data
                job_status = str(job.get("status") or "") if isinstance(job, dict) else ""
                if job_status in TERMINAL_PAYMENT_STATES:
                    if activation is not None:
                        if job_status == "completed":
                            await self.hero_sms.complete(activation.activation_id)
                        else:
                            await self.hero_sms.cancel(activation.activation_id)
                    return

                if activation is not None:
                    sms_status = await self.hero_sms.status(activation.activation_id)
                    if sms_status.state == "received" and sms_status.code:
                        await self._agreement_sidecar_request(
                            "POST",
                            f"/api/jobs/{job_id}/otp",
                            cookie,
                            {"value": sms_status.code},
                        )
                        await self.hero_sms.complete(activation.activation_id)
                        activation = None
                        code_submitted_at = utc_now()

                needs_rotation = utc_now() >= deadline
                if (
                    activation is None
                    and code_submitted_at is not None
                    and job_status == "awaiting_otp"
                    and utc_now() - code_submitted_at >= timedelta(seconds=8)
                ):
                    needs_rotation = True
                if needs_rotation:
                    if activation is not None:
                        await self.hero_sms.cancel(activation.activation_id)
                    if attempt >= 1 + int(settings.get("heroSmsChangeNumberRetries") or 0):
                        return
                    attempt += 1
                    activation = await self.hero_sms.acquire_paypal(
                        int(settings.get("heroSmsCountryId") or 182),
                        float(settings.get("heroSmsMaxPrice") or 1.0),
                    )
                    await self._agreement_sidecar_request(
                        "POST",
                        f"/api/jobs/{job_id}/otp",
                        cookie,
                        {"value": activation.phone},
                    )
                    deadline = utc_now() + timedelta(
                        seconds=int(settings.get("heroSmsNumberWaitSeconds") or 120)
                    )
                    code_submitted_at = None
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            if activation is not None:
                try:
                    await self.hero_sms.cancel(activation.activation_id)
                except HeroSmsError:
                    pass
            raise
        except (HeroSmsError, PipelineServiceError, httpx.HTTPError):
            if activation is not None:
                try:
                    await self.hero_sms.cancel(activation.activation_id)
                except HeroSmsError:
                    pass

    async def hero_sms_test(self) -> dict[str, Any]:
        try:
            balance = await self.hero_sms.balance()
        except HeroSmsError as exc:
            raise PipelineServiceError(exc.code, exc.message, exc.status_code) from exc
        settings = await self.settings()
        return {
            "ok": True,
            "configured": True,
            "countryId": int(settings["heroSmsCountryId"]),
            "service": "PayPal",
            "balance": balance,
        }

    async def _acquire_hero_number(
        self,
        item_id: str,
        settings: dict[str, Any],
        attempt: int,
    ) -> HeroSmsActivation:
        activation = await self.hero_sms.acquire_paypal(
            int(settings.get("heroSmsCountryId") or 182),
            float(settings["heroSmsMaxPrice"]),
        )
        now = utc_now()
        await self._guard(
            self.items.update_one(
                {"_id": item_id},
                {
                    "$set": {
                        "heroSmsActivationId": activation.activation_id,
                        "heroSmsStatus": "number_acquired",
                        "heroSmsAttempt": attempt,
                        "heroSmsPrice": activation.price,
                        "heroSmsAcquiredAt": now,
                        "heroSmsWaitDeadline": now
                        + timedelta(seconds=int(settings["heroSmsNumberWaitSeconds"])),
                        "heroSmsLastPollAt": None,
                        "heroSmsError": None,
                        "paymentPhonePreview": "***" + activation.phone[-4:],
                        "updatedAt": now,
                    }
                },
            )
        )
        return activation

    async def _release_hero_activation(
        self,
        item_id: str,
        activation_id: str,
        *,
        completed: bool,
    ) -> bool:
        if not activation_id:
            return True
        try:
            if completed:
                await self.hero_sms.complete(activation_id)
            else:
                await self.hero_sms.cancel(activation_id)
            return True
        except HeroSmsError as exc:
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {
                        "$set": {
                            "heroSmsReleaseError": {"code": exc.code, "message": exc.message[:300]},
                            "updatedAt": utc_now(),
                        }
                    },
                )
            )
            return False

    async def _mark_payment_failed(self, item_id: str, code: str, message: str) -> None:
        await self._guard(
            self.items.update_one(
                {"_id": item_id},
                {
                    "$set": {
                        "stage": "payment_failed",
                        "paymentStatus": "failed",
                        "paymentError": {"code": code, "message": message[:500]},
                        "updatedAt": utc_now(),
                    }
                },
            )
        )

    async def _rotate_hero_number(
        self,
        item: dict[str, Any],
        settings: dict[str, Any],
        reason: str,
    ) -> None:
        item_id = str(item["_id"])
        activation_id = str(item.get("heroSmsActivationId") or "")
        attempt = int(item.get("heroSmsAttempt") or 1)
        released = await self._release_hero_activation(item_id, activation_id, completed=False)
        if not released:
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {
                        "$set": {
                            "heroSmsStatus": "cancel_retry",
                            "heroSmsWaitDeadline": utc_now() + timedelta(seconds=30),
                            "updatedAt": utc_now(),
                        }
                    },
                )
            )
            return
        if attempt >= 1 + int(settings["heroSmsChangeNumberRetries"]):
            await self._mark_payment_failed(
                item_id,
                "herosms_retries_exhausted",
                f"HeroSMS 已达到换号上限：{reason}",
            )
            return
        try:
            activation = await self._acquire_hero_number(item_id, settings, attempt + 1)
            await self._protocol_request(
                "POST",
                f"/api/jobs/{item['paymentJobId']}/otp",
                str(item.get("paymentDeviceId") or ""),
                {"value": activation.phone},
            )
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {
                        "$set": {
                            "stage": "paying",
                            "paymentStatus": "running",
                            "heroSmsStatus": "new_number_submitted",
                            "updatedAt": utc_now(),
                        }
                    },
                )
            )
        except (HeroSmsError, PipelineServiceError) as exc:
            if "activation" in locals():
                await self._release_hero_activation(
                    item_id, activation.activation_id, completed=False
                )
            await self._mark_payment_failed(
                item_id,
                getattr(exc, "code", "herosms_rotation_failed"),
                str(exc),
            )

    async def _reconcile_hero_sms(
        self,
        item: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        item_id = str(item["_id"])
        if not settings.get("heroSmsEnabled"):
            await self._release_hero_activation(
                item_id, str(item.get("heroSmsActivationId") or ""), completed=False
            )
            await self._mark_payment_failed(
                item_id,
                "herosms_disabled",
                "HeroSMS 已关闭，在途接码已停止",
            )
            return
        if item.get("heroSmsStatus") == "code_submitted":
            await self._rotate_hero_number(item, settings, "验证码未通过，PP 再次要求验证")
            return
        deadline = item.get("heroSmsWaitDeadline")
        if isinstance(deadline, datetime) and deadline <= utc_now():
            await self._rotate_hero_number(item, settings, "单号等待超时")
            return
        last_poll = item.get("heroSmsLastPollAt")
        if isinstance(last_poll, datetime) and utc_now() - last_poll < timedelta(seconds=3):
            return
        activation_id = str(item.get("heroSmsActivationId") or "")
        try:
            status = await self.hero_sms.status(activation_id)
        except HeroSmsError as exc:
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {
                        "$set": {
                            "heroSmsLastPollAt": utc_now(),
                            "heroSmsError": {"code": exc.code, "message": exc.message[:300]},
                            "updatedAt": utc_now(),
                        }
                    },
                )
            )
            return

        if status.state == "received" and status.code:
            await self._protocol_request(
                "POST",
                f"/api/jobs/{item['paymentJobId']}/otp",
                str(item.get("paymentDeviceId") or ""),
                {"value": status.code},
            )
            await self._release_hero_activation(item_id, activation_id, completed=True)
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {
                        "$set": {
                            "stage": "paying",
                            "paymentStatus": "running",
                            "heroSmsStatus": "code_submitted",
                            "heroSmsCodeReceivedAt": utc_now(),
                            "heroSmsLastPollAt": utc_now(),
                            "heroSmsError": None,
                            "updatedAt": utc_now(),
                        }
                    },
                )
            )
        elif status.state == "cancelled":
            await self._rotate_hero_number(item, settings, "激活已取消")
        else:
            await self._guard(
                self.items.update_one(
                    {"_id": item_id},
                    {
                        "$set": {
                            "heroSmsStatus": "waiting_sms",
                            "heroSmsLastPollAt": utc_now(),
                            "heroSmsError": None,
                            "updatedAt": utc_now(),
                        }
                    },
                )
            )

    async def start_payment(self, item_id: str, payload: PipelinePaymentInput) -> dict[str, Any]:
        item = await self._guard(self.items.find_one({"_id": item_id}))
        if not item:
            raise ResourceNotFoundError("流水线记录不存在")
        if (
            item.get("stage") != "payment_ready"
            or item.get("extractionStatus") != "succeeded"
            or item.get("paymentStatus") != "pending"
            or not item.get("paymentLink")
        ):
            raise PipelineServiceError("payment_link_not_ready", "该账号尚未提链成功")
        await self._account_secret(str(item.get("accountId") or ""))
        settings = await self.settings()
        billing_country = str(
            item.get("billingCountry")
            or settings.get("country")
            or DEFAULT_PIPELINE_COUNTRY
        ).strip().upper()
        if billing_country not in SUPPORTED_COUNTRIES:
            billing_country = DEFAULT_PIPELINE_COUNTRY
        proxies = payload.protocolProxy or await self._effective_proxy_pool(
            settings, "protocol"
        )
        if not proxies:
            raise PipelineServiceError("protocol_proxy_required", "请配置协议支付代理")
        activation: HeroSmsActivation | None = None
        phone = payload.phone
        if not phone:
            if not settings["heroSmsEnabled"]:
                raise PipelineServiceError("payment_phone_required", "请输入日本 PP 手机号或启用 HeroSMS")
            try:
                activation = await self._acquire_hero_number(item_id, settings, 1)
                phone = activation.phone
            except HeroSmsError as exc:
                await self._mark_payment_failed(item_id, exc.code, exc.message)
                raise PipelineServiceError(exc.code, exc.message, exc.status_code) from exc
        device_id = uuid.uuid4().hex
        try:
            await asyncio.to_thread(self.agreement.start)
            data = await self._protocol_request(
                "POST",
                "/api/jobs",
                device_id,
                {
                    "paypal_url": str(item["paymentLink"]),
                    "phone": phone,
                    "country": billing_country,
                    "proxies": proxies.splitlines(),
                    "buyer_mode": "original",
                    "agreement_only": False,
                },
            )
        except Exception as exc:
            if activation:
                await self._release_hero_activation(item_id, activation.activation_id, completed=False)
            await self._mark_payment_failed(
                item_id,
                getattr(exc, "code", "payment_start_failed"),
                str(exc),
            )
            raise
        job = data.get("job") if isinstance(data.get("job"), dict) else {}
        job_id = str(job.get("id") or "")
        now = utc_now()
        await self._guard(
            self.items.update_one(
                {"_id": item_id},
                {
                    "$set": {
                        "stage": "paying",
                        "paymentStatus": str(job.get("status") or "queued"),
                        "paymentJobId": job_id,
                        "paymentDeviceId": device_id,
                        "paymentPhonePreview": "***" + phone[-4:],
                        "paymentError": None,
                        "paymentStartedAt": now,
                        "updatedAt": now,
                    }
                },
            )
        )
        return self._public_item((await self._guard(self.items.find_one({"_id": item_id}))) or {})

    async def submit_otp(self, item_id: str, payload: PipelineOtpInput) -> dict[str, Any]:
        item = await self._guard(self.items.find_one({"_id": item_id}))
        if not item or not item.get("paymentJobId") or not item.get("paymentDeviceId"):
            raise PipelineServiceError("payment_job_missing", "协议支付任务不存在", 404)
        await self._protocol_request(
            "POST",
            f"/api/jobs/{item['paymentJobId']}/otp",
            str(item["paymentDeviceId"]),
            {"value": payload.value},
        )
        await self._guard(
            self.items.update_one(
                {"_id": item_id},
                {"$set": {"paymentStatus": "running", "stage": "paying", "updatedAt": utc_now()}},
            )
        )
        return self._public_item((await self._guard(self.items.find_one({"_id": item_id}))) or {})

    async def _reconcile_payments(self) -> None:
        settings = await self.settings()
        cursor = self.items.find(
            {
                "paymentJobId": {"$type": "string", "$ne": ""},
                "paymentStatus": {"$nin": list(TERMINAL_PAYMENT_STATES)},
            }
        )
        for item in await self._guard(cursor.to_list(length=100)):
            try:
                job = await self._protocol_request(
                    "GET",
                    f"/api/jobs/{item['paymentJobId']}",
                    str(item.get("paymentDeviceId") or ""),
                )
            except PipelineServiceError as exc:
                if exc.status_code == 404:
                    await self._guard(
                        self.items.update_one(
                            {"_id": item["_id"]},
                            {
                                "$set": {
                                    "stage": "payment_failed",
                                    "paymentStatus": "failed",
                                    "paymentError": {"code": "payment_job_lost", "message": "协议任务在服务重启后丢失"},
                                    "updatedAt": utc_now(),
                                }
                            },
                        )
                    )
                continue
            status = str(job.get("status") or "running")
            updates: dict[str, Any] = {"paymentStatus": status, "updatedAt": utc_now()}
            if status == "awaiting_otp":
                updates["stage"] = "payment_waiting_otp"
                await self._guard(self.items.update_one({"_id": item["_id"]}, {"$set": updates}))
                if item.get("heroSmsActivationId"):
                    refreshed = await self._guard(self.items.find_one({"_id": item["_id"]}))
                    if refreshed:
                        await self._reconcile_hero_sms(refreshed, settings)
                continue
            elif status == "awaiting_captcha":
                updates["stage"] = "payment_waiting_manual"
            elif status == "completed":
                result = job.get("result") if isinstance(job.get("result"), dict) else {}
                completed_at = utc_now()
                updates.update(
                    {
                        "stage": "paid",
                        "paidAt": completed_at,
                        "paymentError": None,
                        "mailConfirmationStatus": "waiting",
                        "mailConfirmationStartedAt": completed_at,
                        "mailConfirmationDeadline": completed_at + MAIL_CONFIRMATION_WINDOW,
                        "mailConfirmationNextCheckAt": completed_at,
                        "mailConfirmationAttempt": 0,
                        "mailConfirmationSubject": None,
                        "mailConfirmationReceivedAt": None,
                        "mailConfirmationOrderId": None,
                        "mailConfirmationError": None,
                        "paymentSummary": {
                            "status": result.get("status"),
                            "settlementStatus": result.get("settlement_status"),
                            "billingCountry": (
                                result.get("billing_country")
                                or item.get("billingCountry")
                                or settings.get("country")
                                or DEFAULT_PIPELINE_COUNTRY
                            ),
                        },
                    }
                )
                if item.get("heroSmsActivationId") and item.get("heroSmsStatus") not in {
                    "code_submitted",
                    "completed",
                }:
                    await self._release_hero_activation(
                        str(item["_id"]), str(item["heroSmsActivationId"]), completed=True
                    )
                updates["heroSmsStatus"] = "completed"
            elif status in {"failed", "cancelled"}:
                updates.update(
                    {
                        "stage": "payment_failed",
                        "paymentError": {
                            "code": "protocol_" + status,
                            "message": str(job.get("error") or job.get("stage") or status)[:500],
                        },
                    }
                )
                if item.get("heroSmsActivationId"):
                    await self._release_hero_activation(
                        str(item["_id"]), str(item["heroSmsActivationId"]), completed=False
                    )
                updates["heroSmsStatus"] = "cancelled"
            else:
                updates["stage"] = "paying"
            await self._guard(self.items.update_one({"_id": item["_id"]}, {"$set": updates}))
            if status == "completed":
                receiver_task = asyncio.create_task(
                    self._auto_submit_paid_to_sms_receiver(str(item["_id"])),
                    name=f"sms-receiver-{str(item['_id'])[:8]}",
                )
                self._sms_receiver_tasks.add(receiver_task)
                receiver_task.add_done_callback(self._sms_receiver_tasks.discard)

    async def reset_stage(self, item_id: str, stage: Literal["extraction", "payment"]) -> dict[str, Any]:
        if stage == "extraction":
            updates = {
                "stage": "eligible",
                "extractionStatus": "pending",
                "extractorTaskId": None,
                "extractionError": None,
                "paymentStatus": "pending",
                "paymentJobId": None,
                "paymentError": None,
                "paymentLink": None,
                "heroSmsActivationId": None,
                "heroSmsStatus": None,
                "heroSmsAttempt": 0,
                "heroSmsPrice": None,
                "heroSmsWaitDeadline": None,
                "heroSmsError": None,
            }
        else:
            current = await self._guard(self.items.find_one({"_id": item_id}))
            if not current:
                raise ResourceNotFoundError("流水线记录不存在")
            if current.get("extractionStatus") != "succeeded" or not current.get("paymentLink"):
                raise PipelineServiceError("payment_link_not_ready", "该账号尚未提链成功")
            updates = {
                "stage": "payment_ready",
                "paymentStatus": "pending",
                "paymentJobId": None,
                "paymentDeviceId": None,
                "paymentError": None,
                "heroSmsActivationId": None,
                "heroSmsStatus": None,
                "heroSmsAttempt": 0,
                "heroSmsPrice": None,
                "heroSmsWaitDeadline": None,
                "heroSmsError": None,
            }
        updates["updatedAt"] = utc_now()
        result = await self._guard(self.items.update_one({"_id": item_id}, {"$set": updates}))
        if not int(result.matched_count):
            raise ResourceNotFoundError("流水线记录不存在")
        return self._public_item((await self._guard(self.items.find_one({"_id": item_id}))) or {})

    async def delete(self, item_id: str) -> int:
        item = await self._guard(self.items.find_one({"_id": item_id}))
        if item and item.get("heroSmsActivationId") and item.get("heroSmsStatus") not in {
            "completed",
            "code_submitted",
        }:
            released = await self._release_hero_activation(
                item_id, str(item["heroSmsActivationId"]), completed=False
            )
            if not released:
                raise PipelineServiceError(
                    "herosms_cancel_pending",
                    "HeroSMS 旧号码暂未取消，请稍后再移除流水线记录",
                    409,
                )
        result = await self._guard(self.items.delete_one({"_id": item_id}))
        return int(result.deleted_count)
