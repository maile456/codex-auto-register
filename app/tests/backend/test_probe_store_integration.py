from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pymongo import MongoClient

from backend.mongo_manager import MongoManager
from backend.probe_store import MongoProbeStore
from backend.run_store import MongoRunWorkerStore


MONGO_URI = os.environ.get("AUTOREGISTER_MONGO_URI", "mongodb://127.0.0.1:27017")
RUN_MONGO_TESTS = os.environ.get("AUTOREGISTER_RUN_MONGO_TESTS") == "1"


@pytest.mark.skipif(not RUN_MONGO_TESTS, reason="set AUTOREGISTER_RUN_MONGO_TESTS=1")
def test_probe_lock_and_lru_proxy_rotation_persist_in_mongodb() -> None:
    database = f"autoregister_test_{uuid4().hex}"

    async def scenario() -> None:
        manager = MongoManager(uri=MONGO_URI, database_name=database)
        await manager.start()
        assert manager.online
        store = MongoProbeStore(manager)
        await store.ensure_indexes()
        now = datetime.now(timezone.utc)
        await store.proxies.insert_many(
            [
                {
                    "_id": f"proxy-{index}",
                    "host": f"proxy-{index}.example.com",
                    "port": 10000,
                    "username": "user",
                    "password": "password",
                    "enabled": True,
                    "status": "unknown",
                    "createdAt": now,
                }
                for index in range(1, 4)
            ]
        )
        await store.proxies.insert_many(
            [
                {
                    "_id": "expired-probe-lease",
                    "host": "expired.example.com",
                    "port": 10000,
                    "enabled": False,
                    "status": "unknown",
                    "leaseOwner": "probe:expired",
                    "leaseUntil": now - timedelta(seconds=1),
                    "createdAt": now,
                },
                {
                    "_id": "active-probe-lease",
                    "host": "active.example.com",
                    "port": 10000,
                    "enabled": False,
                    "status": "unknown",
                    "leaseOwner": "probe:active",
                    "leaseUntil": now + timedelta(minutes=5),
                    "createdAt": now,
                },
                {
                    "_id": "expired-other-lease",
                    "host": "other.example.com",
                    "port": 10000,
                    "enabled": False,
                    "status": "unknown",
                    "leaseOwner": "run:expired",
                    "leaseUntil": now - timedelta(seconds=1),
                    "createdAt": now,
                },
            ]
        )

        assert await store.clear_expired_probe_leases() == 1
        expired_probe = await store.proxies.find_one(
            {"_id": "expired-probe-lease"}
        )
        active_probe = await store.proxies.find_one(
            {"_id": "active-probe-lease"}
        )
        expired_other = await store.proxies.find_one(
            {"_id": "expired-other-lease"}
        )
        assert "leaseOwner" not in expired_probe
        assert "leaseUntil" not in expired_probe
        assert active_probe["leaseOwner"] == "probe:active"
        assert expired_other["leaseOwner"] == "run:expired"

        assert await store.acquire_probe_lock("owner-1") is True
        assert await store.acquire_probe_lock("owner-2") is False

        selected: list[str] = []
        for _ in range(4):
            lease = await store.acquire_proxy("owner-1")
            assert lease is not None
            selected.append(lease.id)
            await store.release_proxy(lease.id, "owner-1")

        assert selected[:3] == ["proxy-1", "proxy-2", "proxy-3"]
        assert selected[3] == "proxy-1"
        assert await store.proxies.count_documents({"leaseOwner": {"$exists": True}}) == 2

        indexes = await store.proxies.index_information()
        assert "proxies_probe_rotation" in indexes
        assert await store.release_probe_lock("owner-1") is None
        assert await store.acquire_probe_lock("owner-2") is True
        await store.release_probe_lock("owner-2")

        assert await store.acquire_workspace(7, "run:test:worker:1") is True
        assert await store.acquire_workspace(7, "run:test:worker:2") is False
        assert await store.heartbeat_workspace(7, "run:test:worker:1") is True
        await store.release_workspace(7, "run:test:worker:1")
        assert await store.acquire_workspace(7, "run:test:worker:2") is True
        await store.release_workspace(7, "run:test:worker:2")
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


@pytest.mark.skipif(not RUN_MONGO_TESTS, reason="set AUTOREGISTER_RUN_MONGO_TESTS=1")
def test_run_worker_store_public_projection_and_unique_index() -> None:
    database = f"autoregister_test_{uuid4().hex}"

    async def scenario() -> None:
        manager = MongoManager(uri=MONGO_URI, database_name=database)
        await manager.start()
        store = MongoRunWorkerStore(manager)
        await store.ensure_indexes()
        await store.create_many(
            "run-1",
            [
                {
                    "workerId": "worker-1",
                    "sequence": 1,
                    "email": "full@example.com",
                    "emailId": "email-1",
                }
            ],
        )
        await store.assign(
            "run-1",
            "worker-1",
            workspace_id=9,
            lease_owner="run:run-1:worker:worker-1",
            pid=123,
        )
        await store.stage(
            "run-1",
            "worker-1",
            "email",
            egress_ip="203.0.113.10",
            dir_id="dir-secret",
        )
        snapshots = await store.list("run-1")
        assert snapshots[0].email == "full@example.com"
        assert snapshots[0].egressIp == "203.0.113.10"
        payload = snapshots[0].model_dump()
        assert "workspaceId" not in payload
        assert "pid" not in payload
        assert "dirId" not in payload
        await store.finish(
            "run-1",
            "worker-1",
            "success",
        )
        assert (await store.list("run-1"))[0].status == "success"
        indexes = await store.collection.index_information()
        assert "run_workers_run_worker_unique" in indexes
        await manager.stop()

    try:
        asyncio.run(scenario())
    finally:
        cleanup = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            cleanup.drop_database(database)
        finally:
            cleanup.close()
