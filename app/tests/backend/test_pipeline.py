from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.mongo_manager import MongoManager
from backend.checkout_type import checkout_type_from_result
from backend.pipeline_service import (
    AccountPipelineService,
    PipelinePaidExportInput,
    PipelinePaidExportStatusInput,
    PipelinePaidMailCheckInput,
    PipelinePaymentInput,
    PipelineServiceError,
    PipelineSettingsUpdate,
)
from backend.hero_sms_service import HeroSmsActivation, HeroSmsStatus
from backend.paid_mail_service import PaidMailCheckResult


class MemoryCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def sort(self, *_args: Any, **_kwargs: Any) -> "MemoryCursor":
        return self

    def skip(self, count: int) -> "MemoryCursor":
        self.documents = self.documents[count:]
        return self

    def limit(self, count: int) -> "MemoryCursor":
        self.documents = self.documents[:count]
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        documents = self.documents if length is None else self.documents[:length]
        return [dict(item) for item in documents]


class MemoryCollection:
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = {str(item["_id"]): dict(item) for item in documents or []}

    @classmethod
    def _matches(cls, item: dict[str, Any], query: dict[str, Any]) -> bool:
        for field, expected in query.items():
            if field == "$or":
                if not any(cls._matches(item, branch) for branch in expected):
                    return False
                continue
            if field == "$nor":
                if any(cls._matches(item, branch) for branch in expected):
                    return False
                continue
            actual = item.get(field)
            if not isinstance(expected, dict):
                if actual != expected:
                    return False
                continue
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$exists" in expected and (field in item) is not bool(expected["$exists"]):
                return False
            if "$type" in expected and expected["$type"] == "string" and not isinstance(actual, str):
                return False
            if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                return False
            if "$gte" in expected and not (actual is not None and actual >= expected["$gte"]):
                return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
        return True

    def find(self, query: dict[str, Any], _projection: dict[str, Any] | None = None) -> MemoryCursor:
        return MemoryCursor(
            [item for item in self.documents.values() if self._matches(item, query)]
        )

    async def find_one(
        self,
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        return next(
            (
                dict(item)
                for item in self.documents.values()
                if self._matches(item, query)
            ),
            None,
        )

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], **_kwargs: Any) -> Any:
        item = await self.find_one(query)
        if item is None:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        item.update(update.get("$set", {}))
        for field, amount in update.get("$inc", {}).items():
            item[field] = int(item.get(field) or 0) + int(amount)
        for field, operation in update.get("$push", {}).items():
            values = list(item.get(field) or [])
            if isinstance(operation, dict) and isinstance(operation.get("$each"), list):
                values.extend(operation["$each"])
                slice_value = operation.get("$slice")
                if isinstance(slice_value, int):
                    values = values[slice_value:] if slice_value < 0 else values[:slice_value]
            else:
                values.append(operation)
            item[field] = values
        self.documents[str(item["_id"])] = item
        return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)

    async def update_many(self, _query: dict[str, Any], update: dict[str, Any]) -> Any:
        for key, item in list(self.documents.items()):
            item.update(update.get("$set", {}))
            for field, amount in update.get("$inc", {}).items():
                item[field] = int(item.get(field) or 0) + int(amount)
            self.documents[key] = item
        count = len(self.documents)
        return SimpleNamespace(matched_count=count, modified_count=count)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for item in self.documents.values() if self._matches(item, query))

    async def aggregate(self, _pipeline: list[dict[str, Any]]) -> MemoryCursor:
        counts: dict[str, int] = {}
        for item in self.documents.values():
            stage = str(item.get("stage") or "unknown")
            counts[stage] = counts.get(stage, 0) + 1
        return MemoryCursor([{"_id": stage, "count": count} for stage, count in counts.items()])


class SortCaptureCursor(MemoryCursor):
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        super().__init__(documents)
        self.sort_spec: Any = None

    def sort(self, *args: Any, **_kwargs: Any) -> "SortCaptureCursor":
        self.sort_spec = args[0] if args else None
        return self


