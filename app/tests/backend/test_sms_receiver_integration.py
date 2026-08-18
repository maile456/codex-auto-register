from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from backend.pipeline_service import (
    AccountPipelineService,
    PipelinePaidExportInput,
    PipelineServiceError,
    SmsReceiverBatchInput,
    SmsReceiverRetryInput,
    SmsReceiverHeroSmsSettingsUpdate,
    SmsReceiverSettingsUpdate,
)


class _Cursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def sort(self, *_args: Any, **_kwargs: Any) -> "_Cursor":
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        rows = self.documents if length is None else self.documents[:length]
        return [dict(row) for row in rows]


class _Collection:
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = {str(row["_id"]): dict(row) for row in documents or []}
        self.find_calls: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    @classmethod
    def _matches(cls, row: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            if key == "$or":
                if not any(cls._matches(row, branch) for branch in expected):
                    return False
                continue
            actual = row.get(key)
            if not isinstance(expected, dict):
                if actual != expected:
                    return False
                continue
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                return False
            if "$exists" in expected and (key in row) is not bool(expected["$exists"]):
                return False
        return True

    def find(
        self,
        query: dict[str, Any],
        projection: dict[str, Any] | None = None,
    ) -> _Cursor:
        self.find_calls.append((query, projection))
        return _Cursor(
            [row for row in self.documents.values() if self._matches(row, query)]
        )

    async def find_one(
        self,
        query: dict[str, Any],
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        return next(
            (dict(row) for row in self.documents.values() if self._matches(row, query)),
            None,
        )

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> Any:
        count = 0
        for key, row in list(self.documents.items()):
            if not self._matches(row, query):
                continue
            row.update(update.get("$set", {}))
            for field, amount in update.get("$inc", {}).items():
                row[field] = int(row.get(field) or 0) + int(amount)
            self.documents[key] = row
            count += 1
        return SimpleNamespace(matched_count=count, modified_count=count)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> Any:
        for key, row in list(self.documents.items()):
            if not self._matches(row, query):
                continue
            row.update(update.get("$set", {}))
            self.documents[key] = row
            return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)


class _Manager:
    online = True

    def __init__(self, items: _Collection, settings: _Collection) -> None:
        self.database = {
            "account_pipeline": items,
            "pipeline_settings": settings,
        }

    def require_online(self) -> None:
        return None

    def mark_offline(self, _exc: Exception) -> None:
        return None


def _service_with_receiver(
    items: list[dict[str, Any]] | None = None,
    accounts: list[dict[str, Any]] | None = None,
) -> tuple[AccountPipelineService, _Collection]:
    item_collection = _Collection(items)
    settings = _Collection(
        [
            {
                "_id": "default",
                "smsReceiverEnabled": True,
                "smsReceiverBaseUrl": "http://receiver.example.test",
            }
        ]
    )
    manager = _Manager(item_collection, settings)
    resources = SimpleNamespace(manager=manager, accounts=_Collection(accounts))
    service = AccountPipelineService(
        resources,
        SimpleNamespace(),
        SimpleNamespace(),
        hero_sms=SimpleNamespace(configured=False),
    )
    return service, item_collection


def _mock_async_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_local_mailbox_url_is_rewritten_to_server() -> None:
    actual = AccountPipelineService._public_mailbox_url(
        "http://127.0.0.1:3211/api/mail/latest?email=user%40example.com",
        "https://mail.example.test/mailbox",
    )
    assert actual == (
        "https://mail.example.test/mailbox/api/mail/latest"
        "?email=user%40example.com"
    )


def test_existing_public_mailbox_url_is_kept() -> None:
    public_url = "https://api.example.test/get_code?email=user%40example.com"
    assert AccountPipelineService._public_mailbox_url(public_url, "") == public_url


def test_local_mailbox_url_requires_public_server_base() -> None:
    with pytest.raises(PipelineServiceError) as captured:
        AccountPipelineService._public_mailbox_url(
            "http://localhost:3211/api/mail/latest?email=user%40example.com",
            "",
        )
    assert captured.value.code == "mailbox_public_base_url_required"


def test_local_mailbox_url_is_kept_for_local_receiver() -> None:
    local_url = "http://127.0.0.1:3211/api/mail/latest?email=user%40example.com"
    assert AccountPipelineService._public_mailbox_url(
        local_url,
        "",
        "http://127.0.0.1:5015",
    ) == local_url


def test_receiver_server_urls_are_normalized() -> None:
    settings = SmsReceiverSettingsUpdate(
        enabled=True,
        baseUrl="https://sms.example.test/",
        mailboxPublicBaseUrl="https://mail.example.test/",
    )
    assert settings.baseUrl == "https://sms.example.test"
    assert settings.mailboxPublicBaseUrl == "https://mail.example.test"


def test_receiver_server_url_defaults_to_empty() -> None:
    settings = SmsReceiverSettingsUpdate()
    assert settings.baseUrl == ""
    assert settings.mailboxPublicBaseUrl == ""
    assert settings.concurrency == 3
    assert settings.failureRetries == 1
    assert settings.retryBackoffSeconds == 30

    with pytest.raises(ValueError):
        SmsReceiverSettingsUpdate(concurrency=11)
    with pytest.raises(ValueError):
        SmsReceiverSettingsUpdate(failureRetries=4)
    with pytest.raises(ValueError):
        SmsReceiverSettingsUpdate(retryBackoffSeconds=4)


def test_receiver_hero_sms_settings_are_mapped_without_revealing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "config": {
                        "countries": ["16", "182"],
                        "min_price": "0.05",
                        "max_price": "0.11",
                        "preferred_price": "0.08",
                        "acquire_priority": "price",
                        "max_retries": 7,
                        "code_wait": 90,
                        "email_otp_wait": 120,
                        "email_otp_poll_interval": 4,
                        "email_otp_attempts": 2,
                        "reuse_enabled": True,
                        "credential_configured": True,
                        "api_key": "must-not-leak",
                    },
                },
            )
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "config": {
                    "countries": ["182"],
                    "min_price": "0.10",
                    "max_price": "0.25",
                    "preferred_price": "0.20",
                    "acquire_priority": "country",
                    "max_retries": 4,
                    "code_wait": 120,
                    "email_otp_wait": 150,
                    "email_otp_poll_interval": 5,
                    "email_otp_attempts": 3,
                    "reuse_enabled": False,
                    "credentials_configured": {"hero": True},
                    "credential": "must-not-leak",
                },
            },
        )

    _mock_async_client(monkeypatch, handler)
    service, _ = _service_with_receiver()

    async def exercise() -> None:
        loaded = await service.sms_receiver_hero_sms_settings()
        assert loaded == {
            "countryIds": [16, 182],
            "minPrice": 0.05,
            "maxPrice": 0.11,
            "preferredPrice": 0.08,
            "acquirePriority": "price",
            "maxRetries": 7,
            "codeWaitSeconds": 90,
            "emailOtpWaitSeconds": 120,
            "emailOtpPollIntervalSeconds": 4,
            "emailOtpAttempts": 2,
            "reuseEnabled": True,
            "credentialConfigured": True,
        }
        assert "must-not-leak" not in json.dumps(loaded)

        saved = await service.update_sms_receiver_hero_sms_settings(
            SmsReceiverHeroSmsSettingsUpdate(
                apiKey="hero-secret",
                countryIds=[182],
                minPrice=0.1,
                maxPrice=0.25,
                preferredPrice=0.2,
                acquirePriority="country",
                maxRetries=4,
                codeWaitSeconds=120,
                emailOtpWaitSeconds=150,
                emailOtpPollIntervalSeconds=5,
                emailOtpAttempts=3,
                reuseEnabled=False,
            )
        )
        assert saved["credentialConfigured"] is True

    asyncio.run(exercise())
    assert captured == {
        "provider": "hero",
        "countries": ["182"],
        "service": "dr",
        "min_price": 0.1,
        "max_price": 0.25,
        "preferred_price": 0.2,
        "acquire_priority": "country",
        "max_retries": 4,
        "code_wait": 120,
        "email_otp_wait": 150,
        "email_otp_poll_interval": 5,
        "email_otp_attempts": 3,
        "reuse_enabled": False,
        "credential": "hero-secret",
    }


