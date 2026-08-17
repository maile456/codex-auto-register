from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import AutoReconnect

from backend.errors import InsufficientEmailsError, MongoUnavailableError
from backend.chatgpt_plan import AccountPlanResult
from backend.main import create_app
from backend.mongo_manager import MongoManager
from backend.roxy_client import RoxyWorkspace
from backend.resource_models import AccountExportInput
from backend.resource_service import MongoResourceStore, ResourceService


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOREGISTER_RUN_MONGO_TESTS") != "1",
    reason="set AUTOREGISTER_RUN_MONGO_TESTS=1 to run local MongoDB integration tests",
)

MONGO_URI = os.environ.get("AUTOREGISTER_MONGO_URI", "mongodb://127.0.0.1:27017")
MONGOD_BINARY = Path(
    os.environ.get(
        "AUTOREGISTER_MONGOD_BINARY",
        r"C:\Program Files\MongoDB\Server\8.0\bin\mongod.exe",
    )
)


@pytest.fixture
def mongo_client(tmp_path: Path):
    database = f"autoregister_test_{uuid4().hex}"
    assert database.startswith("autoregister_test_")
    manager = MongoManager(uri=MONGO_URI, database_name=database)
    app = create_app(
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        mongo_manager=manager,
    )
    with TestClient(app) as client:
        assert client.get("/api/health").json()["mongodb"]["status"] == "online"
        yield client, tmp_path
    sync_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        if database.startswith("autoregister_test_"):
            sync_client.drop_database(database)
    finally:
        sync_client.close()


def import_emails(client: TestClient, count: int) -> None:
    raw = "\n".join(
        f"queue.{index}@example.com----https://example.com/s/token-{index}/queue.{index}@example.com"
        for index in range(1, count + 1)
    )
    response = client.post("/api/emails/import", json={"rawText": raw})
    assert response.status_code == 200
    assert response.json()["imported"] == count


def configure_execution(client: TestClient, *, concurrency: int = 2) -> None:
    response = client.put(
        "/api/settings/execution",
        json={
            "browserExecutablePath": "D:/RoxyBrowser/RoxyBrowser.exe",
            "roxyApiKey": "test-key",
            "roxyApiPort": 50000,
            "headless": True,
            "proxyRetryCount": 0,
            "concurrency": concurrency,
            "taskTimeoutSeconds": 30,
        },
    )
    assert response.status_code == 200


def test_browser_probe_workspace_preflight_does_not_reserve_when_missing(mongo_client) -> None:
    client, _ = mongo_client
    configure_execution(client, concurrency=2)
    import_emails(client, 2)
    manager = client.app.state.run_manager

    async def no_workspaces(_settings):
        return []

    manager._preflight_workspaces = no_workspaces
    response = client.post("/api/runs/browser-probe", json={"count": 2})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "roxy_workspace_missing"
    assert response.json()["detail"]["available"] == 0
    assert client.get("/api/emails?page=1&pageSize=10").json()["total"] == 2
    assert client.get("/api/runs/active").json() is None
    sync = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        database = sync[client.app.state.mongo_manager.database_name]
        assert database["run_workers"].count_documents({}) == 0
        assert database["executor_locks"].count_documents({}) == 0
        assert database["emails"].count_documents({"status": "reserved"}) == 0
    finally:
        sync.close()