class SortCaptureCollection(MemoryCollection):
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        super().__init__(documents)
        self.last_cursor: SortCaptureCursor | None = None

    def find(
        self,
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> SortCaptureCursor:
        self.last_cursor = SortCaptureCursor(
            [item for item in self.documents.values() if self._matches(item, query)]
        )
        return self.last_cursor


class MemoryManager:
    online = True

    def __init__(self, items: MemoryCollection) -> None:
        self.database = {
            "account_pipeline": items,
            "pipeline_settings": MemoryCollection(),
        }

    def require_online(self) -> None:
        return None

    def mark_offline(self, _exc: Exception) -> None:
        return None


class FakeExtractor:
    def __init__(self) -> None:
        self.payload: Any = None
        self.snapshot: dict[str, Any] = {"taskId": "task-1", "status": "queued"}

    def create(self, payload: Any) -> dict[str, Any]:
        self.payload = payload
        return dict(self.snapshot)

    def get(self, _task_id: str) -> dict[str, Any]:
        return dict(self.snapshot)


class FakeHeroSms:
    configured = True

    def __init__(self) -> None:
        self.acquisitions = [
            HeroSmsActivation("activation-1", "+819012345678", 0.4),
            HeroSmsActivation("activation-2", "+818012345679", 0.45),
        ]
        self.statuses: list[HeroSmsStatus] = []
        self.cancelled: list[str] = []
        self.completed: list[str] = []

    async def acquire_paypal_japan(self, _max_price: float) -> HeroSmsActivation:
        return self.acquisitions.pop(0)

    async def acquire_paypal(self, _country_id: int, _max_price: float) -> HeroSmsActivation:
        return self.acquisitions.pop(0)

    async def status(self, _activation_id: str) -> HeroSmsStatus:
        return self.statuses.pop(0)

    async def cancel(self, activation_id: str) -> str:
        self.cancelled.append(activation_id)
        return "ACCESS_CANCEL"

    async def complete(self, activation_id: str) -> str:
        self.completed.append(activation_id)
        return "ACCESS_ACTIVATION"


def pipeline_service(
    account: dict[str, Any],
    item: dict[str, Any],
) -> tuple[AccountPipelineService, MemoryCollection, FakeExtractor]:
    items = MemoryCollection([item])
    manager = MemoryManager(items)
    resources = SimpleNamespace(manager=manager, accounts=MemoryCollection([account]))
    extractor = FakeExtractor()
    service = AccountPipelineService(resources, extractor, SimpleNamespace(base_url="http://fixture"))
    return service, items, extractor


def eligible_account(**overrides: Any) -> dict[str, Any]:
    return {
        "_id": "account-1",
        "email": "pipeline@example.test",
        "promotionEligible": True,
        "accessToken": "TEST_ACCESS_TOKEN",
        "accessTokenConfigured": True,
        "accessTokenExpiresAt": datetime.now(timezone.utc) + timedelta(hours=1),
        **overrides,
    }


def eligible_item(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "_id": "pipeline-1",
        "accountId": "account-1",
        "email": "pipeline@example.test",
        "promotionEligible": True,
        "stage": "eligible",
        "extractionStatus": "pending",
        "paymentStatus": "pending",
        "createdAt": now,
        "updatedAt": now,
        **overrides,
    }


def test_pipeline_rejects_ineligible_and_expired_accounts() -> None:
    async def exercise() -> None:
        ineligible, _, _ = pipeline_service(
            eligible_account(promotionEligible=False), eligible_item()
        )
        with pytest.raises(PipelineServiceError) as denied:
            await ineligible._account_secret("account-1")
        assert denied.value.code == "account_not_eligible"

        expired, _, _ = pipeline_service(
            eligible_account(accessTokenExpiresAt=datetime.now(timezone.utc) - timedelta(seconds=1)),
            eligible_item(),
        )
        with pytest.raises(PipelineServiceError) as stale:
            await expired._account_secret("account-1")
        assert stale.value.code == "access_token_expired"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"checkoutSessionId": "oaics_fixture"}, "oaics"),
        ({"checkout_session_id": "cs_fixture"}, "cs"),
        ({"sessionKind": "openai_custom_checkout"}, "oaics"),
        ({"session_kind": "stripe_checkout"}, "cs"),
        ({}, None),
    ],
)
def test_checkout_type_is_derived_from_extractor_result(
    result: dict[str, Any], expected: str | None
) -> None:
    assert checkout_type_from_result(result) == expected


def scheduling_settings(**overrides: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "extractionConcurrency": 3,
        "paymentConcurrency": 2,
        "extractionFailureRetries": 2,
        "paymentFailureRetries": 2,
        "country": "JP",
        "checkoutProxy": "http://checkout.example:8080",
        "updateProxy": "http://update.example:8080",
        "protocolProxy": "http://protocol.example:8080",
        "applyCheckoutUpdate": True,
        "heroSmsEnabled": True,
        "autoPaymentEnabled": True,
        "heroSmsMaxPrice": 0.5,
        "heroSmsChangeNumberRetries": 2,
        "heroSmsNumberWaitSeconds": 60,
        "heroSmsApiKeyConfigured": True,
        **overrides,
    }


def test_pipeline_master_switch_does_not_start_new_work() -> None:
    async def exercise() -> None:
        service, _, _ = pipeline_service(eligible_account(), eligible_item())
        starts: list[str] = []

        async def noop() -> None:
            return None

        async def start_extraction(*_args: Any) -> bool:
            starts.append("extraction")
            return True

        async def start_payment(*_args: Any) -> dict[str, Any]:
            starts.append("payment")
            return {}

        service.sync_eligible = noop  # type: ignore[method-assign]
        service._reconcile_extractions = noop  # type: ignore[method-assign]
        service._reconcile_payments = noop  # type: ignore[method-assign]
        service._reconcile_paid_confirmations = noop  # type: ignore[method-assign]
        service.settings = lambda: asyncio.sleep(  # type: ignore[method-assign]
            0, result=scheduling_settings(enabled=False)
        )
        service._start_extraction = start_extraction  # type: ignore[method-assign]
        service.start_payment = start_payment  # type: ignore[method-assign]

        await service.tick()
        assert starts == []

    asyncio.run(exercise())


def test_pipeline_scheduler_fills_configured_concurrency_slots() -> None:
    class SchedulingCollection:
        async def count_documents(self, query: dict[str, Any]) -> int:
            if "extractionStatus" in query:
                return 1
            if "paymentStatus" in query:
                return 1
            return 0

        def find(self, query: dict[str, Any]) -> MemoryCursor:
            if query.get("stage") == "eligible":
                return MemoryCursor(
                    [eligible_item(_id=f"extract-{index}") for index in range(3)]
                )
            if query.get("stage") == "payment_ready":
                return MemoryCursor(
                    [eligible_item(_id=f"payment-{index}") for index in range(2)]
                )
            return MemoryCursor([])

    async def exercise() -> None:
        service, _, _ = pipeline_service(eligible_account(), eligible_item())
        service.manager.database["account_pipeline"] = SchedulingCollection()
        extraction_ids: list[str] = []
        payment_ids: list[str] = []

        async def noop() -> None:
            return None

        async def start_extraction(item: dict[str, Any], _settings: dict[str, Any]) -> bool:
            extraction_ids.append(str(item["_id"]))
            return True

        async def start_payment(item_id: str, _payload: Any) -> dict[str, Any]:
            payment_ids.append(item_id)
            return {}

        service.sync_eligible = noop  # type: ignore[method-assign]
        service._reconcile_extractions = noop  # type: ignore[method-assign]
        service._reconcile_payments = noop  # type: ignore[method-assign]
        service._reconcile_paid_confirmations = noop  # type: ignore[method-assign]
        service.settings = lambda: asyncio.sleep(  # type: ignore[method-assign]
            0, result=scheduling_settings()
        )
        service._start_extraction = start_extraction  # type: ignore[method-assign]
        service.start_payment = start_payment  # type: ignore[method-assign]

        await service.tick()
        assert extraction_ids == ["extract-0", "extract-1"]
        assert payment_ids == ["payment-0"]

    asyncio.run(exercise())


