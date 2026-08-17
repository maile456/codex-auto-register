from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .errors import MongoUnavailableError
from .mongo_manager import MongoManager


PROBE_LOCK_ID = "browser_probe_controller"
WORKSPACE_LOCK_PREFIX = "browser_probe_workspace:"
DEFAULT_PROXY_GROUP = "默认组"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ProxyLease:
    id: str
    host: str
    port: int
    username: str
    password: str
    country: str = "ZZ"
    group: str = DEFAULT_PROXY_GROUP
    scheme: str = "http"


class MongoProbeStore:
    def __init__(self, manager: MongoManager) -> None:
        self.manager = manager

    @property
    def locks(self) -> Any:
        return self.manager.database["executor_locks"]

    @property
    def proxies(self) -> Any:
        return self.manager.database["proxies"]

    async def _guard(self, awaitable: Any) -> Any:
        self.manager.require_online()
        try:
            return await awaitable
        except DuplicateKeyError:
            raise
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 当前不可用") from exc

    async def ensure_indexes(self) -> None:
        await self._guard(
            self.locks.create_index(
                [("leaseUntil", ASCENDING)],
                name="executor_locks_expiry",
            )
        )
        index_name = "proxies_probe_rotation"
        index_keys = [
            ("enabled", ASCENDING),
            ("country", ASCENDING),
            ("group", ASCENDING),
            ("status", ASCENDING),
            ("leaseUntil", ASCENDING),
            ("lastSelectedAt", ASCENDING),
            ("createdAt", ASCENDING),
        ]
        indexes = await self._guard(self.proxies.index_information())
        existing = indexes.get(index_name)
        raw_existing_keys = (existing or {}).get("key", [])
        existing_keys = (
            list(raw_existing_keys.items())
            if hasattr(raw_existing_keys, "items")
            else list(raw_existing_keys)
        )
        if existing is not None and existing_keys != index_keys:
            # Older installations used the same name without the country key.
            # Replace only that index definition; resource documents remain intact.
            await self._guard(self.proxies.drop_index(index_name))
            existing = None
        if existing is None:
            await self._guard(
                self.proxies.create_index(index_keys, name=index_name)
            )

    async def clear_expired_probe_leases(self) -> int:
        now = utc_now()
        result = await self._guard(
            self.proxies.update_many(
                {
                    "leaseOwner": {"$regex": "^probe:"},
                    "leaseUntil": {"$lte": now},
                },
                {"$unset": {"leaseOwner": "", "leaseUntil": ""}},
            )
        )
        return int(result.modified_count)

    async def clear_expired_worker_leases(self) -> int:
        result = await self._guard(
            self.proxies.update_many(
                {
                    "leaseOwner": {"$regex": "^run:"},
                    "leaseUntil": {"$lte": utc_now()},
                },
                {"$unset": {"leaseOwner": "", "leaseUntil": ""}},
            )
        )
        return int(result.modified_count)

    async def clear_expired_locks(self) -> int:
        result = await self._guard(
            self.locks.delete_many({"leaseUntil": {"$lte": utc_now()}})
        )
        return int(result.deleted_count)

    async def acquire_probe_lock(
        self,
        owner: str,
        *,
        lease_seconds: int = 90,
    ) -> bool:
        now = utc_now()
        try:
            document = await self._guard(
                self.locks.find_one_and_update(
                    {
                        "_id": PROBE_LOCK_ID,
                        "$or": [
                            {"leaseUntil": {"$lte": now}},
                            {"leaseUntil": {"$exists": False}},
                            {"owner": owner},
                        ],
                    },
                    {
                        "$set": {
                            "owner": owner,
                            "acquiredAt": now,
                            "heartbeatAt": now,
                            "leaseUntil": now + timedelta(seconds=lease_seconds),
                        }
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            )
        except DuplicateKeyError:
            return False
        return document is not None and document.get("owner") == owner

    async def heartbeat_probe_lock(
        self,
        owner: str,
        *,
        lease_seconds: int = 90,
    ) -> bool:
        now = utc_now()
        result = await self._guard(
            self.locks.update_one(
                {"_id": PROBE_LOCK_ID, "owner": owner},
                {
                    "$set": {
                        "heartbeatAt": now,
                        "leaseUntil": now + timedelta(seconds=lease_seconds),
                    }
                },
            )
        )
        return bool(result.modified_count or result.matched_count)

    async def release_probe_lock(self, owner: str) -> None:
        await self._guard(
            self.locks.delete_one({"_id": PROBE_LOCK_ID, "owner": owner})
        )

    async def acquire_workspace(
        self,
        workspace_id: int,
        owner: str,
        *,
        lease_seconds: int = 180,
    ) -> bool:
        now = utc_now()
        lock_id = f"{WORKSPACE_LOCK_PREFIX}{workspace_id}"
        try:
            document = await self._guard(
                self.locks.find_one_and_update(
                    {
                        "_id": lock_id,
                        "$or": [
                            {"leaseUntil": {"$lte": now}},
                            {"leaseUntil": {"$exists": False}},
                            {"owner": owner},
                        ],
                    },
                    {
                        "$set": {
                            "kind": "browser_probe_workspace",
                            "workspaceId": workspace_id,
                            "owner": owner,
                            "acquiredAt": now,
                            "heartbeatAt": now,
                            "leaseUntil": now + timedelta(seconds=lease_seconds),
                        }
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            )
        except DuplicateKeyError:
            return False
        return document is not None and document.get("owner") == owner

    async def heartbeat_workspace(
        self,
        workspace_id: int,
        owner: str,
        *,
        lease_seconds: int = 180,
    ) -> bool:
        now = utc_now()
        result = await self._guard(
            self.locks.update_one(
                {
                    "_id": f"{WORKSPACE_LOCK_PREFIX}{workspace_id}",
                    "owner": owner,
                },
                {
                    "$set": {
                        "heartbeatAt": now,
                        "leaseUntil": now + timedelta(seconds=lease_seconds),
                    }
                },
            )
        )
        return bool(result.modified_count or result.matched_count)

    async def release_workspace(self, workspace_id: int, owner: str) -> None:
        await self._guard(
            self.locks.delete_one(
                {
                    "_id": f"{WORKSPACE_LOCK_PREFIX}{workspace_id}",
                    "owner": owner,
                }
            )
        )

    async def release_workspace_owner(self, owner: str) -> int:
        result = await self._guard(
            self.locks.delete_many(
                {
                    "kind": "browser_probe_workspace",
                    "owner": owner,
                }
            )
        )
        return int(result.deleted_count)

    @staticmethod
    def _available_proxy_filter(
        now: datetime,
        excluded_ids: set[str] | None = None,
        country: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "enabled": True,
            "status": {"$ne": "quarantined"},
            "$or": [
                {"leaseUntil": {"$lte": now}},
                {"leaseUntil": {"$exists": False}},
            ],
        }
        if excluded_ids:
            query["_id"] = {"$nin": sorted(excluded_ids)}
        filters: list[dict[str, Any]] = [query]
        normalized = str(country or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", normalized):
            escaped = re.escape(normalized)
            inferred_pattern = {
                "$regex": rf"(?:^|[-_.])(?:region|country|res|area|dc|res_sc)-{escaped}(?:[-_.:]|$)",
                "$options": "i",
            }
            filters.append(
                {
                        "$or": [
                            {"country": normalized},
                            {
                                "$and": [
                                    {
                                        "$or": [
                                            {"country": {"$exists": False}},
                                            {"country": None},
                                            {"country": "ZZ"},
                                        ]
                                    },
                                    {
                                        "$or": [
                                            {"username": inferred_pattern},
                                            {"host": inferred_pattern},
                                        ]
                                    },
                                ]
                            },
                        ]
                    }
            )
        normalized_group = " ".join(str(group or "").split())
        if normalized_group:
            if normalized_group == DEFAULT_PROXY_GROUP:
                filters.append(
                    {
                        "$or": [
                            {"group": DEFAULT_PROXY_GROUP},
                            {"group": {"$exists": False}},
                            {"group": None},
                            {"group": ""},
                        ]
                    }
                )
            else:
                filters.append({"group": normalized_group})
        return query if len(filters) == 1 else {"$and": filters}

    async def count_eligible_proxies(
        self, country: str | None = None, group: str | None = None
    ) -> int:
        return int(
            await self._guard(
                self.proxies.count_documents(
                    self._available_proxy_filter(utc_now(), country=country, group=group)
                )
            )
        )

    async def acquire_proxy(
        self,
        owner: str,
        *,
        excluded_ids: set[str] | None = None,
        lease_seconds: int = 180,
        country: str | None = None,
        group: str | None = None,
    ) -> ProxyLease | None:
        now = utc_now()
        document = await self._guard(
            self.proxies.find_one_and_update(
                self._available_proxy_filter(now, excluded_ids, country, group),
                {
                    "$set": {
                        "lastSelectedAt": now,
                        "leaseOwner": owner,
                        "leaseUntil": now + timedelta(seconds=lease_seconds),
                    }
                },
                sort=[
                    ("lastSelectedAt", ASCENDING),
                    ("createdAt", ASCENDING),
                    ("_id", ASCENDING),
                ],
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is None:
            return None
        return ProxyLease(
            id=str(document["_id"]),
            host=str(document["host"]),
            port=int(document["port"]),
            username=str(document.get("username") or ""),
            password=str(document.get("password") or ""),
            country=(
                str(country).upper()
                if str(document.get("country") or "").upper() in {"", "ZZ"}
                and country
                else str(document.get("country") or "ZZ").upper()
            ),
            group=str(document.get("group") or DEFAULT_PROXY_GROUP),
            scheme=str(document.get("scheme") or "http").lower(),
        )

    async def release_proxy(self, proxy_id: str, owner: str) -> None:
        await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id, "leaseOwner": owner},
                {"$unset": {"leaseOwner": "", "leaseUntil": ""}},
            )
        )

    async def heartbeat_proxy(
        self,
        proxy_id: str,
        owner: str,
        *,
        lease_seconds: int = 180,
    ) -> bool:
        now = utc_now()
        result = await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id, "leaseOwner": owner},
                {
                    "$set": {
                        "leaseUntil": now + timedelta(seconds=lease_seconds),
                    }
                },
            )
        )
        return bool(result.modified_count or result.matched_count)

    async def release_proxy_owner(self, owner: str) -> int:
        result = await self._guard(
            self.proxies.update_many(
                {"leaseOwner": owner},
                {"$unset": {"leaseOwner": "", "leaseUntil": ""}},
            )
        )
        return int(result.modified_count)

    async def record_proxy_success(self, proxy_id: str, latency_ms: int) -> None:
        await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id},
                {
                    "$set": {
                        "status": "available",
                        "latencyMs": max(0, latency_ms),
                        "lastCheckedAt": utc_now(),
                    }
                },
            )
        )