def test_browser_probe_route_aggregates_worker_snapshots(mongo_client) -> None:
    client, _ = mongo_client
    configure_execution(client, concurrency=2)
    import_emails(client, 2)
    manager = client.app.state.run_manager

    async def two_workspaces(_settings):
        return [RoxyWorkspace(1, "workspace-1"), RoxyWorkspace(2, "workspace-2")]

    manager._preflight_workspaces = two_workspaces

    class FakeBrowserExecutor:
        async def execute(self, context):
            assert context.workspace_ids == (1,)
            workspace_id = context.workspace_ids[0]
            for item in context.reserved:
                worker_id = item["_workerId"]
                await context.worker_store.assign(
                    context.state.runId,
                    worker_id,
                    workspace_id=workspace_id,
                    lease_owner=f"run:{context.state.runId}:worker:{worker_id}",
                    pid=123,
                )
                await context.worker_store.stage(
                    context.state.runId,
                    worker_id,
                    "email",
                    egress_ip="203.0.113.20",
                )
                await context.worker_store.finish(
                    context.state.runId,
                    worker_id,
                    "success",
                    egress_ip="203.0.113.20",
                )
                await context.resources.release_email(
                    item["_id"], context.state.runId
                )
                await context.record_result(True)
            return False

    manager.browser_executor = FakeBrowserExecutor()
    started = client.post("/api/runs/browser-probe", json={"count": 2})
    assert started.status_code == 202
    run_id = started.json()["runId"]
    for _ in range(50):
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] == "completed":
            break
        time.sleep(0.02)
    assert state["kind"] == "browser_probe"
    assert state["workerCount"] == 2
    assert state["activeWorkers"] == 0
    assert state["succeeded"] == 2
    workers = client.get(f"/api/runs/{run_id}/workers")
    assert workers.status_code == 200
    snapshots = workers.json()
    assert {item["email"] for item in snapshots} == {
        "queue.1@example.com",
        "queue.2@example.com",
    }
    assert all(item["egressIp"] == "203.0.113.20" for item in snapshots)
    assert all("workspaceId" not in item and "pid" not in item for item in snapshots)
    sync = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        database = sync[client.app.state.mongo_manager.database_name]
        assert database["executor_locks"].count_documents({}) == 0
        assert {
            item["workspaceId"]
            for item in database["run_workers"].find(
                {"runId": run_id}, {"workspaceId": 1}
            )
        } == {1}
    finally:
        sync.close()


def test_resource_crud_pagination_search_export_and_indexes(mongo_client) -> None:
    client, _ = mongo_client
    import_emails(client, 12)

    duplicate = client.post(
        "/api/emails/import",
        json={
            "rawText": "QUEUE.1@example.com----https://example.com/different\ninvalid"
        },
    )
    assert duplicate.json() == {
        "total": 2,
        "imported": 0,
        "duplicateCount": 1,
        "errorCount": 1,
    }

    first_page = client.get("/api/emails?page=1&pageSize=10")
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 12
    assert len(first_page.json()["items"]) == 10
    assert client.get("/api/emails?page=1&pageSize=20&q=queue.12").json()["total"] == 1
    assert client.get("/api/emails?page=1&pageSize=11").status_code == 422

    selected_ids = [item["id"] for item in first_page.json()["items"][:2]]
    selected_export = client.post(
        "/api/emails/export", json={"scope": "selected", "ids": selected_ids}
    ).json()
    assert selected_export["count"] == 2
    assert len(selected_export["content"].splitlines()) == 2
    assert selected_export["filename"].startswith("emails-2-mail-links-")

    proxy_import = client.post(
        "/api/proxies/import",
        json={
            "rawText": "proxy.example.test:10000:test-user:TEST_PASSWORD:tail"
        },
    )
    assert proxy_import.json()["imported"] == 1
    proxy = client.get("/api/proxies?page=1&pageSize=10").json()["items"][0]
    assert proxy["password"] == "TEST_PASSWORD:tail"
    assert client.patch(f"/api/proxies/{proxy['id']}", json={"enabled": False}).json()[
        "enabled"
    ] is False

    email = first_page.json()["items"][2]
    account_payload = {
        "email": email["email"],
        "chatgptPassword": "development-password",
        "totpSecret": "DEVELOPMENTTOTPSECRET",
        "emailAccessUrl": email["accessUrl"],
        "accountType": "plus",
        "phoneBound": True,
        "promotionEligible": None,
        "sourceEmailId": email["id"],
    }
    account_response = client.post("/api/accounts", json=account_payload)
    assert account_response.status_code == 201
    assert client.post("/api/accounts", json=account_payload).status_code == 409

    account_export = client.post(
        "/api/accounts/export",
        json={"format": "credentials", "scope": "all", "ids": []},
    ).json()
    assert account_export["count"] == 1
    assert account_export["content"].count("----") == 2
    assert account_export["filename"].startswith("accounts-1-credentials-")

    stats = client.get("/api/stats/overview").json()
    assert stats["accounts"]["plus"] == {"total": 1, "bound": 1, "unbound": 0}
    assert stats["emails"]["available"] == 11
    assert stats["proxies"]["total"] == 1

    assert client.post("/api/emails/bulk-delete", json={"ids": selected_ids}).json()[
        "deleted"
    ] == 2
    assert client.delete(f"/api/proxies/{proxy['id']}").json()["deleted"] == 1