def test_receiver_hero_sms_validation_and_upstream_error_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError):
        SmsReceiverHeroSmsSettingsUpdate(
            countryIds=[],
            maxPrice=0.1,
            maxRetries=3,
            codeWaitSeconds=60,
        )
    with pytest.raises(ValueError):
        SmsReceiverHeroSmsSettingsUpdate(
            countryIds=[182],
            maxPrice=0.1,
            maxRetries=0,
            codeWaitSeconds=60,
        )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "error": "OAuth 任务运行中"})

    _mock_async_client(monkeypatch, handler)
    service, _ = _service_with_receiver()

    async def exercise() -> None:
        with pytest.raises(PipelineServiceError) as captured:
            await service.sms_receiver_hero_sms_settings()
        assert captured.value.status_code == 409
        assert captured.value.message == "OAuth 任务运行中"

    asyncio.run(exercise())


def test_receiver_catalog_never_returns_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "countries": [{"id": "182", "name": "日本", "apiKey": "leak"}],
                "api_key": "leak",
                "service": {"code": "dr"},
            },
        )

    _mock_async_client(monkeypatch, handler)
    service, _ = _service_with_receiver()
    result = asyncio.run(service.sms_receiver_hero_sms_catalog())
    assert "leak" not in json.dumps(result)
    assert result["countries"][0]["id"] == "182"


