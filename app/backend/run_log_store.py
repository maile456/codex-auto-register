from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


DEFAULT_LOG_DIR = Path(
    os.environ.get("AUTOREGISTER_LOG_DIR", r"D:\AutoRegister\data\logs")
)
LOG_SCHEMA_VERSION = 1
LOG_RETENTION_COUNT = 10
RUN_LOG_PATTERN = re.compile(
    r"^run-([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
TERMINAL_EVENTS = {
    "run_completed",
    "run_failed",
    "run_cancelled",
    "run_interrupted",
}

LogLevel = Literal["info", "success", "warning", "error"]
LogDetailValue = str | int | float | bool | None


class RunLogCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestedCount: int = Field(ge=1, strict=True)
    concurrency: int = Field(ge=1, le=12, strict=True)


class RunLogEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    level: LogLevel
    event: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1, max_length=1000)
    email: str | None = Field(default=None, max_length=320)
    sequence: int | None = Field(default=None, ge=1, strict=True)
    details: dict[str, LogDetailValue] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def reject_sensitive_detail_keys(
        cls, value: dict[str, LogDetailValue]
    ) -> dict[str, LogDetailValue]:
        blocked_fragments = (
            "password",
            "totp",
            "accessurl",
            "access_url",
            "cookie",
            "proxy",
            "token",
            "secret",
        )
        for key in value:
            normalized = key.lower().replace("-", "_")
            if any(fragment in normalized for fragment in blocked_fragments):
                raise ValueError(f"日志详情字段不允许包含敏感信息：{key}")
        return value


class RunLogAppendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[RunLogEntryInput] = Field(min_length=1, max_length=100)


class RunLogEntry(RunLogEntryInput):
    schemaVersion: Literal[1] = LOG_SCHEMA_VERSION
    runId: UUID


class RunLogSummary(BaseModel):
    runId: UUID
    filename: str
    startedAt: datetime
    updatedAt: datetime
    entryCount: int
    lastEvent: str


class RunLogFile(RunLogSummary):
    entries: list[RunLogEntry]


class CorruptRunLogError(RuntimeError):
    """Raised when an existing JSONL log cannot be safely parsed."""


class RunLogNotFoundError(FileNotFoundError):
    """Raised when a requested task log does not exist."""


class RunLogStore:
    def __init__(self, directory: Path = DEFAULT_LOG_DIR) -> None:
        self.directory = Path(directory)
        self._lock = threading.RLock()

    def create_run(self, run_id: UUID, incoming: RunLogCreateInput) -> RunLogSummary:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            started_at = datetime.now(timezone.utc)
            path = self.directory / f"run-{run_id}.jsonl"
            first_entry = RunLogEntry(
                schemaVersion=LOG_SCHEMA_VERSION,
                runId=run_id,
                timestamp=started_at,
                level="info",
                event="run_created",
                message="任务日志已创建",
                details={
                    "requestedCount": incoming.requestedCount,
                    "concurrency": incoming.concurrency,
                    "persisted": True,
                },
            )
            try:
                with path.open("x", encoding="utf-8", newline="\n") as handle:
                    self._write_entry(handle, first_entry)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                path.unlink(missing_ok=True)
                raise
            return self._summary(path, [first_entry])

    def append_entries(
        self, run_id: UUID, incoming: RunLogAppendInput
    ) -> RunLogSummary:
        with self._lock:
            path = self._find_path(run_id)
            existing = self._read_entries(path)
            entries = [
                RunLogEntry(
                    schemaVersion=LOG_SCHEMA_VERSION,
                    runId=run_id,
                    **entry.model_dump(),
                )
                for entry in incoming.entries
            ]
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                for entry in entries:
                    self._write_entry(handle, entry)
                handle.flush()
                os.fsync(handle.fileno())
            combined = [*existing, *entries]
            summary = self._summary(path, combined)
            if summary.lastEvent in TERMINAL_EVENTS:
                self.prune_terminal_runs()
            return summary

    def list_runs(self) -> list[RunLogSummary]:
        with self._lock:
            summaries = self._valid_summaries(terminal_only=True)
            return summaries[:LOG_RETENTION_COUNT]

    def get_run(self, run_id: UUID) -> RunLogFile:
        with self._lock:
            path = self._find_path(run_id)
            entries = self._read_entries(path)
            summary = self._summary(path, entries)
            return RunLogFile(**summary.model_dump(), entries=entries)

    def prune_terminal_runs(self) -> int:
        """Delete only valid terminal run logs older than the latest ten."""
        with self._lock:
            summaries = self._valid_summaries(terminal_only=True)
            deleted = 0
            for summary in summaries[LOG_RETENTION_COUNT:]:
                path = self.directory / summary.filename
                path.unlink()
                deleted += 1
            return deleted

    def _find_path(self, run_id: UUID) -> Path:
        path = self.directory / f"run-{run_id}.jsonl"
        if not path.is_file():
            raise RunLogNotFoundError(f"任务日志不存在：{run_id}")
        return path

    def _valid_summaries(self, *, terminal_only: bool) -> list[RunLogSummary]:
        if not self.directory.exists():
            return []
        summaries: list[RunLogSummary] = []
        for path in self.directory.iterdir():
            match = RUN_LOG_PATTERN.fullmatch(path.name)
            if not path.is_file() or match is None:
                continue
            try:
                expected_run_id = UUID(match.group(1))
                entries = self._read_entries(path)
                if any(entry.runId != expected_run_id for entry in entries):
                    raise CorruptRunLogError(f"任务日志 runId 与文件名不一致：{path}")
                summary = self._summary(path, entries)
            except CorruptRunLogError:
                # Corrupt and unknown files are deliberately left untouched.
                continue
            if terminal_only and summary.lastEvent not in TERMINAL_EVENTS:
                continue
            summaries.append(summary)
        summaries.sort(
            key=lambda item: (item.startedAt, item.filename),
            reverse=True,
        )
        return summaries

    def _read_entries(self, path: Path) -> list[RunLogEntry]:
        entries: list[RunLogEntry] = []
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for raw_line in handle:
                    if not raw_line.strip():
                        continue
                    payload = json.loads(raw_line)
                    entries.append(RunLogEntry.model_validate(payload))
        except (OSError, json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise CorruptRunLogError(
                f"任务日志损坏或不符合 schemaVersion 1：{path}"
            ) from exc
        if not entries:
            raise CorruptRunLogError(f"任务日志为空：{path}")
        return entries

    @staticmethod
    def _summary(path: Path, entries: list[RunLogEntry]) -> RunLogSummary:
        first = entries[0]
        last = entries[-1]
        return RunLogSummary(
            runId=first.runId,
            filename=path.name,
            startedAt=first.timestamp,
            updatedAt=last.timestamp,
            entryCount=len(entries),
            lastEvent=last.event,
        )

    @staticmethod
    def _write_entry(handle: object, entry: RunLogEntry) -> None:
        handle.write(
            json.dumps(entry.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
        )
        handle.write("\n")
