from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .errors import (
    MongoUnavailableError,
    RunConflictError,
    RunNotFoundError,
)
from .mongo_manager import MongoManager
from .resource_models import RunState, WorkerSnapshot


ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_for_database")
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled", "interrupted")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MongoRunStore:
    """Persistent run state and single-active-run lease."""

    def __init__(self, manager: MongoManager) -> None:
        self.manager = manager

    @property
    def collection(self) -> Any:
        return self.manager.database["runs"]

    async def _guard(self, awaitable: Any) -> Any:
        self.manager.require_online()
        try:
            return await awaitable
        except DuplicateKeyError:
            raise
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 当前不可用，请检查本机服务") from exc

    async def ensure_indexes(self) -> None:
        self.manager.require_online()
        try:
            await self.collection.create_index(
                [("activeKey", ASCENDING)],
                unique=True,
                sparse=True,
                name="runs_single_active",
            )
            await self.collection.create_index(
                [("createdAt", DESCENDING)], name="runs_created_desc"
            )
            await self.collection.create_index(
                [("status", ASCENDING), ("updatedAt", DESCENDING)],
                name="runs_status_updated",
            )
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 任务索引初始化失败") from exc

    async def create(self, state: RunState) -> RunState:
        document = self._document(state)
        document.update(
            {
                "_id": state.runId,
                "kind": state.kind,
                "activeKey": "singleton",
                "createdAt": state.startedAt,
                "reservedEmailIds": [],
            }
        )
        try:
            await self._guard(self.collection.insert_one(document))
        except DuplicateKeyError as exc:
            raise RunConflictError("已有任务正在运行") from exc
        return state.model_copy(deep=True)

    async def set_reserved(self, run_id: str, email_ids: list[str]) -> None:
        result = await self._guard(
            self.collection.update_one(
                {"_id": run_id, "status": {"$in": ACTIVE_RUN_STATUSES}},
                {
                    "$set": {
                        "reservedEmailIds": list(email_ids),
                        "updatedAt": utc_now(),
                    }
                },
            )
        )
        if result.matched_count != 1:
            raise RunNotFoundError(f"任务不存在或已结束：{run_id}")

    async def save(self, state: RunState) -> RunState:
        state.updatedAt = utc_now()
        update: dict[str, Any] = {"$set": self._document(state)}
        if state.status in TERMINAL_RUN_STATUSES:
            update["$unset"] = {"activeKey": ""}
        result = await self._guard(
            self.collection.update_one({"_id": state.runId}, update)
        )
        if result.matched_count != 1:
            raise RunNotFoundError(f"任务不存在：{state.runId}")
        return state.model_copy(deep=True)

    async def get(self, run_id: str) -> RunState:
        document = await self._guard(self.collection.find_one({"_id": run_id}))
        if document is None:
            raise RunNotFoundError(f"任务不存在：{run_id}")
        return self._state(document)

    async def active(self) -> RunState | None:
        document = await self._guard(
            self.collection.find_one(
                {"status": {"$in": ACTIVE_RUN_STATUSES}},
                sort=[("updatedAt", DESCENDING)],
            )
        )
        return self._state(document) if document else None

    async def active_states(self, exclude_ids: set[str] | None = None) -> list[RunState]:
        query: dict[str, Any] = {"status": {"$in": ACTIVE_RUN_STATUSES}}
        if exclude_ids:
            query["_id"] = {"$nin": sorted(exclude_ids)}
        cursor = self.collection.find(query).sort("createdAt", ASCENDING)
        documents = await self._guard(cursor.to_list(length=None))
        return [self._state(document) for document in documents]

    async def request_cancel(self, run_id: str) -> RunState:
        now = utc_now()
        document = await self._guard(
            self.collection.find_one_and_update(
                {"_id": run_id, "status": {"$in": ACTIVE_RUN_STATUSES}},
                {"$set": {"cancelRequested": True, "updatedAt": now}},
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is not None:
            return self._state(document)
        return await self.get(run_id)

    @staticmethod
    def _document(state: RunState) -> dict[str, Any]:
        return state.model_dump(mode="python")

    @staticmethod
    def _state(document: dict[str, Any]) -> RunState:
        payload = dict(document)
        payload["runId"] = str(payload.pop("_id", payload.get("runId", "")))
        allowed = set(RunState.model_fields)
        return RunState.model_validate(
            {key: value for key, value in payload.items() if key in allowed}
        )


class MongoRunWorkerStore:
    """Internal worker state with an explicitly safe public projection."""

    TERMINAL_STATUSES = ("success", "partial_success", "failed", "cancelled")

    def __init__(self, manager: MongoManager) -> None:
        self.manager = manager

    @property
    def collection(self) -> Any:
        return self.manager.database["run_workers"]

    async def _guard(self, awaitable: Any) -> Any:
        self.manager.require_online()
        try:
            return await awaitable
        except DuplicateKeyError:
            raise
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 当前不可用，请检查本机服务") from exc

    async def ensure_indexes(self) -> None:
        self.manager.require_online()
        try:
            await self.collection.create_index(
                [("runId", ASCENDING), ("workerId", ASCENDING)],
                unique=True,
                name="run_workers_run_worker_unique",
            )
            await self.collection.create_index(
                [
                    ("runId", ASCENDING),
                    ("status", ASCENDING),
                    ("updatedAt", DESCENDING),
                ],
                name="run_workers_run_status_updated",
            )
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB worker 索引初始化失败") from exc

    async def create_many(
        self,
        run_id: str,
        workers: list[dict[str, Any]],
    ) -> None:
        if not workers:
            return
        now = utc_now()
        documents = [
            {
                "_id": f"{run_id}:{item['workerId']}",
                "runId": run_id,
                "workerId": str(item["workerId"]),
                "sequence": int(item["sequence"]),
                "status": "queued",
                "stage": "queued",
                "stageStartedAt": now,
                "email": str(item["email"]),
                "emailId": str(item["emailId"]),
                "egressIp": None,
                "errorCode": None,
                "errorStage": None,
                "errorOperation": None,
                "errorKind": None,
                "errorHttpStatus": None,
                "errorApiCode": None,
                "errorRetryCount": None,
                "errorElapsedMs": None,
                "startedAt": None,
                "updatedAt": now,
                "finishedAt": None,
            }
            for item in workers
        ]
        await self._guard(self.collection.insert_many(documents, ordered=True))

    async def assign(
        self,
        run_id: str,
        worker_id: str,
        *,
        workspace_id: int,
        lease_owner: str,
        pid: int | None = None,
    ) -> None:
        now = utc_now()
        result = await self._guard(
            self.collection.update_one(
                {
                    "runId": run_id,
                    "workerId": worker_id,
                    "status": "queued",
                },
                {
                    "$set": {
                        "status": "running",
                        "stage": "roxy_starting",
                        "stageStartedAt": now,
                        "workspaceId": workspace_id,
                        "leaseOwner": lease_owner,
                        "pid": pid,
                        "startedAt": now,
                        "updatedAt": now,
                    }
                },
            )
        )
        if result.matched_count != 1:
            raise RunNotFoundError(f"worker 不存在或状态已变化：{worker_id}")

    async def set_pid(self, run_id: str, worker_id: str, pid: int) -> None:
        await self._guard(
            self.collection.update_one(
                {"runId": run_id, "workerId": worker_id},
                {"$set": {"pid": pid, "updatedAt": utc_now()}},
            )
        )

    async def stage(
        self,
        run_id: str,
        worker_id: str,
        stage: str,
        *,
        egress_ip: str | None = None,
        dir_id: str | None = None,
    ) -> None:
        now = utc_now()
        values: dict[str, Any] = {
            "stage": stage,
            "stageStartedAt": now,
            "updatedAt": now,
        }
        if egress_ip is not None:
            values["egressIp"] = egress_ip
        if dir_id is not None:
            values["dirId"] = dir_id
        await self._guard(
            self.collection.update_one(
                {
                    "runId": run_id,
                    "workerId": worker_id,
                    "status": "running",
                },
                {"$set": values},
            )
        )

    async def finish(
        self,
        run_id: str,
        worker_id: str,
        status: str,
        *,
        error_code: str | None = None,
        egress_ip: str | None = None,
        error_stage: str | None = None,
        error_operation: str | None = None,
        error_kind: str | None = None,
        error_http_status: int | None = None,
        error_api_code: int | None = None,
        error_retry_count: int | None = None,
        error_elapsed_ms: int | None = None,
    ) -> None:
        if status not in self.TERMINAL_STATUSES:
            raise ValueError(f"unsupported worker terminal status: {status}")
        now = utc_now()
        values: dict[str, Any] = {
            "status": status,
            "stage": status,
            "stageStartedAt": now,
            "errorCode": error_code,
            "updatedAt": now,
            "finishedAt": now,
            "errorStage": error_stage,
            "errorOperation": error_operation,
            "errorKind": error_kind,
            "errorHttpStatus": error_http_status,
            "errorApiCode": error_api_code,
            "errorRetryCount": error_retry_count,
            "errorElapsedMs": error_elapsed_ms,
        }
        if egress_ip is not None:
            values["egressIp"] = egress_ip
        await self._guard(
            self.collection.update_one(
                {"runId": run_id, "workerId": worker_id},
                {"$set": values},
            )
        )

    async def list(self, run_id: str) -> list[WorkerSnapshot]:
        cursor = self.collection.find({"runId": run_id}).sort("sequence", ASCENDING)
        documents = await self._guard(cursor.to_list(length=None))
        now = utc_now()
        return [self._snapshot(document, now) for document in documents]

    async def internal(self, run_id: str, worker_id: str) -> dict[str, Any] | None:
        return await self._guard(
            self.collection.find_one({"runId": run_id, "workerId": worker_id})
        )

    async def active_internal(self, run_id: str) -> list[dict[str, Any]]:
        cursor = self.collection.find(
            {"runId": run_id, "status": {"$nin": list(self.TERMINAL_STATUSES)}}
        )
        return await self._guard(cursor.to_list(length=None))

    async def interrupt_run(self, run_id: str) -> int:
        now = utc_now()
        result = await self._guard(
            self.collection.update_many(
                {
                    "runId": run_id,
                    "status": {"$nin": list(self.TERMINAL_STATUSES)},
                },
                {
                    "$set": {
                        "status": "failed",
                        "stage": "failed",
                        "stageStartedAt": now,
                        "errorCode": "worker_interrupted",
                        "updatedAt": now,
                        "finishedAt": now,
                    }
                },
            )
        )
        return int(result.modified_count)

    @staticmethod
    def _snapshot(document: dict[str, Any], now: datetime) -> WorkerSnapshot:
        stage_started = document.get("stageStartedAt") or document.get("updatedAt") or now
        stage_ended = document.get("finishedAt") or now
        elapsed = max(0, int((stage_ended - stage_started).total_seconds() * 1000))
        return WorkerSnapshot(
            workerId=str(document["workerId"]),
            sequence=int(document["sequence"]),
            status=str(document["status"]),
            stage=str(document["stage"]),
            stageElapsedMs=elapsed,
            email=str(document["email"]),
            egressIp=(str(document["egressIp"]) if document.get("egressIp") else None),
            errorCode=(str(document["errorCode"]) if document.get("errorCode") else None),
            errorStage=(
                str(document["errorStage"]) if document.get("errorStage") else None
            ),
            errorOperation=(
                str(document["errorOperation"])
                if document.get("errorOperation")
                else None
            ),
            errorKind=(
                str(document["errorKind"]) if document.get("errorKind") else None
            ),
            errorHttpStatus=(
                int(document["errorHttpStatus"])
                if document.get("errorHttpStatus") is not None
                else None
            ),
            errorApiCode=(
                int(document["errorApiCode"])
                if document.get("errorApiCode") is not None
                else None
            ),
            errorRetryCount=(
                int(document["errorRetryCount"])
                if document.get("errorRetryCount") is not None
                else None
            ),
            errorElapsedMs=(
                int(document["errorElapsedMs"])
                if document.get("errorElapsedMs") is not None
                else None
            ),
            startedAt=document.get("startedAt"),
            updatedAt=document["updatedAt"],
            finishedAt=document.get("finishedAt"),
        )