def test_pipeline_retry_settings_defaults_and_bounds() -> None:
    defaults = PipelineSettingsUpdate()
    assert defaults.country == "JP"
    assert defaults.extractionFailureRetries == 0
    assert defaults.paymentFailureRetries == 0
    assert PipelineSettingsUpdate(
        extractionFailureRetries=0,
        paymentFailureRetries=10,
    ).model_dump()["paymentFailureRetries"] == 10
    with pytest.raises(ValueError):
        PipelineSettingsUpdate(extractionFailureRetries=11)
    with pytest.raises(ValueError):
        PipelineSettingsUpdate(paymentFailureRetries=-1)
    assert PipelineSettingsUpdate(country="de").country == "DE"
    with pytest.raises(ValueError):
        PipelineSettingsUpdate(country="TR")


def test_pipeline_save_preserves_shared_hero_sms_settings_when_omitted() -> None:
    async def exercise() -> None:
        service, _, _ = pipeline_service(eligible_account(), eligible_item())
        service.hero_sms = FakeHeroSms()  # type: ignore[assignment]
        service.settings_collection.documents["default"] = {
            "_id": "default",
            "heroSmsEnabled": True,
            "heroSmsCountryId": 62,
            "heroSmsMaxPrice": 0.4,
            "heroSmsChangeNumberRetries": 3,
            "heroSmsNumberWaitSeconds": 90,
            "agreementAutoSmsEnabled": True,
        }

        result = await service.update_settings(PipelineSettingsUpdate())

        stored = service.settings_collection.documents["default"]
        assert stored["heroSmsCountryId"] == 62
        assert stored["agreementAutoSmsEnabled"] is True
        assert result["heroSmsEnabled"] is True
        assert result["heroSmsCountryId"] == 62

    asyncio.run(exercise())


def test_pipeline_queues_failed_extraction_until_retry_limit() -> None:
    async def exercise() -> None:
        failure = {"code": "extractor_failed", "message": "fixture failure"}
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="extraction_failed",
                extractionStatus="failed",
                extractionError=failure,
                extractionRetryCount=0,
            ),
        )
        await service._queue_failed_retries(scheduling_settings())
        stored = items.documents["pipeline-1"]
        assert stored["stage"] == "eligible"
        assert stored["extractionStatus"] == "pending"
        assert stored["extractionRetryCount"] == 1
        assert stored["extractionLastError"] == failure

        exhausted, exhausted_items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="extraction_failed",
                extractionStatus="failed",
                extractionRetryCount=2,
            ),
        )
        await exhausted._queue_failed_retries(scheduling_settings())
        assert exhausted_items.documents["pipeline-1"]["stage"] == "extraction_failed"

    asyncio.run(exercise())


def test_pipeline_queues_failed_auto_payment_until_retry_limit() -> None:
    async def exercise() -> None:
        failure = {"code": "protocol_failed", "message": "fixture failure"}
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="payment_failed",
                extractionStatus="succeeded",
                paymentStatus="failed",
                paymentLink="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
                paymentError=failure,
                paymentRetryCount=0,
            ),
        )
        await service._queue_failed_retries(scheduling_settings())
        stored = items.documents["pipeline-1"]
        assert stored["stage"] == "payment_ready"
        assert stored["paymentStatus"] == "pending"
        assert stored["paymentRetryCount"] == 1
        assert stored["paymentLastError"] == failure

        disabled, disabled_items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="payment_failed",
                extractionStatus="succeeded",
                paymentStatus="failed",
                paymentLink="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
            ),
        )
        await disabled._queue_failed_retries(
            scheduling_settings(paymentFailureRetries=0)
        )
        assert disabled_items.documents["pipeline-1"]["stage"] == "payment_failed"

    asyncio.run(exercise())


def test_pipeline_extraction_uses_selected_billing_country_and_reaches_payment_ready() -> None:
    async def exercise() -> None:
        service, items, extractor = pipeline_service(eligible_account(), eligible_item())
        settings = {
            "country": "DE",
            "checkoutProxy": "http://checkout.example:8080",
            "updateProxy": "http://update.example:8080",
            "applyCheckoutUpdate": True,
        }
        assert await service._start_extraction(eligible_item(), settings) is True
        assert extractor.payload.country == "DE"
        assert extractor.payload.paymentMethod == "paypal"
        assert items.documents["pipeline-1"]["billingCountry"] == "DE"

        extractor.snapshot = {
            "taskId": "task-1",
            "status": "succeeded",
            "result": {
                "paypalUrl": "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
                "checkoutSessionId": "oaics_fixture",
                "sessionKind": "openai_custom_checkout",
            },
        }
        await service._reconcile_extractions()
        stored = items.documents["pipeline-1"]
        assert stored["stage"] == "payment_ready"
        assert stored["extractionStatus"] == "succeeded"
        assert stored["paymentLink"].endswith("BA-FIXTURE")
        assert stored["paymentLinkExpiresAt"] > stored["extractedAt"]
        assert stored["paymentLinkExpiresAt"] <= stored["extractedAt"] + timedelta(minutes=15)
        assert stored["checkoutType"] == "oaics"
        assert service.resources.accounts.documents["account-1"]["checkoutType"] == "oaics"

    asyncio.run(exercise())