def test_registered_email_is_excluded_from_available_pool() -> None:
    database = f"autoregister_test_{uuid4().hex}"

    async def scenario() -> None:
        manager = MongoManager(uri=MONGO_URI, database_name=database)
        store = MongoResourceStore(manager)
        await manager.start()
        try:
            await store.ensure_indexes()
            now = datetime.now(timezone.utc)
            registered = "registered@example.com"
            available = "available@example.com"
            await store.accounts.insert_one(
                {
                    "_id": str(uuid4()),
                    "email": registered,
                    "emailNormalized": registered,
                    "createdAt": now,
                    "accountType": "free",
                }
            )

            assert await store.upsert_email(
                registered,
                "https://example.com/registered",
            ) is False

            await store.emails.insert_many(
                [
                    {
                        "_id": str(uuid4()),
                        "email": registered,
                        "emailNormalized": registered,
                        "accessUrl": "https://example.com/registered",
                        "importedAt": now,
                        "status": "available",
                    },
                    {
                        "_id": str(uuid4()),
                        "email": available,
                        "emailNormalized": available,
                        "accessUrl": "https://example.com/available",
                        "importedAt": now,
                        "status": "available",
                    },
                ]
            )

            assert (await store.overview_stats()).emails.available == 1
            page = await store.list_emails(1, 10, "")
            assert page.total == 1
            assert [item.email for item in page.items] == [available]
            reserved = await store.reserve_emails(1, "registered-email-test")
            assert [item["email"] for item in reserved] == [available]
        finally:
            await manager.stop()

    try:
        asyncio.run(scenario())
    finally:
        MongoClient(MONGO_URI).drop_database(database)


