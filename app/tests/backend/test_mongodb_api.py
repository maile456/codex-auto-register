from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.mongo_manager import MongoManager
from backend.resource_service import (
    MongoResourceStore,
    ResourceService,
    infer_proxy_country,
    interleave_email_parent_groups,
    proxy_country_filter,
)
from backend.resource_models import (
    AccountExportInput,
    AccountPlanCheckItem,
    AccountPlanCheckResult,
)


def test_email_candidates_are_interleaved_across_parent_mailboxes() -> None:
    candidates = [
        {"_id": "a1", "parentEmail": "a@example.com"},
        {"_id": "a2", "parentEmail": "a@example.com"},
        {"_id": "b1", "parentEmail": "b@example.com"},
        {"_id": "b2", "parentEmail": "b@example.com"},
        {"_id": "manual", "emailNormalized": "manual@example.net"},
        {"_id": "a3", "parentEmail": "a@example.com"},
    ]

    ordered = interleave_email_parent_groups(candidates)

    assert [item["_id"] for item in ordered] == [
        "a1",
        "b1",
        "manual",
        "a2",
        "b2",
        "a3",
    ]


def test_resource_records_expose_browser_ready_api798_urls(monkeypatch) -> None:
    monkeypatch.setenv("API798_AUTH_CODE", "AUTH_FIXTURE")
    created_at = datetime.now(timezone.utc)
    account = MongoResourceStore._account_record(
        {
            "_id": "account-fixture",
            "email": "person@example.com",
            "chatgptPassword": "PASSWORD_FIXTURE",
            "totpSecret": "TOTP_FIXTURE",
            "emailAccessUrl": (
                "https://api798.com/get_code?email=person%40example.com"
            ),
            "createdAt": created_at,
            "accountType": "free",
        }
    )
    email = MongoResourceStore._email_record(
        {
            "_id": "email-fixture",
            "email": "person@example.com",
            "accessUrl": "https://api798.com/get_code?email=person%40example.com",
            "importedAt": created_at,
        }
    )

    expected = (
        "https://api798.com/latest?email=person%40example.com"
        "&auth_code=AUTH_FIXTURE"
    )
    assert account.emailAccessUrl == expected
    assert email.accessUrl == expected


class ImportStore:
    def __init__(self) -> None:
        self.emails: set[str] = set()
        self.email_options: list[tuple[str, str | None]] = []
        self.email_sources: list[tuple[str, str | None]] = []
        self.proxies: set[tuple[str, int, str, str]] = set()
        self.proxy_schemes: list[str] = []
        self.proxy_groups: list[str | None] = []

    async def upsert_email(
        self,
        email: str,
        access_url: str,
        *,
        mailbox_kind: str = "url",
        mailbox_password: str | None = None,
        source_type: str = "manual",
        parent_email: str | None = None,
    ) -> bool:
        self.email_options.append((mailbox_kind, mailbox_password))
        self.email_sources.append((source_type, parent_email))
        key = f"{email}|{access_url}"
        if key in self.emails:
            return False
        self.emails.add(key)
        return True

    async def upsert_proxy(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        scheme: str = "http",
        country: str | None = None,
        group: str | None = None,
    ) -> bool:
        _ = country, group
        self.proxy_schemes.append(scheme)
        self.proxy_groups.append(group)
        key = (host, port, username, password)
        if key in self.proxies:
            return False
        self.proxies.add(key)
        return True


def test_resource_routes_fail_fast_while_mongodb_is_offline(tmp_path: Path) -> None:
    manager = MongoManager(uri="mongodb://127.0.0.1:1", database_name="offline_test")
    client = TestClient(
        create_app(
            settings_path=tmp_path / "settings.json",
            log_dir=tmp_path / "logs",
            mongo_manager=manager,
        )
    )

    for method, path, payload in [
        ("get", "/api/accounts", None),
        ("post", "/api/emails/import", {"rawText": ""}),
        ("post", "/api/proxies/import", {"rawText": ""}),
        ("post", "/api/proxies/bulk-delete", {"ids": ["proxy-id"]}),
        ("post", "/api/accounts/check-promotion", {"ids": ["account-id"]}),
        ("delete", "/api/proxies", None),
        ("get", "/api/stats/overview", None),
    ]:
        response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "mongodb_unavailable",
                "message": "MongoDB 当前不可用，请检查本机服务",
            }
        }

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/settings/execution").status_code == 200
    assert client.get("/api/run-logs/runs").status_code == 200
    assert client.get("/api/docs").status_code == 200


class FakeOnlineMongo(MongoManager):
    def require_online(self) -> None:
        return None


