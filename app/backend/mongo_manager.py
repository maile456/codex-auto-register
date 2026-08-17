from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from bson.codec_options import CodecOptions
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from .errors import MongoUnavailableError
from .resource_models import MongoHealth


DEFAULT_MONGO_URI = os.environ.get(
    "AUTOREGISTER_MONGO_URI", "mongodb://127.0.0.1:27017"
)
DEFAULT_MONGO_DATABASE = os.environ.get(
    "AUTOREGISTER_MONGO_DATABASE", "autoregister"
)


class MongoManager:
    RETRY_DELAYS = (2, 5, 10, 30)

    def __init__(
        self,
        uri: str = DEFAULT_MONGO_URI,
        database_name: str = DEFAULT_MONGO_DATABASE,
    ) -> None:
        self.uri = uri
        self.database_name = database_name
        self.client: AsyncMongoClient[dict[str, Any]] | None = None
        self.online = False
        self.reconnecting = False
        self.error: str | None = None
        self.next_retry_seconds: int | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._online_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._callbacks: list[Callable[[], Awaitable[None]]] = []
        self._probe_lock = asyncio.Lock()

    @property
    def database(self) -> Any:
        if self.client is None:
            raise MongoUnavailableError("MongoDB 客户端尚未初始化")
        return self.client.get_database(
            self.database_name,
            codec_options=CodecOptions(tz_aware=True),
        )

    def add_reconnect_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._callbacks.append(callback)

    async def start(self) -> None:
        if self.client is not None:
            return
        self._stop_event.clear()
        self.client = AsyncMongoClient(
            self.uri,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
            socketTimeoutMS=5000,
            maxPoolSize=20,
            minPoolSize=0,
            retryReads=True,
            retryWrites=True,
            appname="AutoRegister",
        )
        await self._probe()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="mongodb-health-monitor"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_task:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        self.online = False
        self.reconnecting = False
        self._online_event.clear()

    async def _probe(self) -> bool:
        async with self._probe_lock:
            if self.client is None:
                return False
            try:
                await self.client.admin.command({"ping": 1})
                recovered = not self.online
                self.online = True
                self.reconnecting = False
                self.error = None
                self.next_retry_seconds = None
                if recovered:
                    for callback in self._callbacks:
                        try:
                            await callback()
                        except Exception as exc:
                            self.mark_offline(exc)
                            return False
                self._online_event.set()
                return True
            except (PyMongoError, OSError) as exc:
                self.mark_offline(exc)
                return False

    def mark_offline(self, error: BaseException) -> None:
        self.online = False
        self.reconnecting = True
        self.error = str(error)
        self._online_event.clear()

    async def _monitor_loop(self) -> None:
        retry_index = 0
        while not self._stop_event.is_set():
            delay = 5 if self.online else self.RETRY_DELAYS[min(retry_index, 3)]
            self.next_retry_seconds = None if self.online else delay
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass

            was_online = self.online
            connected = await self._probe()
            if connected:
                retry_index = 0
            elif was_online:
                retry_index = 0
            else:
                retry_index = min(retry_index + 1, 3)

    async def wait_until_online(self, timeout: float) -> bool:
        if self.online:
            return True
        try:
            await asyncio.wait_for(self._online_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def require_online(self) -> None:
        if not self.online or self.client is None:
            raise MongoUnavailableError("MongoDB 当前不可用，请检查本机服务")

    def health(self) -> MongoHealth:
        if self.online:
            status = "online"
        elif self.reconnecting:
            status = "reconnecting"
        else:
            status = "offline"
        return MongoHealth(
            status=status,
            database=self.database_name,
            error=self.error,
            nextRetrySeconds=self.next_retry_seconds,
        )