def test_pipeline_resolves_selected_country_proxy_pools_at_runtime() -> None:
    async def exercise() -> None:
        service, _, extractor = pipeline_service(eligible_account(), eligible_item())
        requested: list[tuple[str, str | None]] = []

        async def proxy_urls(country: str, *, group: str | None = None) -> list[str]:
            requested.append((country, group))
            return [f"http://user:password@{country.lower()}.proxy.test:8000"]

        service.resources.enabled_proxy_urls = proxy_urls  # type: ignore[attr-defined]
        settings = {
            "checkoutProxy": "",
            "updateProxy": "",
            "checkoutProxyCountry": "TR",
            "updateProxyCountry": "JP",
            "checkoutProxyGroup": "TR-A",
            "updateProxyGroup": "JP-B",
            "applyCheckoutUpdate": True,
        }

        assert await service._start_extraction(eligible_item(), settings) is True
        assert requested == [("TR", "TR-A"), ("JP", "JP-B")]
        assert extractor.payload.checkoutProxy.endswith("tr.proxy.test:8000")
        assert extractor.payload.updateProxy.endswith("jp.proxy.test:8000")

    asyncio.run(exercise())


def test_pipeline_limits_protocol_proxy_pool_to_service_capacity() -> None:
    async def exercise() -> None:
        service, _, _ = pipeline_service(eligible_account(), eligible_item())

        async def proxy_urls(_country: str, *, group: str | None = None) -> list[str]:
            assert group is None
            return [f"http://proxy-{index}.example.test:8000" for index in range(1100)]

        service.resources.enabled_proxy_urls = proxy_urls  # type: ignore[attr-defined]
        pool = await service._effective_proxy_pool(
            {"protocolProxyCountry": "GB", "protocolProxyGroup": "", "protocolProxy": ""},
            "protocol",
        )
        assert len(pool.splitlines()) == 500
        assert pool.splitlines()[-1].endswith("proxy-499.example.test:8000")

    asyncio.run(exercise())


def test_pipeline_marks_expired_paypal_link_for_manual_reextract() -> None:
    async def exercise() -> None:
        extracted_at = datetime.now(timezone.utc) - timedelta(minutes=16)
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="payment_failed",
                extractionStatus="succeeded",
                extractionRetryCount=3,
                extractedAt=extracted_at,
                paymentStatus="failed",
                paymentRetryCount=2,
                paymentLink="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
                paymentError={"code": "protocol_failed", "message": "fixture failure"},
            ),
        )

        assert await service._mark_expired_payment_links() == 1
        stored = items.documents["pipeline-1"]
        assert stored["stage"] == "payment_failed"
        assert stored["extractionStatus"] == "succeeded"
        assert stored["extractionRetryCount"] == 3
        assert stored["paymentRetryCount"] == 2
        assert stored["paymentLink"].endswith("BA-FIXTURE")
        assert stored["paymentLastError"]["code"] == "protocol_failed"
        assert stored["paymentError"]["code"] == "payment_link_expired"
        assert stored["logs"][-1]["event"] == "payment_link.expired"
        assert "手动重新提炼" in stored["logs"][-1]["message"]
        assert await service._mark_expired_payment_links() == 0

        await service._queue_failed_retries(
            scheduling_settings(
                extractionFailureRetries=0,
                paymentFailureRetries=0,
            )
        )
        assert items.documents["pipeline-1"]["stage"] == "payment_failed"

    asyncio.run(exercise())


def test_pipeline_manual_reextract_cancels_inflight_payment_and_sms() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="payment_waiting_otp",
                extractionStatus="succeeded",
                paymentStatus="awaiting_otp",
                extractedAt=datetime.now(timezone.utc) - timedelta(minutes=16),
                paymentLink="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
                paymentJobId="job-1",
                paymentDeviceId="device-1",
                heroSmsActivationId="activation-1",
                heroSmsStatus="cancel_retry",
            ),
        )
        hero = FakeHeroSms()
        service.hero_sms = hero  # type: ignore[assignment]
        requests: list[tuple[str, str, str]] = []

        async def protocol(method: str, path: str, device: str, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
            requests.append((method, path, device))
            return {"ok": True}

        service._protocol_request = protocol  # type: ignore[method-assign]

        result = await service.reset_stage("pipeline-1", "extraction")

        assert requests == [("POST", "/api/jobs/job-1/cancel", "device-1")]
        assert hero.cancelled == ["activation-1"]
        assert result["stage"] == "eligible"
        assert items.documents["pipeline-1"]["paymentJobId"] is None
        assert items.documents["pipeline-1"]["heroSmsActivationId"] is None
        assert items.documents["pipeline-1"]["logs"][-1]["event"] == "extraction.retry_manual"

    asyncio.run(exercise())


def test_pipeline_account_logs_include_legacy_failure_with_redaction() -> None:
    async def exercise() -> None:
        now = datetime.now(timezone.utc)
        service, _, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="payment_failed",
                extractionStatus="succeeded",
                extractedAt=now - timedelta(minutes=2),
                paymentStatus="failed",
                paymentLink="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
                paymentError={
                    "code": "protocol_request_failed",
                    "message": "http://user:SECRET@proxy.test token=TOKENVALUE pipeline@example.test +819012345678",
                },
                updatedAt=now,
            ),
        )

        response = await service.item_logs("pipeline-1")
        rendered = str(response["logs"])
        assert "protocol_request_failed" in rendered
        assert "SECRET" not in rendered
        assert "TOKENVALUE" not in rendered
        assert "+819012345678" not in rendered
        assert "pipeline@example.test" not in rendered

    asyncio.run(exercise())


def test_pipeline_payment_moves_from_otp_to_paid() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="paying",
                extractionStatus="succeeded",
                paymentStatus="running",
                paymentJobId="job-1",
                paymentDeviceId="device-1",
            ),
        )
        responses = [
            {"status": "awaiting_otp"},
            {
                "status": "completed",
                "result": {"status": "approved", "settlement_status": "settled", "billing_country": "JP"},
            },
        ]

        async def protocol_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return responses.pop(0)

        service._protocol_request = protocol_request  # type: ignore[method-assign]
        await service._reconcile_payments()
        assert items.documents["pipeline-1"]["stage"] == "payment_waiting_otp"
        await service._reconcile_payments()
        stored = items.documents["pipeline-1"]
        assert stored["stage"] == "paid"
        assert stored["paymentSummary"]["billingCountry"] == "JP"
        assert stored["mailConfirmationStatus"] == "waiting"
        assert stored["mailConfirmationNextCheckAt"] is not None

    asyncio.run(exercise())