def test_paid_receiver_submit_uses_password_totp_and_skips_invalid_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.method, request.url.path, payload))
        if request.url.path == "/api/accounts/import":
            return httpx.Response(
                200,
                json={"ok": True, "parsed": 1, "inserted": 1, "updated": 0},
            )
        if request.url.path == "/api/v1/integration/accounts/submit":
            return httpx.Response(
                202,
                json={
                    "ok": True,
                    "started": True,
                    "account": {
                        "state": "queued",
                        "credential_ready": False,
                        "phone_verified": False,
                        "task": {"id": "fixture-task"},
                    },
                },
            )
        return httpx.Response(404)

    _mock_async_client(monkeypatch, handler)
    service, items = _service_with_receiver(
        [
            {
                "_id": "paid-ready",
                "accountId": "account-ready",
                "email": "ready@example.test",
                "stage": "paid",
            },
            {
                "_id": "paid-missing-password",
                "accountId": "account-missing-password",
                "email": "missing-password@example.test",
                "stage": "paid",
            },
            {
                "_id": "paid-missing-totp",
                "accountId": "account-missing-totp",
                "email": "missing-totp@example.test",
                "stage": "paid",
            },
            {
                "_id": "paid-invalid-totp",
                "accountId": "account-invalid-totp",
                "email": "invalid-totp@example.test",
                "stage": "paid",
            },
        ],
        [
            {
                "_id": "account-ready",
                "email": "ready@example.test",
                "chatgptPassword": "Password|Fixture",
                "totpSecret": "jbsw y3dp-ehpk3pxp",
                "emailAccessUrl": "https://mail.example.test/unused",
            },
            {
                "_id": "account-missing-password",
                "email": "missing-password@example.test",
                "chatgptPassword": "",
                "totpSecret": "JBSWY3DPEHPK3PXP",
            },
            {
                "_id": "account-missing-totp",
                "email": "missing-totp@example.test",
                "chatgptPassword": "Password!MissingTotp",
                "totpSecret": "",
            },
            {
                "_id": "account-invalid-totp",
                "email": "invalid-totp@example.test",
                "chatgptPassword": "Password!InvalidTotp",
                "totpSecret": "invalid!secret",
            },
        ],
    )

    result = asyncio.run(
        service.submit_paid_to_sms_receiver(
            SmsReceiverBatchInput(
                ids=[
                    "paid-ready",
                    "paid-missing-password",
                    "paid-missing-totp",
                    "paid-invalid-totp",
                ]
            )
        )
    )

    projection = service.resources.accounts.find_calls[-1][1]
    assert projection is not None
    assert {"email", "chatgptPassword", "totpSecret"}.issubset(projection)
    assert requests == [
        (
            "POST",
            "/api/accounts/import",
            {
                "source": "password_totp",
                "text": (
                    "ready@example.test|Password|Fixture|JBSWY3DPEHPK3PXP"
                ),
            },
        ),
        (
            "POST",
            "/api/v1/integration/accounts/submit",
            {"email": "ready@example.test"},
        ),
    ]
    assert result["requested"] == 4
    assert result["processed"] == 4
    assert result["submitted"] == 1
    assert result["skipped"] == 3
    assert result["failed"] == 0
    assert [item["state"] for item in result["items"]].count("skipped") == 3
    serialized = json.dumps(result, ensure_ascii=False)
    assert "Password|Fixture" not in serialized
    assert "JBSWY3DPEHPK3PXP" not in serialized
    assert items.documents["paid-ready"]["smsReceiverState"] == "queued"
    assert items.documents["paid-ready"]["smsReceiverMailboxUrl"] is None
    assert items.documents["paid-missing-password"]["smsReceiverState"] == "failed"
    assert "缺少 ChatGPT 密码" in items.documents["paid-missing-password"][
        "smsReceiverError"
    ]
    assert items.documents["paid-missing-totp"]["smsReceiverState"] == "failed"
    assert "缺少 2FA 密钥" in items.documents["paid-missing-totp"][
        "smsReceiverError"
    ]
    assert items.documents["paid-invalid-totp"]["smsReceiverState"] == "failed"
    assert "Base32" in items.documents["paid-invalid-totp"]["smsReceiverError"]