def test_probe_profile_success_persists_pending_account_and_consumes_email() -> None:
    database = f"autoregister_test_{uuid4().hex}"
    owner = f"probe:{uuid4()}"
    first_email_id = str(uuid4())
    second_email_id = str(uuid4())

    async def scenario() -> None:
        manager = MongoManager(uri=MONGO_URI, database_name=database)
        store = MongoResourceStore(manager)
        await manager.start()
        try:
            await store.ensure_indexes()
            now = datetime.now(timezone.utc)
            base_document = {
                "email": "profile.pending@example.com",
                "emailNormalized": "profile.pending@example.com",
                "accessUrl": "https://example.com/messages/private-token/profile.pending%40example.com",
                "importedAt": now,
                "status": "available",
            }
            await store.emails.insert_one({"_id": first_email_id, **base_document})
            reserved = await store.reserve_emails(1, owner)

            account = await store.complete_probe_profile_success(reserved[0], owner)

            assert account.email == "profile.pending@example.com"
            assert account.chatgptPassword == ""
            assert account.totpSecret == ""
            assert account.accountType == "free"
            assert account.phoneBound is None
            assert account.promotionEligible is None
            assert account.accessTokenConfigured is False
            assert account.accessTokenExpiresAt is None
            assert await store.emails.count_documents({}) == 0
            assert await store.accounts.count_documents({}) == 1

            stored = await store.accounts.find_one({"_id": account.id})
            assert stored is not None
            assert stored["sourceEmailId"] == first_email_id
            assert stored["chatgptPassword"] == ""
            assert stored["totpSecret"] == ""
            assert not {
                "name",
                "age",
                "fullName",
                "profileName",
                "profileAge",
            }.intersection(stored)
            assert (await store.overview_stats()).accounts.totpComplete == 0

            access_token = "TEST_AT_VALUE_NOT_REAL"
            expires_at = now + timedelta(hours=1)
            updated_at = await store.store_account_access_token(
                account.id,
                access_token,
                expires_at,
            )
            assert updated_at.tzinfo is not None
            stored_with_token = await store.accounts.find_one({"_id": account.id})
            assert stored_with_token is not None
            assert stored_with_token["accessToken"] == access_token
            assert stored_with_token["accessTokenConfigured"] is True
            listed = await store.list_accounts(1, 10, "")
            assert listed.items[0].accessTokenConfigured is True
            assert "accessToken" not in listed.items[0].model_dump()

            plan_result = AccountPlanResult(
                checked_at=now,
                account_id="chatgpt-account-id",
                current_plan_type="free",
                subscription_plan="chatgptfreeplan",
                has_active_subscription=False,
                expires_at=None,
                renews_at=None,
                plus_trial_eligible=True,
                plus_trial_campaign_id="campaign-id",
            )
            await store.store_account_plan_result(account.id, plan_result)
            listed_with_plan = await store.list_accounts(1, 10, "")
            account_with_plan = listed_with_plan.items[0]
            assert account_with_plan.promotionEligible is True
            assert account_with_plan.planCheckStatus == "success"
            assert account_with_plan.subscriptionPlan == "chatgptfreeplan"
            assert account_with_plan.hasActiveSubscription is False
            assert account_with_plan.promotionCampaignId == "campaign-id"
            assert "accessToken" not in account_with_plan.model_dump()
            untried_plus = await store.list_accounts(1, 10, "", "untried_plus")
            assert untried_plus.total == 1
            assert untried_plus.items[0].id == account.id

            exported = await ResourceService(store).export_accounts(
                AccountExportInput(
                    format="access-tokens",
                    scope="single",
                    ids=[account.id],
                )
            )
            assert exported.content == access_token
            assert exported.count == 1
            assert exported.skippedMissingCount == 0
            assert exported.skippedExpiredCount == 0
            assert exported.filename.startswith("accounts-1-access-tokens-")

            await store.emails.insert_one({"_id": second_email_id, **base_document})
            with pytest.raises(InsufficientEmailsError):
                await store.reserve_emails(1, owner)
            assert await store.accounts.count_documents({}) == 1
            assert await store.emails.count_documents({}) == 1
            index = await store.accounts.index_information()
            assert index["accounts_email_unique"]["unique"] is True
            assert index["accounts_source_email_unique"]["unique"] is True
        finally:
            await manager.stop()

    try:
        asyncio.run(scenario())
    finally:
        cleanup = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            if database.startswith("autoregister_test_"):
                cleanup.drop_database(database)
        finally:
            cleanup.close()


def test_failed_email_is_rotated_behind_never_attempted_email() -> None:
    database = f"autoregister_test_{uuid4().hex}"
    owner = f"probe:{uuid4()}"

    async def scenario() -> None:
        manager = MongoManager(uri=MONGO_URI, database_name=database)
        store = MongoResourceStore(manager)
        await manager.start()
        try:
            await store.ensure_indexes()
            now = datetime.now(timezone.utc)
            await store.emails.insert_many(
                [
                    {
                        "_id": "older",
                        "email": "older@example.com",
                        "emailNormalized": "older@example.com",
                        "accessUrl": "https://example.com/older",
                        "importedAt": now - timedelta(minutes=1),
                        "status": "available",
                    },
                    {
                        "_id": "newer",
                        "email": "newer@example.com",
                        "emailNormalized": "newer@example.com",
                        "accessUrl": "https://example.com/newer",
                        "importedAt": now,
                        "status": "available",
                    },
                ]
            )

            first = (await store.reserve_emails(1, owner))[0]
            assert first["_id"] == "newer"
            await store.release_email("newer", owner)

            second = (await store.reserve_emails(1, owner))[0]
            assert second["_id"] == "older"
            rotated = await store.emails.find_one({"_id": "newer"})
            assert rotated is not None
            assert rotated["lastAttemptAt"].tzinfo is not None
        finally:
            await manager.stop()

    try:
        asyncio.run(scenario())
    finally:
        cleanup = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            if database.startswith("autoregister_test_"):
                cleanup.drop_database(database)
        finally:
            cleanup.close()