class FakePlanCheckService:
    def __init__(self) -> None:
        self.ids: list[str] = []

    async def check_accounts(self, ids: list[str]) -> AccountPlanCheckResult:
        self.ids = ids
        return AccountPlanCheckResult(
            requested=len(ids),
            succeeded=1,
            failed=0,
            skipped=max(0, len(ids) - 1),
            items=[
                AccountPlanCheckItem(id=ids[0], status="success"),
                *[
                    AccountPlanCheckItem(
                        id=value,
                        status="skipped",
                        errorCode="account_missing_token_or_busy",
                    )
                    for value in ids[1:]
                ],
            ],
        )


def test_promotion_check_api_uses_account_id_batch(tmp_path: Path) -> None:
    manager = FakeOnlineMongo(
        uri="mongodb://127.0.0.1:1", database_name="plan_api_test"
    )
    app = create_app(
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        mongo_manager=manager,
    )
    service = FakePlanCheckService()
    app.state.plan_check_service = service
    client = TestClient(app)

    response = client.post(
        "/api/accounts/check-promotion",
        json={"ids": ["one", "two"]},
    )

    assert response.status_code == 200
    assert service.ids == ["one", "two"]
    assert response.json()["succeeded"] == 1
    assert response.json()["skipped"] == 1


def test_server_side_import_validation_and_proxy_password_colons() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]

    email_result = asyncio.run(
        service.import_emails(
            "\ufeffA@Example.com----https://example.com/s/secret-token/a@example.com\n"
            "a@example.com----https://example.com/duplicate\n"
            "bad-email----https://example.com/a\n"
            "b@example.com----file:///private/token\n"
        )
    )
    assert email_result.model_dump() == {
        "total": 4,
        "imported": 1,
        "duplicateCount": 1,
        "errorCount": 2,
    }

    proxy_result = asyncio.run(
        service.import_proxies(
            "proxy.example.test:10000:test-user:TEST_PASSWORD:tail\n"
            "proxy.example.test:10000:test-user:TEST_PASSWORD:tail\n"
            "host:70000:user:password\n"
        )
    )
    assert proxy_result.model_dump() == {
        "total": 3,
        "imported": 1,
        "duplicateCount": 1,
        "errorCount": 1,
    }
    assert store.proxies == {
        (
            "proxy.example.test",
            10000,
            "test-user",
            "TEST_PASSWORD:tail",
        )
    }


def test_server_side_import_accepts_mailcom_password_without_public_exposure() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]

    result = asyncio.run(
        service.import_emails(
            "person@gardener.com----mail-password\n"
            "broken@example.com----file:///private/mail-password\n"
        )
    )

    assert result.model_dump() == {
        "total": 2,
        "imported": 1,
        "duplicateCount": 0,
        "errorCount": 1,
    }
    assert store.emails == {
        "person@gardener.com|https://www.mail.com/int/"
    }
    assert store.email_options == [("mailcom_imap", "mail-password")]


def test_mailcom_alias_sync_imports_only_alias_items_with_local_urls() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.sync_mailcom_aliases(
            [
                {
                    "email": "owner@gardener.com",
                    "accountEmail": "owner@gardener.com",
                    "isAlias": False,
                    "accessUrl": (
                        "http://127.0.0.1:3211/api/mail/latest?"
                        "email=owner%40gardener.com"
                    ),
                },
                {
                    "email": "alias.one@example.com",
                    "accountEmail": "owner@gardener.com",
                    "isAlias": True,
                    "accessUrl": (
                        "http://127.0.0.1:3211/api/mail/latest?"
                        "email=alias.one%40example.com"
                    ),
                },
                {
                    "email": "outside@example.com",
                    "accountEmail": "owner@gardener.com",
                    "isAlias": True,
                    "accessUrl": (
                        "https://outside.example/api/mail/latest?"
                        "email=outside%40example.com"
                    ),
                },
            ]
        )
    )

    assert result.model_dump() == {
        "total": 2,
        "imported": 1,
        "duplicateCount": 0,
        "errorCount": 1,
    }
    assert store.emails == {
        "alias.one@example.com|"
        "http://127.0.0.1:3211/api/mail/latest?email=alias.one%40example.com"
    }
    assert store.email_sources == [("mailcom_alias", "owner@gardener.com")]


def test_proxy_country_inference_and_explicit_import_classification() -> None:
    assert infer_proxy_country("sid-region-TR-sid-abc") == "TR"
    assert infer_proxy_country("sid_area-TR_life-5") == "TR"
    assert infer_proxy_country("sid-country-JP-sid-abc") == "JP"
    assert infer_proxy_country("plain-user", "edge-area-DE-host") == "DE"
    assert infer_proxy_country("plain-user", "edge.example") == "ZZ"

    country_filter = proxy_country_filter("tr")
    assert country_filter["$or"][0] == {"country": "TR"}
    legacy = country_filter["$or"][1]["$and"]
    assert {"country": "ZZ"} in legacy[0]["$or"]
    assert {"host": legacy[1]["$or"][1]["host"]} in legacy[1]["$or"]

    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.import_proxies(
            "proxy.example.test:10000:plain-user:password\n",
            country="tr",
        )
    )
    assert result.imported == 1