def test_paid_receiver_submit_retries_transient_failures_with_saved_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    import_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal import_attempts
        calls.append(request.url.path)
        if request.url.path == "/api/accounts/import":
            import_attempts += 1
            if import_attempts == 1:
                return httpx.Response(503, json={"ok": False, "error": "temporary"})
            return httpx.Response(200, json={"ok": True, "parsed": 1})
        return httpx.Response(
            202,
            json={
                "ok": True,
                "started": True,
                "account": {"state": "queued", "task": {"id": "retry-task"}},
            },
        )

    async def no_sleep(_seconds: float) -> None:
        return None

    _mock_async_client(monkeypatch, handler)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    service, items = _service_with_receiver(
        [
            {
                "_id": "paid-retry",
                "accountId": "account-retry",
                "email": "retry@example.test",
                "stage": "paid",
            }
        ],
        [
            {
                "_id": "account-retry",
                "email": "retry@example.test",
                "chatgptPassword": "Password!Retry",
                "totpSecret": "JBSWY3DPEHPK3PXP",
            }
        ],
    )
    service.settings_collection.documents["default"].update(
        smsReceiverConcurrency=2,
        smsReceiverFailureRetries=2,
        smsReceiverRetryBackoffSeconds=5,
    )

    result = asyncio.run(
        service.submit_paid_to_sms_receiver(SmsReceiverBatchInput(ids=["paid-retry"]))
    )

    assert calls == [
        "/api/accounts/import",
        "/api/accounts/import",
        "/api/v1/integration/accounts/submit",
    ]
    assert result["submitted"] == 1
    assert result["failed"] == 0
    assert items.documents["paid-retry"]["smsReceiverRetryCount"] == 1


def test_failed_receiver_jobs_are_persisted_in_retry_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, items = _service_with_receiver(
        [
            {
                "_id": "paid-failed-1",
                "accountId": "account-failed-1",
                "email": "failed-1@example.test",
                "stage": "paid",
                "smsReceiverState": "failed",
            },
            {
                "_id": "paid-failed-2",
                "accountId": "account-failed-2",
                "email": "failed-2@example.test",
                "stage": "paid",
                "smsReceiverState": "stopped",
            },
        ],
        [
            {
                "_id": "account-failed-1",
                "email": "failed-1@example.test",
                "chatgptPassword": "Password!Failed1",
                "totpSecret": "JBSWY3DPEHPK3PXP",
            },
            {
                "_id": "account-failed-2",
                "email": "failed-2@example.test",
                "chatgptPassword": "Password!Failed2",
                "totpSecret": "JBSWY3DPEHPK3PXP",
            },
        ],
    )

    async def no_op(_item_id: str) -> None:
        return None

    monkeypatch.setattr(service, "_auto_submit_paid_to_sms_receiver", no_op)
    result = asyncio.run(
        service.queue_sms_receiver_retry(
            SmsReceiverRetryInput(ids=["paid-failed-1", "paid-failed-2"])
        )
    )

    assert result["queued"] == 2
    assert result["skipped"] == 0
    assert items.documents["paid-failed-1"]["smsReceiverState"] == "waiting"
    assert items.documents["paid-failed-2"]["smsReceiverState"] == "waiting"