def test_pipeline_list_awaits_async_aggregate_and_joins_account_details() -> None:
    async def exercise() -> None:
        service, _, _ = pipeline_service(
            eligible_account(
                chatgptPassword="FIXTURE_PASSWORD",
                totpSecret="FIXTURE_TOTP",
                emailAccessUrl="https://mail.example.test/inbox",
            ),
            eligible_item(paymentLink="https://checkout.example.test/pay/fixture"),
        )
        result = await service.list_items()
        assert result["counts"] == {"eligible": 1}
        assert result["items"][0]["chatgptPassword"] == "FIXTURE_PASSWORD"
        assert result["items"][0]["totpSecret"] == "FIXTURE_TOTP"
        assert result["items"][0]["emailAccessUrl"].startswith("https://mail.example.test/")
        assert result["items"][0]["paymentLink"] == "https://checkout.example.test/pay/fixture"

    asyncio.run(exercise())


def test_pipeline_list_filters_paid_settlement_state() -> None:
    async def exercise() -> None:
        service, _, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="paid",
                paymentStatus="completed",
                mailConfirmationStatus="confirmed",
            ),
        )
        confirmed = await service.list_items(stage="paid", settlement_state="confirmed")
        waiting = await service.list_items(stage="paid", settlement_state="waiting")
        assert confirmed["total"] == 1
        assert len(confirmed["items"]) == 1
        assert waiting["total"] == 0
        assert waiting["items"] == []

    asyncio.run(exercise())


def test_paid_receiver_credential_counts_as_verified() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="paid",
                smsReceiverCredentialReady=True,
                smsReceiverPhoneVerified=False,
            ),
        )
        verified = await service.list_items(stage="paid", receiver_state="verified")
        unverified = await service.list_items(stage="paid", receiver_state="unverified")
        stats = await service.paid_stats()

        assert verified["total"] == 1
        assert unverified["total"] == 0
        assert stats["smsVerified"] == 1
        assert stats["smsUnverified"] == 0

    asyncio.run(exercise())


def test_paid_pipeline_list_sorts_by_payment_success_time_descending() -> None:
    async def exercise() -> None:
        paid_at_old = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
        paid_at_new = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
        items = SortCaptureCollection(
            [
                {
                    "_id": "paid-old",
                    "accountId": "account-old",
                    "email": "old@example.test",
                    "stage": "paid",
                    "paidAt": paid_at_old,
                    "updatedAt": paid_at_new,
                },
                {
                    "_id": "paid-new",
                    "accountId": "account-new",
                    "email": "new@example.test",
                    "stage": "paid",
                    "paidAt": paid_at_new,
                    "updatedAt": paid_at_old,
                },
            ]
        )
        manager = MemoryManager(items)
        resources = SimpleNamespace(
            manager=manager,
            accounts=MemoryCollection(
                [
                    eligible_account(_id="account-old", email="old@example.test"),
                    eligible_account(_id="account-new", email="new@example.test"),
                ]
            ),
        )
        service = AccountPipelineService(
            resources,
            FakeExtractor(),
            SimpleNamespace(base_url="http://fixture"),
        )

        await service.list_items(stage="paid")

        assert items.last_cursor is not None
        assert items.last_cursor.sort_spec == [
            ("paidAt", -1),
            ("updatedAt", -1),
            ("_id", -1),
        ]

    asyncio.run(exercise())


def test_pipeline_paid_overview_and_export_join_mailbox_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API798_AUTH_CODE", "AUTH_FIXTURE")

    async def exercise() -> None:
        paid_at = datetime.now(timezone.utc)
        service, _, _ = pipeline_service(
            eligible_account(
                emailAccessUrl=(
                    "https://api798.com/get_code?email=pipeline%40example.test"
                )
            ),
            eligible_item(
                stage="paid",
                extractionStatus="succeeded",
                paymentStatus="completed",
                paidAt=paid_at,
                heroSmsPrice=0.42,
            ),
        )
        overview = await service.paid_stats(days=14)
        assert overview["total"] == 1
        assert overview["today"] == 1
        assert overview["last7Days"] == 1
        assert overview["successRate"] == 100.0
        assert overview["averageHeroSmsPrice"] == 0.42
        assert overview["exported"] == 0
        assert overview["unexported"] == 1
        assert overview["mailConfirmed"] == 0
        assert sum(point["count"] for point in overview["daily"]) == 1

        listed = await service.list_items()
        assert listed["items"][0]["emailAccessUrl"] == (
            "https://api798.com/latest?email=pipeline%40example.test"
            "&auth_code=AUTH_FIXTURE"
        )

        exported = await service.export_paid(PipelinePaidExportInput())
        assert exported["count"] == 1
        assert exported["skippedMissingUrlCount"] == 0
        assert exported["content"] == (
            "pipeline@example.test----"
            "https://api798.com/latest?email=pipeline%40example.test"
            "&auth_code=AUTH_FIXTURE"
        )
        assert exported["filename"].startswith("paid-accounts-1-mail-links-")
        stored = service.items.documents["pipeline-1"]
        assert stored["exportCount"] == 1
        assert stored["firstExportedAt"] is not None

        marked = await service.mark_paid_export_status(
            PipelinePaidExportStatusInput(ids=["pipeline-1"], exported=False)
        )
        assert marked == {"updated": 1}
        assert service.items.documents["pipeline-1"]["exportCount"] == 0

    asyncio.run(exercise())


