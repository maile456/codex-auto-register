from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)


DEFAULT_SETTINGS_PATH = Path(
    os.environ.get("AUTOREGISTER_SETTINGS_PATH", r"D:\AutoRegister\data\settings.json")
)
LEGACY_BROWSER_PATH = r"D:\ImRun Browser\ImRun Browser.exe"
DEFAULT_BROWSER_PATH = r"D:\RoxyBrowser\RoxyBrowser.exe"


class ExecutionSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browserExecutablePath: str = Field(min_length=1)
    roxyApiKey: SecretStr
    roxyApiPort: int = Field(ge=1, le=65535, strict=True)
    headless: bool = Field(strict=True)
    requireRegistrationPassword: bool = Field(default=False, strict=True)
    enableRegistrationTotp: bool = Field(default=True, strict=True)
    proxyRetryCount: int = Field(ge=0, le=5, strict=True)
    concurrency: int = Field(ge=1, le=12, strict=True)
    taskTimeoutSeconds: int = Field(ge=0, strict=True)

    @field_validator("browserExecutablePath")
    @classmethod
    def validate_browser_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("browserExecutablePath cannot be blank")
        return stripped

    @field_validator("roxyApiKey")
    @classmethod
    def normalize_api_key(cls, value: SecretStr) -> SecretStr:
        return SecretStr(value.get_secret_value().strip())


class StoredExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[2] = 2
    browserExecutablePath: str
    roxyApiKey: SecretStr = SecretStr("")
    roxyApiPort: int = Field(ge=1, le=65535, strict=True)
    headless: bool = Field(strict=True)
    requireRegistrationPassword: bool = Field(default=False, strict=True)
    enableRegistrationTotp: bool = Field(default=True, strict=True)
    proxyRetryCount: int = Field(ge=0, le=5, strict=True)
    concurrency: int = Field(ge=1, le=12, strict=True)
    taskTimeoutSeconds: int = Field(ge=0, strict=True)
    updatedAt: datetime | None = None

    @field_validator("browserExecutablePath")
    @classmethod
    def validate_browser_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("browserExecutablePath cannot be blank")
        return stripped


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[2] = 2
    browserExecutablePath: str
    roxyApiKey: str
    roxyApiPort: int
    headless: bool
    requireRegistrationPassword: bool
    enableRegistrationTotp: bool
    proxyRetryCount: int
    concurrency: int
    taskTimeoutSeconds: int
    updatedAt: datetime | None

    @classmethod
    def from_stored(cls, settings: StoredExecutionSettings) -> "ExecutionSettings":
        return cls(
            schemaVersion=2,
            browserExecutablePath=settings.browserExecutablePath,
            roxyApiKey=settings.roxyApiKey.get_secret_value(),
            roxyApiPort=settings.roxyApiPort,
            headless=settings.headless,
            requireRegistrationPassword=settings.requireRegistrationPassword,
            enableRegistrationTotp=settings.enableRegistrationTotp,
            proxyRetryCount=settings.proxyRetryCount,
            concurrency=settings.concurrency,
            taskTimeoutSeconds=settings.taskTimeoutSeconds,
            updatedAt=settings.updatedAt,
        )


class LegacyExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[1] = 1
    browserExecutablePath: str
    concurrency: int = Field(ge=1, le=12, strict=True)
    taskTimeoutSeconds: int = Field(ge=0, strict=True)
    updatedAt: datetime | None = None


class CorruptSettingsError(RuntimeError):
    """Raised when the persisted settings file cannot be safely validated."""


def default_settings() -> StoredExecutionSettings:
    return StoredExecutionSettings(
        browserExecutablePath=DEFAULT_BROWSER_PATH,
        roxyApiKey=SecretStr(""),
        roxyApiPort=50000,
        headless=False,
        requireRegistrationPassword=False,
        enableRegistrationTotp=True,
        proxyRetryCount=1,
        concurrency=2,
        taskTimeoutSeconds=0,
        updatedAt=None,
    )


class SettingsStore:
    def __init__(self, path: Path = DEFAULT_SETTINGS_PATH) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self.temp_path = self.path.with_name(f"{self.path.name}.tmp")
        self._lock = threading.RLock()

    def load(self) -> StoredExecutionSettings:
        with self._lock:
            if not self.path.exists():
                return default_settings()
            return self._load_existing()

    def load_public(self) -> ExecutionSettings:
        return ExecutionSettings.from_stored(self.load())

    def _load_existing(self) -> StoredExecutionSettings:
        try:
            with self.path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise TypeError("settings root must be an object")
            return self._validate_or_migrate(payload)
        except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise CorruptSettingsError(
                f"配置文件损坏或不符合受支持的 schema：{self.path}"
            ) from exc

    @staticmethod
    def _validate_or_migrate(payload: dict[str, Any]) -> StoredExecutionSettings:
        schema_version = payload.get("schemaVersion")
        if schema_version == 2:
            return StoredExecutionSettings.model_validate(payload)
        if schema_version == 1:
            legacy = LegacyExecutionSettings.model_validate(payload)
            browser_path = legacy.browserExecutablePath.strip()
            if browser_path.casefold() == LEGACY_BROWSER_PATH.casefold():
                browser_path = DEFAULT_BROWSER_PATH
            return StoredExecutionSettings(
                browserExecutablePath=browser_path,
                roxyApiKey=SecretStr(""),
                roxyApiPort=50000,
                headless=False,
                requireRegistrationPassword=False,
                enableRegistrationTotp=True,
                proxyRetryCount=1,
                concurrency=legacy.concurrency,
                taskTimeoutSeconds=legacy.taskTimeoutSeconds,
                updatedAt=legacy.updatedAt,
            )
        raise ValueError("unsupported settings schemaVersion")

    def save(self, incoming: ExecutionSettingsInput) -> StoredExecutionSettings:
        with self._lock:
            existing = self._load_existing() if self.path.exists() else default_settings()
            settings = StoredExecutionSettings(
                browserExecutablePath=incoming.browserExecutablePath,
                roxyApiKey=incoming.roxyApiKey,
                roxyApiPort=incoming.roxyApiPort,
                headless=incoming.headless,
                requireRegistrationPassword=incoming.requireRegistrationPassword,
                enableRegistrationTotp=incoming.enableRegistrationTotp,
                proxyRetryCount=incoming.proxyRetryCount,
                concurrency=incoming.concurrency,
                taskTimeoutSeconds=incoming.taskTimeoutSeconds,
                schemaVersion=2,
                updatedAt=datetime.now(timezone.utc),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)

            payload = settings.model_dump(mode="json")
            payload["roxyApiKey"] = settings.roxyApiKey.get_secret_value()
            try:
                with self.temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())

                if self.path.exists():
                    shutil.copy2(self.path, self.backup_path)
                os.replace(self.temp_path, self.path)
            except OSError:
                self.temp_path.unlink(missing_ok=True)
                raise

            return settings