def test_proxy_bulk_delete_and_clear_pool(mongo_client) -> None:
    client, _ = mongo_client
    raw = "\n".join(
        f"proxy-{index}.example.com:{10000 + index}:user-{index}:password-{index}"
        for index in range(1, 6)
    )
    imported = client.post("/api/proxies/import", json={"rawText": raw})
    assert imported.status_code == 200
    assert imported.json()["imported"] == 5

    page = client.get("/api/proxies?page=1&pageSize=10").json()
    selected_ids = [item["id"] for item in page["items"][:2]]
    assert client.post("/api/proxies/bulk-delete", json={"ids": []}).status_code == 422

    bulk_deleted = client.post(
        "/api/proxies/bulk-delete", json={"ids": selected_ids}
    )
    assert bulk_deleted.status_code == 200
    assert bulk_deleted.json() == {"deleted": 2}
    assert client.get("/api/proxies?page=1&pageSize=10").json()["total"] == 3
    assert client.get("/api/stats/overview").json()["proxies"]["total"] == 3

    cleared = client.delete("/api/proxies")
    assert cleared.status_code == 200
    assert cleared.json() == {"deleted": 3}
    assert client.delete("/api/proxies").json() == {"deleted": 0}
    assert client.get("/api/proxies?page=1&pageSize=10").json()["total"] == 0
    assert client.get("/api/stats/overview").json()["proxies"] == {
        "total": 0,
        "enabled": 0,
        "available": 0,
        "quarantined": 0,
    }


def test_background_mock_run_updates_resources_and_safe_jsonl(mongo_client) -> None:
    client, tmp_path = mongo_client
    import_emails(client, 4)

    started = client.post("/api/runs/mock", json={"count": 4})
    assert started.status_code == 202
    run_id = started.json()["runId"]
    assert client.post("/api/runs/mock", json={"count": 1}).status_code == 409

    deadline = time.monotonic() + 15
    state = started.json()
    while state["status"] in {"running", "waiting_for_database"}:
        assert time.monotonic() < deadline
        time.sleep(0.2)
        state = client.get(f"/api/runs/{run_id}").json()

    assert state["status"] == "completed"
    assert (state["requested"], state["pending"], state["processed"]) == (4, 0, 4)
    assert (state["succeeded"], state["failed"]) == (3, 1)
    assert client.get("/api/stats/overview").json()["emails"]["available"] == 1
    accounts = client.get("/api/accounts?page=1&pageSize=10").json()["items"]
    assert len(accounts) == 3
    assert [item["promotionEligible"] for item in accounts].count(True) == 2
    assert [item["promotionEligible"] for item in accounts].count(False) == 1

    history = client.get("/api/run-logs/runs").json()
    assert history[0]["runId"] == run_id
    log_path = tmp_path / "logs" / history[0]["filename"]
    raw = log_path.read_text(encoding="utf-8")
    for forbidden in ["chatgptPassword", "totpSecret", "accessUrl", "proxyPassword", "token-"]:
        assert forbidden not in raw


def test_mock_run_can_be_cancelled_and_releases_every_unfinished_email(
    mongo_client,
) -> None:
    client, _ = mongo_client
    import_emails(client, 8)

    started = client.post("/api/runs/mock", json={"count": 8})
    assert started.status_code == 202
    run_id = started.json()["runId"]
    cancel = client.post(f"/api/runs/{run_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelRequested"] is True

    deadline = time.monotonic() + 15
    state = cancel.json()
    while state["status"] in {"queued", "running", "waiting_for_database"}:
        assert time.monotonic() < deadline
        time.sleep(0.1)
        state = client.get(f"/api/runs/{run_id}").json()

    assert state["status"] == "cancelled"
    assert client.get("/api/runs/active").json() is None
    assert client.get("/api/accounts?page=1&pageSize=10").json()["total"] == 0
    assert client.get("/api/emails?page=1&pageSize=10").json()["total"] == 8
    history = client.get("/api/run-logs/runs").json()
    assert history[0]["lastEvent"] == "run_cancelled"