def test_pipeline_checks_paid_confirmation_mail_and_persists_status() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(
                emailAccessUrl="https://mail.example.test/paid",
                chatgptPassword="FIXTURE_PASSWORD",
                totpSecret="JBSWY3DPEHPK3PXP",
            ),
            eligible_item(stage="paid", paymentStatus="completed"),
        )
        service.settings_collection.documents["default"] = {
            "_id": "default",
            "smsReceiverEnabled": True,
            "smsReceiverAutoSubmit": True,
        }
        submitted: list[str] = []

        async def submit_receiver(payload: Any) -> dict[str, Any]:
            submitted.extend(payload.ids)
            items.documents["pipeline-1"]["smsReceiverState"] = "queued"
            return {"submitted": 1}

        service.submit_paid_to_sms_receiver = submit_receiver  # type: ignore[method-assign]
        service.mail_checker = lambda _url, _email, _paid_at: PaidMailCheckResult(
            status="confirmed",
            subject="ChatGPT - New plan",
            received_at=datetime(2026, 8, 15, 5, 47, 15),
            order_id="sub_fixture",
        )

        result = await service.check_paid_mail(
            PipelinePaidMailCheckInput(ids=["pipeline-1"])
        )

        assert result["confirmed"] == 1
        stored = items.documents["pipeline-1"]
        assert stored["mailConfirmationStatus"] == "confirmed"
        assert stored["mailConfirmationSubject"] == "ChatGPT - New plan"
        assert stored["mailConfirmationOrderId"] == "sub_fixture"
        assert submitted == ["pipeline-1"]

    asyncio.run(exercise())


def test_pipeline_automatically_confirms_waiting_paid_mail() -> None:
    async def exercise() -> None:
        paid_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        service, items, _ = pipeline_service(
            eligible_account(
                emailAccessUrl="https://mail.example.test/paid",
                chatgptPassword="FIXTURE_PASSWORD",
                totpSecret="JBSWY3DPEHPK3PXP",
            ),
            eligible_item(
                stage="paid",
                paymentStatus="completed",
                paidAt=paid_at,
                mailConfirmationStatus="waiting",
                mailConfirmationDeadline=paid_at + timedelta(minutes=10),
                mailConfirmationNextCheckAt=datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        )
        service.settings_collection.documents["default"] = {
            "_id": "default",
            "smsReceiverEnabled": True,
            "smsReceiverAutoSubmit": True,
        }
        submitted: list[str] = []

        async def submit_receiver(payload: Any) -> dict[str, Any]:
            submitted.extend(payload.ids)
            items.documents["pipeline-1"]["smsReceiverState"] = "queued"
            return {"submitted": 1}

        service.submit_paid_to_sms_receiver = submit_receiver  # type: ignore[method-assign]
        service.mail_checker = lambda _url, _email, _paid_at: PaidMailCheckResult(
            status="confirmed",
            subject="ChatGPT - New plan",
            received_at=paid_at + timedelta(seconds=30),
            order_id="sub_auto_fixture",
        )

        await service._reconcile_paid_confirmations()

        stored = items.documents["pipeline-1"]
        assert stored["mailConfirmationStatus"] == "confirmed"
        assert stored["mailConfirmationOrderId"] == "sub_auto_fixture"
        assert stored["mailConfirmationNextCheckAt"] is None
        assert stored["mailConfirmationAttempt"] == 1
        assert submitted == ["pipeline-1"]

    asyncio.run(exercise())


def test_pipeline_auto_receiver_requires_confirmed_mail_and_complete_material() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(
                emailAccessUrl="https://mail.example.test/paid",
                chatgptPassword="",
                totpSecret="JBSWY3DPEHPK3PXP",
            ),
            eligible_item(
                stage="paid",
                paymentStatus="completed",
                mailConfirmationStatus="waiting",
            ),
        )
        service.settings_collection.documents["default"] = {
            "_id": "default",
            "smsReceiverEnabled": True,
            "smsReceiverAutoSubmit": True,
        }
        submitted: list[str] = []

        async def submit_receiver(payload: Any) -> dict[str, Any]:
            submitted.extend(payload.ids)
            return {"submitted": 1}

        service.submit_paid_to_sms_receiver = submit_receiver  # type: ignore[method-assign]

        await service._auto_submit_paid_to_sms_receiver("pipeline-1")
        assert submitted == []

        items.documents["pipeline-1"]["mailConfirmationStatus"] = "confirmed"
        service.resources.accounts.documents["account-1"]["chatgptPassword"] = "FIXTURE_PASSWORD"
        await service._auto_submit_paid_to_sms_receiver("pipeline-1")
        assert submitted == ["pipeline-1"]

    asyncio.run(exercise())


def test_pipeline_auto_receiver_picks_up_existing_confirmed_paid_item() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(
                emailAccessUrl="https://mail.example.test/paid",
                chatgptPassword="FIXTURE_PASSWORD",
                totpSecret="JBSWY3DPEHPK3PXP",
            ),
            eligible_item(
                stage="paid",
                paymentStatus="completed",
                mailConfirmationStatus="confirmed",
            ),
        )
        service.settings_collection.documents["default"] = {
            "_id": "default",
            "smsReceiverEnabled": True,
            "smsReceiverAutoSubmit": True,
        }
        submitted: list[str] = []

        async def submit_receiver(payload: Any) -> dict[str, Any]:
            submitted.extend(payload.ids)
            items.documents["pipeline-1"]["smsReceiverState"] = "queued"
            return {"submitted": 1}

        service.submit_paid_to_sms_receiver = submit_receiver  # type: ignore[method-assign]
        await service._auto_submit_confirmed_paid_to_sms_receiver()
        assert submitted == ["pipeline-1"]

    asyncio.run(exercise())