def test_proxy_import_accepts_whitespace_separated_socks5_urls() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.import_proxies(
            "socks5://user-region-GB-sid-one:password@proxy.example.test:3000 "
            "socks5://user-region-GB-sid-two:password@proxy.example.test:3000",
            group="英国住宅公式 A",
        )
    )

    assert result.model_dump() == {
        "total": 2,
        "imported": 2,
        "duplicateCount": 0,
        "errorCount": 0,
    }
    assert len(store.proxies) == 2
    assert store.proxy_schemes == ["socks5", "socks5"]
    assert store.proxy_groups == ["英国住宅公式 A", "英国住宅公式 A"]


def test_proxy_upsert_keeps_mutable_fields_out_of_set_on_insert() -> None:
    class CapturingCollection:
        def __init__(self) -> None:
            self.update: dict | None = None

        async def update_one(self, _identity, update, *, upsert):
            assert upsert is True
            self.update = update
            return type("UpdateResult", (), {"upserted_id": "fixture-id"})()

    class CapturingManager:
        def __init__(self) -> None:
            self.collection = CapturingCollection()
            self.database = {"proxies": self.collection}

        def require_online(self) -> None:
            return None

    manager = CapturingManager()
    store = MongoResourceStore(manager)  # type: ignore[arg-type]

    inserted = asyncio.run(
        store.upsert_proxy(
            "proxy.example.test",
            3000,
            "user-region-GB",
            "password",
            scheme="socks5",
            country="GB",
            group="英国住宅公式 A",
        )
    )

    assert inserted is True
    assert manager.collection.update is not None
    assert manager.collection.update["$set"] == {
        "scheme": "socks5",
        "country": "GB",
        "group": "英国住宅公式 A",
    }
    assert "scheme" not in manager.collection.update["$setOnInsert"]
    assert "country" not in manager.collection.update["$setOnInsert"]
    assert "group" not in manager.collection.update["$setOnInsert"]


def test_mailcom_alias_upsert_avoids_mongodb_path_conflicts() -> None:
    class CapturingAccounts:
        async def find_one(self, _identity, _projection):
            return None

    class CapturingCollection:
        def __init__(self) -> None:
            self.update: dict | None = None

        async def update_one(self, _identity, update, *, upsert):
            assert upsert is True
            self.update = update
            return type("UpdateResult", (), {"upserted_id": "fixture-id"})()

    class CapturingManager:
        def __init__(self) -> None:
            self.collection = CapturingCollection()
            self.database = {
                "accounts": CapturingAccounts(),
                "emails": self.collection,
            }

        def require_online(self) -> None:
            return None

    manager = CapturingManager()
    store = MongoResourceStore(manager)  # type: ignore[arg-type]

    inserted = asyncio.run(
        store.upsert_email(
            "alias@example.com",
            "http://127.0.0.1:3211/api/mail/latest?email=alias%40example.com",
            source_type="mailcom_alias",
            parent_email="owner@example.com",
        )
    )

    assert inserted is True
    assert manager.collection.update is not None
    mutable = manager.collection.update["$set"]
    inserted_fields = manager.collection.update["$setOnInsert"]
    assert mutable == {
        "accessUrl": (
            "http://127.0.0.1:3211/api/mail/latest?email=alias%40example.com"
        ),
        "sourceType": "mailcom_alias",
        "parentEmail": "owner@example.com",
    }
    assert not set(mutable).intersection(inserted_fields)


class AccessTokenExportStore:
    async def access_tokens_for_export(self, ids):
        _ = ids
        now = datetime.now(timezone.utc)
        return [
            {
                "_id": "valid",
                "accessTokenConfigured": True,
                "accessToken": "VALID_TEST_AT",
                "accessTokenExpiresAt": now + timedelta(hours=1),
            },
            {"_id": "missing", "accessTokenConfigured": False},
            {
                "_id": "expired",
                "accessTokenConfigured": True,
                "accessToken": "EXPIRED_TEST_AT",
                "accessTokenExpiresAt": now - timedelta(seconds=1),
            },
        ]


def test_access_token_export_contains_only_valid_tokens_and_reports_skips() -> None:
    service = ResourceService(AccessTokenExportStore())  # type: ignore[arg-type]
    result = asyncio.run(
        service.export_accounts(
            AccountExportInput(
                format="access-tokens",
                scope="selected",
                ids=["valid", "missing", "expired"],
            )
        )
    )

    assert result.content == "VALID_TEST_AT"
    assert result.count == 1
    assert result.skippedMissingCount == 1
    assert result.skippedExpiredCount == 1
    assert "EXPIRED_TEST_AT" not in result.content