def test_startup_interrupts_stale_run_and_releases_reserved_email(
    tmp_path: Path,
) -> None:
    database = f"autoregister_test_{uuid4().hex}"
    run_id = str(uuid4())
    email_id = str(uuid4())
    now = datetime.now(timezone.utc)
    sync_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    db = sync_client[database]
    db["runs"].insert_one(
        {
            "_id": run_id,
            "kind": "mock",
            "activeKey": "singleton",
            "status": "running",
            "requested": 1,
            "pending": 1,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "startedAt": now,
            "updatedAt": now,
            "finishedAt": None,
            "logPersisted": True,
            "cancelRequested": False,
            "createdAt": now,
            "reservedEmailIds": [email_id],
        }
    )
    db["emails"].insert_one(
        {
            "_id": email_id,
            "email": "stale@example.com",
            "emailNormalized": "stale@example.com",
            "accessUrl": "https://example.com/inbox/stale",
            "importedAt": now,
            "status": "reserved",
            "reservedBy": run_id,
            "reservedAt": now,
        }
    )
    sync_client.close()

    manager = MongoManager(uri=MONGO_URI, database_name=database)
    app = create_app(
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        mongo_manager=manager,
    )
    try:
        with TestClient(app) as client:
            state = client.get(f"/api/runs/{run_id}").json()
            assert state["status"] == "interrupted"
            assert state["finishedAt"] is not None
            assert client.get("/api/runs/active").json() is None
            assert client.get("/api/emails?page=1&pageSize=10").json()["total"] == 1
            history = client.get("/api/run-logs/runs").json()
            assert history[0]["runId"] == run_id
            assert history[0]["lastEvent"] == "run_interrupted"
    finally:
        cleanup = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            if database.startswith("autoregister_test_"):
                cleanup.drop_database(database)
        finally:
            cleanup.close()


@pytest.mark.skipif(not MONGOD_BINARY.exists(), reason="local mongod binary is unavailable")
def test_manager_recovers_without_recreating_client(tmp_path: Path) -> None:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = int(candidate.getsockname()[1])

    db_path = tmp_path / "reconnect-db"
    db_path.mkdir()
    log_path = tmp_path / "reconnect-mongod.log"
    uri = f"mongodb://127.0.0.1:{port}"

    def launch() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [
                str(MONGOD_BINARY),
                "--dbpath",
                str(db_path),
                "--bind_ip",
                "127.0.0.1",
                "--port",
                str(port),
                "--logpath",
                str(log_path),
                "--logappend",
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        probe = MongoClient(uri, serverSelectionTimeoutMS=250)
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                try:
                    probe.admin.command("ping")
                    return process
                except Exception:
                    time.sleep(0.1)
        finally:
            probe.close()
        process.terminate()
        raise AssertionError("temporary mongod did not start")

    first_process = launch()

    async def scenario() -> None:
        manager = MongoManager(uri=uri, database_name=f"autoregister_test_{uuid4().hex}")
        store = MongoResourceStore(manager)
        manager.add_reconnect_callback(store.ensure_indexes)
        await manager.start()
        original_client = manager.client
        assert manager.online

        shutdown_client = MongoClient(uri, serverSelectionTimeoutMS=1000)
        try:
            with pytest.raises(AutoReconnect):
                shutdown_client.admin.command({"shutdown": 1, "force": True})
        finally:
            shutdown_client.close()
        first_process.wait(timeout=10)

        with pytest.raises(MongoUnavailableError):
            await store.list_emails(1, 10, "")
        assert manager.health().status == "reconnecting"

        second_process = launch()
        try:
            assert await manager.wait_until_online(12)
            assert manager.client is original_client
            assert manager.health().status == "online"
            assert (await store.list_emails(1, 10, "")).total == 0
            index_names = await store.emails.index_information()
            assert "emails_email_unique" in index_names
            assert "emails_status_imported" in index_names
        finally:
            await manager.stop()
            terminate_client = MongoClient(uri, serverSelectionTimeoutMS=1000)
            try:
                with pytest.raises(AutoReconnect):
                    terminate_client.admin.command({"shutdown": 1, "force": True})
            finally:
                terminate_client.close()
            second_process.wait(timeout=10)

    try:
        asyncio.run(scenario())
    finally:
        if first_process.poll() is None:
            first_process.terminate()
            first_process.wait(timeout=10)