def test_manual_recheck_uses_paid_time_and_does_not_downgrade_confirmed() -> None:
    async def exercise() -> None:
        paid_at = datetime.now(timezone.utc) - timedelta(minutes=3)
        seen_paid_at: list[datetime] = []
        service, items, _ = pipeline_service(
            eligible_account(emailAccessUrl="https://mail.example.test/paid"),
            eligible_item(
                stage="paid",
                paymentStatus="completed",
                paidAt=paid_at,
                mailConfirmationStatus="confirmed",
                mailConfirmationSubject="Original confirmation",
                mailConfirmationOrderId="sub_original",
            ),
        )

        def checker(_url: str, _email: str, checked_paid_at: datetime) -> PaidMailCheckResult:
            seen_paid_at.append(checked_paid_at)
            return PaidMailCheckResult(
                status="not_found",
                subject="Unrelated newer mail",
                received_at=None,
                error_code="confirmation_pending",
            )

        service.mail_checker = checker
        result = await service.check_paid_mail(PipelinePaidMailCheckInput(ids=["pipeline-1"]))

        stored = items.documents["pipeline-1"]
        assert seen_paid_at == [paid_at]
        assert result["confirmed"] == 1
        assert stored["mailConfirmationStatus"] == "confirmed"
        assert stored["mailConfirmationSubject"] == "Original confirmation"
        assert stored["mailConfirmationOrderId"] == "sub_original"

    asyncio.run(exercise())


def hero_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "country": "JP",
        "checkoutProxy": "",
        "updateProxy": "",
        "protocolProxy": "http://protocol.example:8080",
        "applyCheckoutUpdate": True,
        "heroSmsEnabled": True,
        "autoPaymentEnabled": False,
        "heroSmsMaxPrice": 0.5,
        "heroSmsChangeNumberRetries": 2,
        "heroSmsNumberWaitSeconds": 60,
        "heroSmsApiKeyConfigured": True,
    }