def test_paid_receiver_submit_respects_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_requests = 0
    max_active_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, max_active_requests
        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        try:
            await asyncio.sleep(0.01)
            if request.url.path == "/api/accounts/import":
                return httpx.Response(200, json={"ok": True, "parsed": 1})
            return httpx.Response(
                202,
                json={
                    "ok": True,
                    "started": True,
                    "account": {"state": "queued", "task": {"id": "concurrency-task"}},
                },
            )
        finally:
            active_requests -= 1

    _mock_async_client(monkeypatch, handler)
    item_ids = [f"paid-concurrency-{index}" for index in range(4)]
    items = [
        {
            "_id": item_id,
            "accountId": item_id.replace("paid-", "account-"),
            "email": f"concurrency-{index}@example.test",
            "stage": "paid",
        }
        for index, item_id in enumerate(item_ids)
    ]
    accounts = [
        {
            "_id": item["accountId"],
            "email": item["email"],
            "chatgptPassword": "Password!Concurrency",
            "totpSecret": "JBSWY3DPEHPK3PXP",
        }
        for item in items
    ]
    service, _ = _service_with_receiver(items, accounts)
    service.settings_collection.documents["default"].update(
        smsReceiverConcurrency=2,
        smsReceiverFailureRetries=0,
        smsReceiverRetryBackoffSeconds=5,
    )

    result = asyncio.run(
        service.submit_paid_to_sms_receiver(SmsReceiverBatchInput(ids=item_ids))
    )

    assert result["submitted"] == 4
    assert result["failed"] == 0
    assert max_active_requests == 2


def test_credential_export_uses_receiver_binary_and_marks_only_exported_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/accounts":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "accounts": [
                        {
                            "id": "receiver-1",
                            "email": "ready@example.test",
                            "has_credential": True,
                        },
                        {
                            "id": "receiver-2",
                            "email": "missing@example.test",
                            "has_credential": False,
                        },
                    ],
                },
            )
        requested.update(json.loads(request.content))
        return httpx.Response(
            200,
            content=b'{"type":"sub2api-data"}\n',
            headers={
                "content-type": "application/json; charset=utf-8",
                "content-disposition": "attachment; filename*=UTF-8''receiver-sub2.json",
            },
        )

    _mock_async_client(monkeypatch, handler)
    service, items = _service_with_receiver(
        [
            {
                "_id": "paid-1",
                "accountId": "account-1",
                "email": "READY@example.test",
                "stage": "paid",
            },
            {
                "_id": "paid-2",
                "accountId": "account-2",
                "email": "missing@example.test",
                "stage": "paid",
            },
        ]
    )
    result = asyncio.run(
        service.export_paid(PipelinePaidExportInput(format="sub2api"))
    )

    assert requested == {
        "confirmed": True,
        "account_ids": ["receiver-1"],
        "formats": ["sub2api"],
    }
    assert result["content"] == ""
    assert base64.b64decode(result["contentBase64"]) == b'{"type":"sub2api-data"}\n'
    assert result["encoding"] == "base64"
    assert result["mimeType"] == "application/json"
    assert result["filename"] == "receiver-sub2.json"
    assert result["count"] == 1
    assert result["skippedMissingCredentialCount"] == 1
    assert items.documents["paid-1"]["exportCount"] == 1
    assert "exportCount" not in items.documents["paid-2"]


