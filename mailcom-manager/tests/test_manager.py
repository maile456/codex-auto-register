from __future__ import annotations

import asyncio
from pathlib import Path
import time

from fastapi.testclient import TestClient

from manager.alias_creator import AliasCreationResult
from manager.app import create_app
from manager.imap_client import MailSummary
from manager.server_sync import ServerSyncError, ServerSyncResult


class FakeCipher:
    prefix = b"test:"

    def encrypt(self, value: str) -> bytes:
        return self.prefix + value.encode("utf-8")

    def decrypt(self, value: bytes) -> str:
        assert value.startswith(self.prefix)
        return value[len(self.prefix) :].decode("utf-8")


class FakeMailbox:
    def __init__(self) -> None:
        self.tests: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str, str, int]] = []

    def test(self, email: str, password: str):
        self.tests.append((email, password))
        return {"ok": True, "messageCount": 7}

    def messages(self, email: str, password: str, *, folder: str, limit: int):
        self.reads.append((email, password, folder, limit))
        return [
            MailSummary(
                uid="42",
                folder=folder,
                subject="Your temporary ChatGPT verification code",
                sender="noreply@example.test",
                recipients=email,
                received_at="2026-08-17T03:00:00+00:00",
                verification_code="123456",
                preview="Enter this temporary verification code: 123456",
            )
        ]


def make_client(tmp_path: Path) -> tuple[TestClient, FakeMailbox]:
    mailbox = FakeMailbox()
    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=mailbox,  # type: ignore[arg-type]
    )
    return TestClient(app), mailbox