def test_pipeline_starts_payment_with_hero_sms_japan_number() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="payment_ready",
                extractionStatus="succeeded",
                paymentStatus="pending",
                paymentLink="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
                extractedAt=datetime.now(timezone.utc),
                billingCountry="DE",
            ),
        )
        hero = FakeHeroSms()
        service.hero_sms = hero  # type: ignore[assignment]
        service.agreement = SimpleNamespace(start=lambda: None)
        service.settings = lambda: asyncio.sleep(0, result=hero_settings())  # type: ignore[method-assign]
        requests: list[tuple[str, str, dict[str, Any] | None]] = []

        async def protocol(method: str, path: str, _device: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
            requests.append((method, path, payload))
            return {"job": {"id": "job-1", "status": "queued"}}

        service._protocol_request = protocol  # type: ignore[method-assign]
        result = await service.start_payment("pipeline-1", PipelinePaymentInput())
        assert requests[0][2]["phone"] == "+819012345678"
        assert requests[0][2]["country"] == "DE"
        assert result["heroSmsManaged"] is True
        assert items.documents["pipeline-1"]["heroSmsAttempt"] == 1

    asyncio.run(exercise())


def test_agreement_auto_sms_injects_number_and_selected_protocol_pool() -> None:
    async def exercise() -> None:
        service, _, _ = pipeline_service(eligible_account(), eligible_item())
        hero = FakeHeroSms()
        service.hero_sms = hero  # type: ignore[assignment]
        service.settings = lambda: asyncio.sleep(0, result={  # type: ignore[method-assign]
            **hero_settings(),
            "agreementAutoSmsEnabled": True,
            "heroSmsCountryId": 62,
            "protocolProxy": "",
            "protocolProxyCountry": "TR",
        })

        async def proxy_urls(country: str) -> list[str]:
            assert country == "TR"
            return ["http://user:password@tr.proxy.test:8000"]

        service.resources.enabled_proxy_urls = proxy_urls  # type: ignore[attr-defined]
        prepared, context = await service.prepare_agreement_hero_sms({
            "paypal_url": "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
            "phone": "",
            "country": "JP",
        })

        assert prepared["phone"] == "+819012345678"
        assert prepared["country"] == "JP"
        assert prepared["proxies"] == ["http://user:password@tr.proxy.test:8000"]
        assert context is not None

    asyncio.run(exercise())


def test_pipeline_rotates_timed_out_number_and_submits_received_code() -> None:
    async def exercise() -> None:
        service, items, _ = pipeline_service(
            eligible_account(),
            eligible_item(
                stage="payment_waiting_otp",
                extractionStatus="succeeded",
                paymentStatus="awaiting_otp",
                paymentJobId="job-1",
                paymentDeviceId="device-1",
                heroSmsActivationId="activation-old",
                heroSmsStatus="waiting_sms",
                heroSmsAttempt=1,
                heroSmsWaitDeadline=datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        )
        hero = FakeHeroSms()
        service.hero_sms = hero  # type: ignore[assignment]
        submitted: list[str] = []

        async def protocol(_method: str, _path: str, _device: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
            submitted.append(str((payload or {}).get("value") or ""))
            return {"ok": True}

        service._protocol_request = protocol  # type: ignore[method-assign]
        await service._reconcile_hero_sms(items.documents["pipeline-1"], hero_settings())
        assert hero.cancelled == ["activation-old"]
        assert submitted == ["+819012345678"]
        assert items.documents["pipeline-1"]["heroSmsAttempt"] == 2

        hero.statuses = [HeroSmsStatus("received", "123456", "STATUS_OK")]
        current = items.documents["pipeline-1"]
        current["heroSmsLastPollAt"] = None
        current["heroSmsStatus"] = "waiting_sms"
        await service._reconcile_hero_sms(current, hero_settings())
        assert submitted[-1] == "123456"
        assert hero.completed == ["activation-1"]
        assert items.documents["pipeline-1"]["heroSmsStatus"] == "code_submitted"

    asyncio.run(exercise())


class FakePipelineApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def ensure_indexes(self) -> None: pass
    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def settings(self) -> dict[str, Any]:
        return {"enabled": False, "country": "JP", "checkoutProxy": "", "updateProxy": "", "protocolProxy": "", "applyCheckoutUpdate": True}

    async def update_settings(self, payload: Any) -> dict[str, Any]:
        self.calls.append(("settings", payload))
        return {**await self.settings(), **payload.model_dump()}

    async def list_items(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", kwargs))
        return {"items": [], "total": 0, "page": kwargs["page"], "pageSize": kwargs["page_size"], "counts": {}}

    async def item_logs(self, item_id: str) -> dict[str, Any]:
        self.calls.append(("logs", item_id))
        return {
            "itemId": item_id,
            "email": "fixture@example.test",
            "stage": "payment_failed",
            "logs": [{
                "id": "log-1",
                "timestamp": datetime.now(timezone.utc),
                "level": "error",
                "event": "payment.failed",
                "message": "fixture failure",
                "code": "protocol_failed",
                "details": {},
            }],
        }

    async def paid_stats(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("paid_stats", kwargs))
        return {"total": 2, "today": 1, "last7Days": 2, "terminalTotal": 3, "failed": 1, "successRate": 66.7, "averageHeroSmsPrice": 0.4, "daily": []}

    async def export_paid(self, payload: Any) -> dict[str, Any]:
        self.calls.append(("paid_export", payload))
        return {"content": "fixture@example.test----https://mail.example.test", "filename": "paid.txt", "count": 1, "skippedMissingUrlCount": 0}

    async def mark_paid_export_status(self, payload: Any) -> dict[str, int]:
        self.calls.append(("export_status", payload))
        return {"updated": len(payload.ids)}

    async def check_paid_mail(self, payload: Any) -> dict[str, Any]:
        self.calls.append(("mail_check", payload))
        return {"requested": len(payload.ids), "checked": len(payload.ids), "confirmed": len(payload.ids), "notFound": 0, "failed": 0, "items": []}

    async def sync_eligible(self) -> dict[str, int]: return {"eligible": 1, "inserted": 1}
    async def tick(self) -> None: pass
    async def start_extractions(self, ids: list[str]) -> dict[str, int]: return {"requested": len(ids), "started": len(ids)}
    async def hero_sms_test(self) -> dict[str, Any]: return {"ok": True, "configured": True, "country": "JP", "service": "PayPal", "balance": 12.34}
    async def start_payment(self, item_id: str, payload: Any) -> dict[str, Any]:
        self.calls.append(("payment", (item_id, payload.phone)))
        return {"id": item_id, "stage": "paying"}
    async def submit_otp(self, item_id: str, payload: Any) -> dict[str, Any]:
        self.calls.append(("otp", (item_id, payload.value)))
        return {"id": item_id, "stage": "paying"}
    async def reset_stage(self, item_id: str, stage: str) -> dict[str, Any]: return {"id": item_id, "stage": stage}
    async def delete(self, _item_id: str) -> int: return 1


def test_pipeline_api_exposes_management_actions(tmp_path: Path) -> None:
    service = FakePipelineApi()
    app = create_app(
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        mongo_manager=MongoManager(uri="mongodb://127.0.0.1:1", database_name="pipeline_api"),
        pipeline_service=service,  # type: ignore[arg-type]
    )
    client = TestClient(app)

    assert client.get("/api/pipeline/settings").json()["country"] == "JP"
    settings_response = client.put("/api/pipeline/settings", json={
        "enabled": False, "checkoutProxy": "", "updateProxy": "", "protocolProxy": "", "applyCheckoutUpdate": True,
        "extractionFailureRetries": 3, "paymentFailureRetries": 4,
    })
    assert settings_response.status_code == 200
    assert settings_response.json()["extractionFailureRetries"] == 3
    assert settings_response.json()["paymentFailureRetries"] == 4
    assert client.get("/api/pipeline?page=2&pageSize=20&stage=paid&q=fixture&settlementState=confirmed").json()["page"] == 2
    assert client.get("/api/pipeline/pipeline-1/logs").json()["logs"][0]["code"] == "protocol_failed"
    assert client.get("/api/pipeline/paid/stats?days=14").json()["total"] == 2
    assert client.post("/api/pipeline/paid/export", json={"ids": ["pipeline-1"], "query": "fixture"}).json()["count"] == 1
    assert client.post("/api/pipeline/paid/export-status", json={"ids": ["pipeline-1"], "exported": True}).json()["updated"] == 1
    assert client.post("/api/pipeline/paid/mail-check", json={"ids": ["pipeline-1"]}).json()["confirmed"] == 1
    assert client.post("/api/pipeline/sync").json()["inserted"] == 1
    assert client.post("/api/pipeline/extract", json={"ids": ["pipeline-1"]}).status_code == 202
    assert client.post("/api/pipeline/herosms/test").json()["balance"] == 12.34
    assert client.post("/api/pipeline/pipeline-1/payment", json={"phone": "+819012345678", "protocolProxy": "http://proxy.example:8080"}).status_code == 202
    assert client.post("/api/pipeline/pipeline-1/otp", json={"value": "123456"}).status_code == 200
    assert client.post("/api/pipeline/pipeline-1/retry-extraction").status_code == 200
    assert client.post("/api/pipeline/pipeline-1/retry-payment").status_code == 200
    assert client.delete("/api/pipeline/pipeline-1").json() == {"deleted": 1}
    assert ("payment", ("pipeline-1", "+819012345678")) in service.calls
    assert ("otp", ("pipeline-1", "123456")) in service.calls
    assert ("paid_stats", {"days": 14}) in service.calls
    assert ("logs", "pipeline-1") in service.calls
    assert (
        "list",
        {
            "page": 2,
            "page_size": 20,
            "stage": "paid",
            "query": "fixture",
                "export_state": "all",
                "settlement_state": "confirmed",
                "receiver_state": "all",
        },
    ) in service.calls