def test_password_totp_export_includes_only_complete_paid_accounts() -> None:
    service, items = _service_with_receiver(
        [
            {
                "_id": "paid-ready",
                "accountId": "account-ready",
                "email": "ready@example.test",
                "stage": "paid",
                "exportCount": 2,
            },
            {
                "_id": "paid-missing-password",
                "accountId": "account-missing-password",
                "email": "missing-password@example.test",
                "stage": "paid",
            },
            {
                "_id": "paid-missing-totp",
                "accountId": "account-missing-totp",
                "email": "missing-totp@example.test",
                "stage": "paid",
            },
            {
                "_id": "eligible-ready",
                "accountId": "account-eligible",
                "email": "eligible@example.test",
                "stage": "eligible",
            },
        ],
        [
            {
                "_id": "account-ready",
                "email": "ready@example.test",
                "chatgptPassword": "Password!Fixture",
                "totpSecret": "TOTPREADYFIXTURE",
            },
            {
                "_id": "account-missing-password",
                "email": "missing-password@example.test",
                "chatgptPassword": "",
                "totpSecret": "TOTPMISSINGPASS",
            },
            {
                "_id": "account-missing-totp",
                "email": "missing-totp@example.test",
                "chatgptPassword": "Password!MissingTotp",
                "totpSecret": "",
            },
            {
                "_id": "account-eligible",
                "email": "eligible@example.test",
                "chatgptPassword": "Password!Eligible",
                "totpSecret": "TOTPELIGIBLE",
            },
        ],
    )

    result = asyncio.run(
        service.export_paid(PipelinePaidExportInput(format="password_totp"))
    )

    assert result["content"] == (
        "ready@example.test----Password!Fixture----TOTPREADYFIXTURE"
    )
    assert result["contentBase64"] is None
    assert result["encoding"] == "utf-8"
    assert result["mimeType"] == "text/plain"
    assert result["format"] == "password_totp"
    assert result["filename"].startswith("paid-accounts-1-password-totp-")
    assert result["count"] == 1
    assert result["skippedMissingSecurityCount"] == 2
    assert items.documents["paid-ready"]["exportCount"] == 3
    assert "exportCount" not in items.documents["paid-missing-password"]
    assert "exportCount" not in items.documents["paid-missing-totp"]
    assert "exportCount" not in items.documents["eligible-ready"]


def test_paid_export_input_accepts_ordered_unique_formats_and_keeps_legacy_format() -> None:
    legacy = PipelinePaidExportInput(format="password_totp")
    assert legacy.format == "password_totp"
    assert legacy.formats is None

    multi = PipelinePaidExportInput(
        formats=["password_totp", "original", "password_totp"]
    )
    assert multi.formats == ["password_totp", "original"]

    with pytest.raises(ValueError):
        PipelinePaidExportInput(formats=[])


def test_multi_format_export_returns_separate_artifacts_in_requested_order() -> None:
    service, items = _service_with_receiver(
        [
            {
                "_id": "paid-ready",
                "accountId": "account-ready",
                "email": "ready@example.test",
                "stage": "paid",
            }
        ],
        [
            {
                "_id": "account-ready",
                "email": "ready@example.test",
                "emailAccessUrl": "https://mail.example.test/latest",
                "chatgptPassword": "Password!Fixture",
                "totpSecret": "TOTPREADYFIXTURE",
            }
        ],
    )

    result = asyncio.run(
        service.export_paid(
            PipelinePaidExportInput(formats=["password_totp", "original"])
        )
    )

    assert result["formats"] == ["password_totp", "original"]
    assert [artifact["format"] for artifact in result["exports"]] == [
        "password_totp",
        "original",
    ]
    assert result["exports"][0]["content"] == (
        "ready@example.test----Password!Fixture----TOTPREADYFIXTURE"
    )
    assert result["exports"][1]["content"] == (
        "ready@example.test----https://mail.example.test/latest"
    )
    assert result["errors"] == []
    assert result["artifactCount"] == 2
    assert result["failedFormatCount"] == 0
    assert items.documents["paid-ready"]["exportCount"] == 2


def test_multi_format_export_reports_receiver_failure_without_losing_local_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "receiver unavailable"})

    _mock_async_client(monkeypatch, handler)
    service, items = _service_with_receiver(
        [
            {
                "_id": "paid-ready",
                "accountId": "account-ready",
                "email": "ready@example.test",
                "stage": "paid",
            }
        ],
        [
            {
                "_id": "account-ready",
                "email": "ready@example.test",
                "emailAccessUrl": "https://mail.example.test/latest",
            }
        ],
    )

    result = asyncio.run(
        service.export_paid(
            PipelinePaidExportInput(formats=["original", "sub2api"])
        )
    )

    assert [artifact["format"] for artifact in result["exports"]] == ["original"]
    assert result["errors"] == [
        {
            "format": "sub2api",
            "code": "sms_receiver_upstream_error",
            "message": "receiver unavailable",
            "statusCode": 503,
        }
    ]
    assert result["artifactCount"] == 1
    assert result["failedFormatCount"] == 1
    assert items.documents["paid-ready"]["exportCount"] == 1
