from __future__ import annotations

import re
from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PageSize = Literal[10, 20, 50, 100]
AccountType = Literal["plus", "free"]
CheckoutType = Literal["oaics", "cs"]
PlanCheckStatus = Literal["running", "success", "failed"]
AccountExportFormat = Literal["credentials", "mail-links", "access-tokens"]
ProxyStatus = Literal["available", "unknown", "quarantined"]
ProxyScheme = Literal["http", "https", "socks5", "socks5h"]
EmailSource = Literal["all", "standard", "mailcom_alias"]
EmailSourceType = Literal["manual", "mailcom_alias"]
RunStatus = Literal[
    "idle",
    "queued",
    "running",
    "waiting_for_database",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
RunKind = Literal["mock", "browser_probe"]
WorkerStatus = Literal[
    "queued",
    "running",
    "success",
    "partial_success",
    "failed",
    "cancelled",
]
WorkerStage = Literal[
    "queued",
    "roxy_starting",
    "proxy_check",
    "login",
    "email",
    "verification",
    "profile",
    "access_token",
    "two_factor",
    "password_setup",
    "cleanup",
    "success",
    "partial_success",
    "failed",
    "cancelled",
]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountRecord(ApiModel):
    id: str
    email: str
    chatgptPassword: str
    totpSecret: str
    emailAccessUrl: str
    createdAt: datetime
    accountType: AccountType
    phoneBound: bool | None = None
    promotionEligible: bool | None = None
    accessTokenConfigured: bool = False
    accessTokenExpiresAt: datetime | None = None
    accessTokenUpdatedAt: datetime | None = None
    planCheckStatus: PlanCheckStatus | None = None
    planCheckedAt: datetime | None = None
    planCheckErrorCode: str | None = None
    planCheckHttpStatus: int | None = None
    planAccountId: str | None = None
    subscriptionPlan: str | None = None
    hasActiveSubscription: bool | None = None
    planExpiresAt: datetime | None = None
    planRenewsAt: datetime | None = None
    promotionCampaignId: str | None = None
    checkoutType: CheckoutType | None = None
    checkoutTypeCheckedAt: datetime | None = None
    registrationCountry: str | None = None


class AccountCreate(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    chatgptPassword: str = Field(min_length=1, max_length=1024)
    totpSecret: str = Field(min_length=1, max_length=256)
    emailAccessUrl: str = Field(min_length=1, max_length=4096)
    accountType: AccountType = "free"
    phoneBound: bool | None = None
    promotionEligible: bool | None = None
    sourceEmailId: str | None = None
    registrationCountry: str | None = Field(default=None, min_length=2, max_length=2)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class EmailRecord(ApiModel):
    id: str
    email: str
    accessUrl: str
    importedAt: datetime
    sourceType: EmailSourceType = "manual"
    parentEmail: str | None = None


class ProxyRecord(ApiModel):
    id: str
    host: str
    port: int
    username: str
    password: str
    enabled: bool
    status: ProxyStatus
    latencyMs: int | None = None
    lastCheckedAt: datetime | None = None
    country: str = "ZZ"
    group: str = "默认组"
    scheme: ProxyScheme = "http"


T = TypeVar("T")


class Page(ApiModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    pageSize: PageSize


class RawImportInput(ApiModel):
    rawText: str


class ProxyImportInput(RawImportInput):
    country: str | None = Field(default=None, min_length=2, max_length=2)
    group: str | None = Field(default=None, max_length=64)

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", normalized):
            raise ValueError("country must be a two-letter code")
        return normalized

    @field_validator("group")
    @classmethod
    def normalize_group(cls, value: str | None) -> str | None:
        normalized = " ".join(str(value or "").split())
        return normalized or None


class ImportResult(ApiModel):
    total: int
    imported: int
    duplicateCount: int
    errorCount: int


class BulkIdsInput(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=10000)


class AccountPlanCheckInput(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class AccountPlanCheckItem(ApiModel):
    id: str
    status: Literal["success", "failed", "skipped"]
    errorCode: str | None = None


class AccountPlanCheckResult(ApiModel):
    requested: int
    succeeded: int
    failed: int
    skipped: int
    items: list[AccountPlanCheckItem]


class DeleteResult(ApiModel):
    deleted: int


class AccountExportInput(ApiModel):
    format: AccountExportFormat
    scope: Literal["single", "selected", "all"]
    ids: list[str] = Field(default_factory=list, max_length=10000)


class EmailExportInput(ApiModel):
    scope: Literal["single", "selected", "all"]
    ids: list[str] = Field(default_factory=list, max_length=10000)


class TextExport(ApiModel):
    content: str
    filename: str
    count: int
    format: AccountExportFormat | None = None
    skippedMissingCount: int = 0
    skippedExpiredCount: int = 0


class ProxyUpdate(ApiModel):
    enabled: bool | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    group: str | None = Field(default=None, max_length=64)

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", normalized):
            raise ValueError("country must be a two-letter code")
        return normalized

    @field_validator("group")
    @classmethod
    def normalize_group(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("group is required")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "ProxyUpdate":
        if self.enabled is None and self.country is None and self.group is None:
            raise ValueError("enabled, country or group is required")
        return self


class ProxyCountrySummary(ApiModel):
    country: str
    total: int
    enabled: int


class ProxyGroupSummary(ApiModel):
    country: str
    group: str
    total: int
    enabled: int
    available: int
    quarantined: int
    schemes: list[ProxyScheme] = Field(default_factory=list)


class ProxyGroupUpdate(ApiModel):
    country: str = Field(min_length=2, max_length=2)
    group: str = Field(min_length=1, max_length=64)
    newCountry: str | None = Field(default=None, min_length=2, max_length=2)
    newGroup: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None

    @field_validator("country", "newCountry")
    @classmethod
    def normalize_countries(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", normalized) or normalized == "ZZ":
            raise ValueError("country must be a classified two-letter code")
        return normalized

    @field_validator("group", "newGroup")
    @classmethod
    def normalize_groups(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("group is required")
        return normalized

    @model_validator(mode="after")
    def require_group_change(self) -> "ProxyGroupUpdate":
        if self.newCountry is None and self.newGroup is None and self.enabled is None:
            raise ValueError("newCountry, newGroup or enabled is required")
        return self


class PlusStats(ApiModel):
    total: int = 0
    bound: int = 0
    unbound: int = 0


class FreeStats(ApiModel):
    total: int = 0
    eligible: int = 0
    ineligible: int = 0


class AccountStats(ApiModel):
    total: int = 0
    today: int = 0
    totpComplete: int = 0
    plus: PlusStats = Field(default_factory=PlusStats)
    free: FreeStats = Field(default_factory=FreeStats)


class EmailStats(ApiModel):
    available: int = 0
    aliases: int = 0


class ProxyStats(ApiModel):
    total: int = 0
    enabled: int = 0
    available: int = 0
    quarantined: int = 0


class OverviewStats(ApiModel):
    accounts: AccountStats = Field(default_factory=AccountStats)
    emails: EmailStats = Field(default_factory=EmailStats)
    proxies: ProxyStats = Field(default_factory=ProxyStats)


class MockRunCreate(ApiModel):
    count: int = Field(ge=1, strict=True)


class BrowserProbeRunCreate(ApiModel):
    count: int = Field(ge=1, strict=True)
    country: str = Field(min_length=2, max_length=2)
    group: str = Field(default="默认组", min_length=1, max_length=64)
    emailSource: EmailSource = "all"

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", normalized) or normalized == "ZZ":
            raise ValueError("country must be a classified two-letter code")
        return normalized

    @field_validator("group")
    @classmethod
    def normalize_group(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("group is required")
        return normalized


class RunState(ApiModel):
    runId: str
    kind: RunKind = "mock"
    status: RunStatus
    requested: int
    pending: int
    processed: int
    succeeded: int
    failed: int
    workerCount: int = 0
    activeWorkers: int = 0
    startedAt: datetime
    updatedAt: datetime
    finishedAt: datetime | None = None
    logPersisted: bool = True
    cancelRequested: bool = False
    terminalReasonCode: str | None = None
    registrationCountry: str | None = None
    registrationProxyGroup: str | None = None
    emailSource: EmailSource = "all"


class WorkerSnapshot(ApiModel):
    workerId: str
    sequence: int
    status: WorkerStatus
    stage: WorkerStage
    stageElapsedMs: int = 0
    email: str
    egressIp: str | None = None
    errorCode: str | None = None
    errorStage: str | None = None
    errorOperation: str | None = None
    errorKind: str | None = None
    errorHttpStatus: int | None = None
    errorApiCode: int | None = None
    errorRetryCount: int | None = None
    errorElapsedMs: int | None = None
    startedAt: datetime | None = None
    updatedAt: datetime
    finishedAt: datetime | None = None


class MongoHealth(ApiModel):
    status: Literal["online", "offline", "reconnecting"]
    database: str
    error: str | None = None
    nextRetrySeconds: int | None = None


class HealthResponse(ApiModel):
    status: Literal["ok", "degraded"]
    mode: Literal["local"] = "local"
    mongodb: MongoHealth