def test_import_list_and_api_never_return_password(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    result = client.post(
        "/api/accounts/import",
        json={
            "rawText": (
                "Person@Gardener.com----mail-password\n"
                "person@gardener.com----duplicate-password\n"
                "broken-line"
            )
        },
    )
    assert result.status_code == 200
    assert result.json() == {
        "total": 3,
        "imported": 1,
        "duplicateCount": 1,
        "errorCount": 1,
        "errors": [{"line": 3, "message": "缺少 ---- 分隔符"}],
    }

    response = client.get("/api/accounts?pageSize=100")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    serialized = response.text
    assert "mail-password" not in serialized
    assert "duplicate-password" not in serialized
    assert "password" not in payload["items"][0]


def test_server_sync_pushes_complete_snapshot_without_returning_password(
    tmp_path: Path,
) -> None:
    class CapturingSync:
        def __init__(self) -> None:
            self.snapshot = None
            self.connection = None

        def push(self, snapshot, **connection):
            self.snapshot = snapshot
            self.connection = connection
            return ServerSyncResult(1, 1, "a" * 64)

    sync = CapturingSync()
    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=FakeMailbox(),  # type: ignore[arg-type]
        server_sync_service=sync,
    )
    client = TestClient(app)
    client.post(
        "/api/accounts/import",
        json={"rawText": "owner@gardener.com----mail-password"},
    )
    account = client.get("/api/accounts?pageSize=100").json()["items"][0]
    client.post(
        f"/api/accounts/{account['id']}/aliases/import",
        json={"rawText": "alias@gardener.com----worker"},
    )

    response = client.post(
        "/api/server-sync",
        json={
            "host": "server.example",
            "port": 22,
            "username": "root",
            "password": "ssh-password-must-not-leak",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "accounts": 1,
        "aliases": 1,
        "hostKeySha256": "a" * 64,
    }
    assert "ssh-password-must-not-leak" not in response.text
    assert sync.connection == {
        "host": "server.example",
        "port": 22,
        "username": "root",
        "password": "ssh-password-must-not-leak",
    }
    assert sync.snapshot == {
        "version": 1,
        "accounts": [
            {"email": "owner@gardener.com", "password": "mail-password"}
        ],
        "aliases": [
            {
                "email": "alias@gardener.com",
                "accountEmail": "owner@gardener.com",
                "label": "worker",
            }
        ],
    }


def test_server_sync_error_is_redacted(tmp_path: Path) -> None:
    class FailingSync:
        def push(self, _snapshot, **_connection):
            raise ServerSyncError("server_sync_connection_failed", "SSH 认证失败")

    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=FakeMailbox(),  # type: ignore[arg-type]
        server_sync_service=FailingSync(),
    )
    response = TestClient(app).post(
        "/api/server-sync",
        json={
            "host": "server.example",
            "port": 22,
            "username": "root",
            "password": "never-return-this-password",
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "server_sync_connection_failed",
        "message": "SSH 认证失败",
    }
    assert "never-return-this-password" not in response.text


def test_connection_and_read_only_message_routes(tmp_path: Path) -> None:
    client, mailbox = make_client(tmp_path)
    client.post(
        "/api/accounts/import",
        json={"rawText": "person@gardener.com----mail-password"},
    )
    account = client.get("/api/accounts?pageSize=100").json()["items"][0]

    tested = client.post(f"/api/accounts/{account['id']}/test")
    assert tested.status_code == 200
    assert tested.json()["messageCount"] == 7
    assert mailbox.tests == [("person@gardener.com", "mail-password")]

    messages = client.get(
        f"/api/accounts/{account['id']}/messages?folder=INBOX&limit=10"
    )
    assert messages.status_code == 200
    assert messages.json()["items"][0]["verificationCode"] == "123456"
    assert mailbox.reads == [
        ("person@gardener.com", "mail-password", "INBOX", 10)
    ]
    assert "mail-password" not in messages.text


def test_delete_removes_encrypted_record(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    client.post(
        "/api/accounts/import",
        json={"rawText": "person@gardener.com----mail-password"},
    )
    account_id = client.get("/api/accounts?pageSize=100").json()["items"][0]["id"]
    assert client.delete(f"/api/accounts/{account_id}").json() == {"deleted": True}
    assert client.get("/api/accounts?pageSize=100").json()["total"] == 0


def test_registration_adapter_accepts_email_and_exports_compatible_lines(
    tmp_path: Path,
) -> None:
    client, mailbox = make_client(tmp_path)
    client.post(
        "/api/accounts/import",
        json={"rawText": "person@gardener.com----mail-password"},
    )

    latest = client.get(
        "/api/mail/latest",
        params={"email": "person@gardener.com"},
    )
    assert latest.status_code == 200
    assert latest.json()["verification_code"] == "123456"
    assert latest.json()["receivedAt"] == "2026-08-17T03:00:00+00:00"
    assert len(mailbox.reads) == 3

    exported = client.get("/api/export/registration-lines")
    assert exported.status_code == 200
    assert exported.text.strip() == (
        "person@gardener.com----http://testserver/api/mail/latest?"
        "email=person%40gardener.com"
    )
    assert "mail-password" not in exported.text

    structured = client.get("/api/export/registration-items")
    assert structured.status_code == 200
    assert structured.json() == {
        "items": [
            {
                "email": "person@gardener.com",
                "accountEmail": "person@gardener.com",
                "isAlias": False,
                "accessUrl": (
                    "http://testserver/api/mail/latest?"
                    "email=person%40gardener.com"
                ),
            }
        ]
    }


def test_payment_confirmation_requires_recipient_time_success_and_order(tmp_path: Path) -> None:
    class PaymentMailbox(FakeMailbox):
        def messages(self, email: str, password: str, *, folder: str, limit: int):
            self.reads.append((email, password, folder, limit))
            if folder != "Spam":
                return []
            return [
                MailSummary(
                    uid="paid-1",
                    folder=folder,
                    subject="ChatGPT - New plan",
                    sender="OpenAI <noreply@example.test>",
                    recipients="person@gardener.com",
                    received_at="2026-08-17T03:10:00+00:00",
                    verification_code=None,
                    preview=(
                        "You successfully subscribed to ChatGPT Plus. "
                        "Order number: sub_fixture123"
                    ),
                )
            ]

    mailbox = PaymentMailbox()
    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=mailbox,  # type: ignore[arg-type]
    )
    client = TestClient(app)
    client.post(
        "/api/accounts/import",
        json={"rawText": "person@gardener.com----mail-password"},
    )

    confirmed = client.get(
        "/api/mail/payment-confirmation",
        params={"email": "person@gardener.com", "since": "2026-08-17T03:05:00+00:00"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["orderId"] == "sub_fixture123"
    assert confirmed.json()["folder"] == "Spam"

    too_late = client.get(
        "/api/mail/payment-confirmation",
        params={"email": "person@gardener.com", "since": "2026-08-17T03:20:00+00:00"},
    )
    assert too_late.status_code == 200
    assert too_late.json()["status"] == "waiting"


def test_aliases_have_independent_urls_and_do_not_mix_codes(tmp_path: Path) -> None:
    class AliasMailbox(FakeMailbox):
        def messages(self, email: str, password: str, *, folder: str, limit: int):
            self.reads.append((email, password, folder, limit))
            if folder != "INBOX":
                return []
            return [
                MailSummary(
                    uid="101",
                    folder=folder,
                    subject="Your temporary ChatGPT verification code",
                    sender="noreply@example.test",
                    recipients="Alias One <alias.one@example.com>",
                    received_at="2026-08-17T03:01:00+00:00",
                    verification_code="111111",
                    preview="Enter this temporary verification code: 111111",
                ),
                MailSummary(
                    uid="102",
                    folder=folder,
                    subject="Your temporary ChatGPT verification code",
                    sender="noreply@example.test",
                    recipients="alias.two@example.com | other@example.com",
                    received_at="2026-08-17T03:02:00+00:00",
                    verification_code="222222",
                    preview="Enter this temporary verification code: 222222",
                ),
            ]

    mailbox = AliasMailbox()
    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=mailbox,  # type: ignore[arg-type]
    )
    client = TestClient(app)
    client.post(
        "/api/accounts/import",
        json={"rawText": "owner@gardener.com----mail-password"},
    )
    account = client.get("/api/accounts?pageSize=100").json()["items"][0]

    imported = client.post(
        f"/api/accounts/{account['id']}/aliases/import",
        json={
            "rawText": (
                "alias.one@example.com----第一组\n"
                "alias.two@example.com----第二组\n"
                "alias.one@example.com----重复"
            )
        },
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 2
    assert imported.json()["duplicateCount"] == 1

    aliases = client.get(f"/api/accounts/{account['id']}/aliases")
    assert aliases.status_code == 200
    assert {item["email"] for item in aliases.json()["items"]} == {
        "alias.one@example.com",
        "alias.two@example.com",
    }
    refreshed_account = client.get("/api/accounts?pageSize=100").json()["items"][0]
    assert refreshed_account["aliasCount"] == 2

    first = client.get(
        "/api/mail/latest", params={"email": "alias.one@example.com"}
    )
    second = client.get(
        "/api/mail/latest", params={"email": "alias.two@example.com"}
    )
    assert first.status_code == 200
    assert first.json()["email"] == "alias.one@example.com"
    assert first.json()["isAlias"] is True
    assert first.json()["verification_code"] == "111111"
    assert second.json()["verification_code"] == "222222"
    assert all(read[0] == "owner@gardener.com" for read in mailbox.reads)
    assert all(read[1] == "mail-password" for read in mailbox.reads)

    exported = client.get("/api/export/registration-lines")
    assert exported.status_code == 200
    lines = set(exported.text.strip().splitlines())
    assert len(lines) == 3
    assert (
        "alias.one@example.com----http://testserver/api/mail/latest?"
        "email=alias.one%40example.com"
    ) in lines
    assert "mail-password" not in exported.text

    structured = client.get("/api/export/registration-items").json()["items"]
    alias_items = [item for item in structured if item["isAlias"]]
    assert {item["email"] for item in alias_items} == {
        "alias.one@example.com",
        "alias.two@example.com",
    }
    assert {item["accountEmail"] for item in alias_items} == {
        "owner@gardener.com"
    }

    alias_id = aliases.json()["items"][0]["id"]
    assert client.delete(f"/api/aliases/{alias_id}").json() == {"deleted": True}
    assert client.get(f"/api/accounts/{account['id']}/aliases").json()["items"]


def test_alias_cannot_shadow_a_login_account(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    client.post(
        "/api/accounts/import",
        json={"rawText": "owner@gardener.com----mail-password"},
    )
    account = client.get("/api/accounts?pageSize=100").json()["items"][0]
    result = client.post(
        f"/api/accounts/{account['id']}/aliases/import",
        json={"rawText": "owner@gardener.com"},
    )
    assert result.status_code == 200
    assert result.json()["imported"] == 0
    assert result.json()["duplicateCount"] == 1


def test_auto_create_aliases_persists_remote_and_new_addresses(tmp_path: Path) -> None:
    class FakeAliasCreator:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        async def create_to_total(
            self,
            email: str,
            password: str,
            target_total: int,
            on_created,
        ) -> AliasCreationResult:
            self.calls.append((email, password, target_total))
            await on_created("created.alias@example.com")
            return AliasCreationResult(
                remote_before=(email, "existing.alias@example.com"),
                created=("created.alias@example.com",),
                remote_after=(
                    email,
                    "existing.alias@example.com",
                    "created.alias@example.com",
                ),
            )

    creator = FakeAliasCreator()
    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=FakeMailbox(),  # type: ignore[arg-type]
        alias_creator=creator,
    )
    client = TestClient(app)
    client.post(
        "/api/accounts/import",
        json={"rawText": "owner@gardener.com----mail-password"},
    )
    account = client.get("/api/accounts?pageSize=100").json()["items"][0]

    response = client.post(
        f"/api/accounts/{account['id']}/aliases/auto-create",
        json={"targetTotal": 10},
    )

    assert response.status_code == 200
    assert response.json() == {
        "accountId": account["id"],
        "remoteBefore": 2,
        "created": 1,
        "importedExisting": 1,
        "aliasCount": 2,
        "targetTotal": 10,
    }
    assert creator.calls == [
        ("owner@gardener.com", "mail-password", 10)
    ]
    aliases = client.get(f"/api/accounts/{account['id']}/aliases").json()["items"]
    assert {item["email"] for item in aliases} == {
        "existing.alias@example.com",
        "created.alias@example.com",
    }


def test_bulk_auto_create_processes_only_underfilled_accounts(tmp_path: Path) -> None:
    class BulkAliasCreator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create_to_total(
            self,
            email: str,
            password: str,
            target_total: int,
            on_created,
        ) -> AliasCreationResult:
            self.calls.append(email)
            local, domain = email.split("@", 1)
            created = tuple(
                f"{local}.bulk{index}@{domain}" for index in range(1, target_total)
            )
            for alias in created:
                await on_created(alias)
            return AliasCreationResult(
                remote_before=(email,),
                created=created,
                remote_after=(email, *created),
            )

    creator = BulkAliasCreator()
    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=FakeMailbox(),  # type: ignore[arg-type]
        alias_creator=creator,
    )
    with TestClient(app) as client:
        client.post(
            "/api/accounts/import",
            json={
                "rawText": (
                    "first@gardener.com----first-password\n"
                    "second@engineer.com----second-password"
                )
            },
        )
        started = client.post(
            "/api/aliases/auto-create-all",
            json={"targetTotal": 10},
        )
        assert started.status_code == 202
        assert started.json()["total"] == 2

        job = started.json()
        for _ in range(100):
            job = client.get(
                f"/api/aliases/auto-create-all/{job['id']}"
            ).json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.03)

        assert job["status"] == "completed"
        assert job["completed"] == 2
        assert job["succeeded"] == 2
        assert job["failed"] == 0
        assert job["created"] == 18
        assert set(creator.calls) == {
            "first@gardener.com",
            "second@engineer.com",
        }
        accounts = client.get("/api/accounts?pageSize=100").json()["items"]
        assert {item["aliasCount"] for item in accounts} == {9}

        second_start = client.post(
            "/api/aliases/auto-create-all",
            json={"targetTotal": 10},
        )
        assert second_start.status_code == 202
        assert second_start.json()["total"] == 0


def test_bulk_auto_create_honors_concurrency_limit(tmp_path: Path) -> None:
    class ConcurrentAliasCreator:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def create_to_total(
            self,
            email: str,
            password: str,
            target_total: int,
            on_created,
        ) -> AliasCreationResult:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.04)
                return AliasCreationResult(
                    remote_before=(email,),
                    created=(),
                    remote_after=(email,),
                )
            finally:
                self.active -= 1

    creator = ConcurrentAliasCreator()
    app = create_app(
        db_path=tmp_path / "manager.db",
        cipher=FakeCipher(),
        imap_service=FakeMailbox(),  # type: ignore[arg-type]
        alias_creator=creator,
    )
    with TestClient(app) as client:
        client.post(
            "/api/accounts/import",
            json={
                "rawText": (
                    "one@gardener.com----one-password\n"
                    "two@engineer.com----two-password\n"
                    "three@worker.com----three-password\n"
                    "four@collector.com----four-password"
                )
            },
        )
        started = client.post(
            "/api/aliases/auto-create-all",
            json={"targetTotal": 10, "concurrency": 2},
        )
        assert started.status_code == 202
        assert started.json()["concurrency"] == 2
        job = started.json()
        for _ in range(100):
            job = client.get(f"/api/aliases/auto-create-all/{job['id']}").json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)

        assert job["status"] == "completed"
        assert job["completed"] == 4
        assert creator.max_active == 2
