from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse, urlsplit
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .errors import (
    DuplicateResourceError,
    InsufficientEmailsError,
    MongoUnavailableError,
    ResourceNotFoundError,
)
from .mongo_manager import MongoManager
from .mailbox_client import MAILCOM_WEBMAIL_URL, direct_mailbox_access_url
from .resource_models import (
    AccountCreate,
    AccountExportInput,
    AccountRecord,
    AccountStats,
    DeleteResult,
    EmailExportInput,
    EmailRecord,
    EmailStats,
    FreeStats,
    ImportResult,
    OverviewStats,
    Page,
    PageSize,
    PlusStats,
    ProxyRecord,
    ProxyCountrySummary,
    ProxyGroupSummary,
    ProxyStats,
    TextExport,
)
from .chatgpt_plan import AccountPlanResult, PlanCheckError
from .checkout_type import CheckoutTypeResult
from .totp import normalize_totp_secret


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROXY_COUNTRY_PATTERN = re.compile(
    r"(?:^|[-_.])(?:region|country|res|area|dc|res_sc)-([A-Za-z]{2})(?:[-_.:]|$)",
    re.IGNORECASE,
)
SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
DEFAULT_PROXY_GROUP = "默认组"
REGISTRATION_EXCLUDED_EMAIL_DOMAINS = tuple(
    domain.strip().casefold().lstrip("@")
    for domain in os.getenv(
        "AUTOREGISTER_EXCLUDED_EMAIL_DOMAINS", "icloud.com"
    ).split(",")
    if domain.strip().lstrip("@")
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_proxy_group(value: Any) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:64] or DEFAULT_PROXY_GROUP


def proxy_group_filter(value: str | None) -> dict[str, Any]:
    group = normalize_proxy_group(value)
    if group == DEFAULT_PROXY_GROUP:
        return {
            "$or": [
                {"group": DEFAULT_PROXY_GROUP},
                {"group": {"$exists": False}},
                {"group": None},
                {"group": ""},
            ]
        }
    return {"group": group}


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def email_source_filter(source: str | None) -> dict[str, Any]:
    normalized = str(source or "all").strip().casefold()
    if normalized == "mailcom_alias":
        return {"sourceType": "mailcom_alias"}
    if normalized == "standard":
        return {
            "$or": [
                {"sourceType": {"$exists": False}},
                {"sourceType": {"$ne": "mailcom_alias"}},
            ]
        }
    return {}


def registration_email_filter(source: str | None = "all") -> dict[str, Any]:
    query: dict[str, Any] = {"status": "available"}
    query.update(email_source_filter(source))
    if REGISTRATION_EXCLUDED_EMAIL_DOMAINS:
        query["$nor"] = [
            {
                "emailNormalized": {
                    "$regex": rf"@{re.escape(domain)}$",
                    "$options": "i",
                }
            }
            for domain in REGISTRATION_EXCLUDED_EMAIL_DOMAINS
        ]
    return query


def export_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def normalize_country_code(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if re.fullmatch(r"[A-Z]{2}", normalized) else "ZZ"


def infer_proxy_country(username: str, host: str = "") -> str:
    for candidate in (username, host):
        match = PROXY_COUNTRY_PATTERN.search(str(candidate or ""))
        if match is not None:
            return match.group(1).upper()
    return "ZZ"


def proxy_country_filter(country: str) -> dict[str, Any]:
    normalized = normalize_country_code(country)
    escaped = re.escape(normalized)
    inferred_pattern = {
        "$regex": rf"(?:^|[-_.])(?:region|country|res|area|dc|res_sc)-{escaped}(?:[-_.:]|$)",
        "$options": "i",
    }
    return {
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


class MongoResourceStore:
    def __init__(self, manager: MongoManager) -> None:
        self.manager = manager

    @property
    def accounts(self) -> Any:
        return self.manager.database["accounts"]

    @property
    def emails(self) -> Any:
        return self.manager.database["emails"]

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
            raise MongoUnavailableError("MongoDB 当前不可用，请检查本机服务") from exc

    async def ensure_indexes(self) -> None:
        self.manager.require_online()
        try:
            await self.accounts.create_index(
                [("emailNormalized", ASCENDING)], unique=True, name="accounts_email_unique"
            )
            await self.accounts.create_index(
                [("createdAt", DESCENDING)], name="accounts_created_desc"
            )
            await self.accounts.create_index(
                [("accountType", ASCENDING)], name="accounts_type"
            )
            await self.accounts.create_index(
                [("sourceEmailId", ASCENDING)],
                unique=True,
                sparse=True,
                name="accounts_source_email_unique",
            )
            await self.emails.create_index(
                [("emailNormalized", ASCENDING)], unique=True, name="emails_email_unique"
            )
            await self.emails.create_index(
                [("status", ASCENDING), ("importedAt", DESCENDING)],
                name="emails_status_imported",
            )
            await self.emails.create_index(
                [
                    ("status", ASCENDING),
                    ("lastAttemptAt", ASCENDING),
                    ("importedAt", DESCENDING),
                ],
                name="emails_status_attempt_imported",
            )
            await self.proxies.create_index(
                [
                    ("host", ASCENDING),
                    ("port", ASCENDING),
                    ("username", ASCENDING),
                    ("password", ASCENDING),
                ],
                unique=True,
                name="proxies_identity_unique",
            )
            await self.proxies.create_index(
                [("country", ASCENDING), ("enabled", ASCENDING)],
                name="proxies_country_enabled",
            )
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 索引初始化失败") from exc

    async def release_orphaned_reservations(self, active_run_ids: list[str]) -> int:
        query: dict[str, Any] = {"status": "reserved"}
        if active_run_ids:
            query["reservedBy"] = {"$nin": active_run_ids}
        result = await self._guard(
            self.emails.update_many(
                query,
                {
                    "$set": {"status": "available"},
                    "$unset": {"reservedBy": "", "reservedAt": ""},
                },
            )
        )
        return int(result.modified_count)

    async def reconcile_run_reservations(self, run_id: str) -> tuple[int, int]:
        """Finalize already-created accounts, then release every other reservation."""
        cursor = self.emails.find({"status": "reserved", "reservedBy": run_id})
        documents = await self._guard(cursor.to_list(length=None))
        consumed = released = 0
        for email in documents:
            account = await self._guard(
                self.accounts.find_one(
                    {
                        "$or": [
                            {"sourceEmailId": email["_id"]},
                            {"emailNormalized": email["emailNormalized"]},
                        ]
                    }
                )
            )
            if account is not None:
                result = await self._guard(
                    self.emails.delete_one(
                        {"_id": email["_id"], "reservedBy": run_id}
                    )
                )
                consumed += int(result.deleted_count)
            else:
                result = await self._guard(
                    self.emails.update_one(
                        {"_id": email["_id"], "reservedBy": run_id},
                        {
                            "$set": {"status": "available"},
                            "$unset": {"reservedBy": "", "reservedAt": ""},
                        },
                    )
                )
                released += int(result.modified_count)
        return consumed, released

    async def list_accounts(
        self,
        page: int,
        page_size: PageSize,
        query: str,
        promotion: str = "",
    ) -> Page[AccountRecord]:
        mongo_query: dict[str, Any] = {}
        if query.strip():
            mongo_query["emailNormalized"] = {
                "$regex": re.escape(query.strip().lower()),
            }
        if promotion == "untried_plus":
            mongo_query.update({"accountType": "free", "promotionEligible": True})
        elif promotion == "ineligible":
            mongo_query["promotionEligible"] = False
        elif promotion == "unchecked":
            mongo_query["$or"] = [
                {"promotionEligible": None},
                {"promotionEligible": {"$exists": False}},
            ]
        total = await self._guard(self.accounts.count_documents(mongo_query))
        cursor = (
            self.accounts.find(mongo_query, {"accessToken": 0})
            .sort("createdAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self._guard(cursor.to_list(length=page_size))
        return Page[AccountRecord](
            items=[self._account_record(item) for item in documents],
            total=total,
            page=page,
            pageSize=page_size,
        )

    async def create_account(self, incoming: AccountCreate) -> AccountRecord:
        now = utc_now()
        document = {
            "_id": str(uuid4()),
            "email": incoming.email,
            "emailNormalized": normalize_email(incoming.email),
            "chatgptPassword": incoming.chatgptPassword,
            "totpSecret": incoming.totpSecret,
            "emailAccessUrl": incoming.emailAccessUrl,
            "createdAt": now,
            "accountType": incoming.accountType,
            "phoneBound": incoming.phoneBound,
            "promotionEligible": incoming.promotionEligible,
            "registrationCountry": (
                normalize_country_code(incoming.registrationCountry)
                if incoming.registrationCountry
                else None
            ),
            "accessTokenConfigured": False,
            "accessTokenExpiresAt": None,
            "accessTokenUpdatedAt": None,
        }
        if incoming.sourceEmailId:
            document["sourceEmailId"] = incoming.sourceEmailId
        try:
            await self._guard(self.accounts.insert_one(document))
        except DuplicateKeyError as exc:
            raise DuplicateResourceError(f"账号已存在：{incoming.email}") from exc
        if incoming.sourceEmailId:
            await self._guard(
                self.emails.delete_one(
                    {
                        "_id": incoming.sourceEmailId,
                        "emailNormalized": document["emailNormalized"],
                    }
                )
            )
        return self._account_record(document)

    async def delete_accounts(self, ids: list[str]) -> DeleteResult:
        result = await self._guard(self.accounts.delete_many({"_id": {"$in": ids}}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def accounts_for_export(self, ids: list[str] | None) -> list[AccountRecord]:
        query = {} if ids is None else {"_id": {"$in": ids}}
        cursor = self.accounts.find(query, {"accessToken": 0}).sort(
            "createdAt", DESCENDING
        )
        documents = await self._guard(cursor.to_list(length=None))
        return [self._account_record(item) for item in documents]

    async def access_tokens_for_export(
        self,
        ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        query = {} if ids is None else {"_id": {"$in": ids}}
        cursor = self.accounts.find(
            query,
            {
                "accessToken": 1,
                "accessTokenConfigured": 1,
                "accessTokenExpiresAt": 1,
                "createdAt": 1,
            },
        ).sort("createdAt", DESCENDING)
        return await self._guard(cursor.to_list(length=None))

    async def store_account_access_token(
        self,
        account_id: str,
        access_token: str,
        expires_at: datetime,
    ) -> datetime:
        if expires_at.tzinfo is None:
            raise ValueError("AccessToken 过期时间必须包含时区")
        updated_at = utc_now()
        result = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "accessToken": access_token,
                        "accessTokenConfigured": True,
                        "accessTokenExpiresAt": expires_at.astimezone(timezone.utc),
                        "accessTokenUpdatedAt": updated_at,
                    }
                },
            )
        )
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("AccessToken 对应账号不存在")
        return updated_at

    async def store_account_totp(
        self,
        account_id: str,
        secret: str,
        access_token: str,
        expires_at: datetime,
        activated_at: datetime,
    ) -> datetime:
        normalized_secret = normalize_totp_secret(secret)
        if expires_at.tzinfo is None or activated_at.tzinfo is None:
            raise ValueError("2FA 时间字段必须包含时区")
        updated_at = utc_now()
        result = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "totpSecret": normalized_secret,
                        "totpActivatedAt": activated_at.astimezone(timezone.utc),
                        "totpStatus": "enabled",
                        "totpError": None,
                        "accessToken": access_token,
                        "accessTokenConfigured": True,
                        "accessTokenExpiresAt": expires_at.astimezone(timezone.utc),
                        "accessTokenUpdatedAt": updated_at,
                    }
                },
            )
        )
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("2FA 对应账号不存在")
        return updated_at

    async def store_account_password(
        self,
        account_id: str,
        password: str,
        configured_at: datetime,
    ) -> datetime:
        if not password or configured_at.tzinfo is None:
            raise ValueError("账号密码或配置时间无效")
        updated_at = utc_now()
        result = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "chatgptPassword": password,
                        "passwordStatus": "enabled",
                        "passwordConfiguredAt": configured_at.astimezone(timezone.utc),
                        "passwordError": None,
                        "updatedAt": updated_at,
                    }
                },
            )
        )
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("密码对应账号不存在")
        return updated_at

    async def claim_account_plan_check(self, account_id: str) -> dict[str, Any] | None:
        now = utc_now()
        return await self._guard(
            self.accounts.find_one_and_update(
                {
                    "_id": account_id,
                    "accessTokenConfigured": True,
                    "accessToken": {"$type": "string", "$ne": ""},
                    "$or": [
                        {"planCheckStatus": {"$ne": "running"}},
                        {
                            "planCheckStartedAt": {
                                "$lte": now - timedelta(minutes=5)
                            }
                        },
                    ],
                },
                {
                    "$set": {
                        "planCheckStatus": "running",
                        "planCheckStartedAt": now,
                        "planCheckErrorCode": None,
                        "planCheckHttpStatus": None,
                    }
                },
                projection={
                    "accessToken": 1,
                    "accessTokenExpiresAt": 1,
                },
                return_document=ReturnDocument.AFTER,
            )
        )

    async def store_account_plan_result(
        self,
        account_id: str,
        result: AccountPlanResult,
    ) -> None:
        updates: dict[str, Any] = {
            "planCheckStatus": "success",
            "planCheckedAt": result.checked_at,
            "planCheckErrorCode": None,
            "planCheckHttpStatus": result.http_status,
            "planAccountId": result.account_id,
            "subscriptionPlan": result.subscription_plan,
            "hasActiveSubscription": result.has_active_subscription,
            "planExpiresAt": result.expires_at,
            "planRenewsAt": result.renews_at,
            "promotionEligible": result.plus_trial_eligible,
            "promotionCampaignId": result.plus_trial_campaign_id,
        }
        normalized_plan = (result.current_plan_type or "").casefold()
        if normalized_plan in {"free", "plus"}:
            updates["accountType"] = normalized_plan
        result_update = await self._guard(
            self.accounts.update_one({"_id": account_id}, {"$set": updates})
        )
        if int(result_update.matched_count) != 1:
            raise ResourceNotFoundError("套餐查询对应账号不存在")

    async def store_account_plan_failure(
        self,
        account_id: str,
        error: PlanCheckError,
    ) -> None:
        updates: dict[str, Any] = {
            "planCheckStatus": "failed",
            "planCheckedAt": utc_now(),
            "planCheckErrorCode": error.code,
            "planCheckHttpStatus": error.http_status,
        }
        if error.code in {"access_token_expired", "access_token_unauthorized"}:
            updates["accessTokenConfigured"] = False
        await self._guard(
            self.accounts.update_one({"_id": account_id}, {"$set": updates})
        )

    async def store_account_checkout_type(
        self,
        account_id: str,
        result: CheckoutTypeResult,
    ) -> None:
        update = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "checkoutType": result.checkout_type,
                        "checkoutTypeCheckedAt": result.checked_at,
                    }
                },
            )
        )
        if int(update.matched_count) != 1:
            raise ResourceNotFoundError("结账类型对应账号不存在")

    async def list_emails(
        self, page: int, page_size: PageSize, query: str, source: str = "all"
    ) -> Page[EmailRecord]:
        mongo_query: dict[str, Any] = {"status": "available"}
        mongo_query.update(email_source_filter(source))
        if query.strip():
            mongo_query["emailNormalized"] = {
                "$regex": re.escape(query.strip().lower()),
            }
        mongo_query = await self._exclude_registered_accounts(mongo_query)
        total = await self._guard(self.emails.count_documents(mongo_query))
        cursor = (
            self.emails.find(mongo_query)
            .sort("importedAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self._guard(cursor.to_list(length=page_size))
        return Page[EmailRecord](
            items=[self._email_record(item) for item in documents],
            total=total,
            page=page,
            pageSize=page_size,
        )

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
        normalized_email = normalize_email(email)
        registered_account = await self._guard(
            self.accounts.find_one(
                {"emailNormalized": normalized_email},
                {"_id": 1},
            )
        )
        if registered_account is not None:
            return False
        document = {
            "_id": str(uuid4()),
            "email": email,
            "emailNormalized": normalized_email,
            "accessUrl": access_url,
            "importedAt": utc_now(),
            "status": "available",
            "mailboxKind": mailbox_kind,
            "sourceType": (
                "mailcom_alias" if source_type == "mailcom_alias" else "manual"
            ),
        }
        if source_type == "mailcom_alias" and parent_email:
            document["parentEmail"] = normalize_email(parent_email)
        if mailbox_kind == "mailcom_imap" and mailbox_password:
            document["mailboxPassword"] = mailbox_password
        set_on_insert = dict(document)
        update: dict[str, Any] = {"$setOnInsert": set_on_insert}
        if source_type == "mailcom_alias":
            mutable_fields = {
                "accessUrl": access_url,
                "sourceType": "mailcom_alias",
                "parentEmail": normalize_email(parent_email or ""),
            }
            for field in mutable_fields:
                set_on_insert.pop(field, None)
            update["$set"] = mutable_fields
        result = await self._guard(
            self.emails.update_one(
                {"emailNormalized": document["emailNormalized"]},
                update,
                upsert=True,
            )
        )
        return result.upserted_id is not None

    async def delete_emails(self, ids: list[str]) -> DeleteResult:
        result = await self._guard(self.emails.delete_many({"_id": {"$in": ids}}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def emails_for_export(self, ids: list[str] | None) -> list[EmailRecord]:
        query: dict[str, Any] = {"status": "available"}
        if ids is not None:
            query["_id"] = {"$in": ids}
        query = await self._exclude_registered_accounts(query)
        cursor = self.emails.find(query).sort("importedAt", DESCENDING)
        documents = await self._guard(cursor.to_list(length=None))
        return [self._email_record(item) for item in documents]

    async def reserve_emails(
        self,
        count: int,
        run_id: str,
        email_source: str = "all",
    ) -> list[dict[str, Any]]:
        reserved: list[dict[str, Any]] = []
        query = await self._exclude_registered_accounts(
            registration_email_filter(email_source)
        )
        for _ in range(count):
            document = await self._guard(
                self.emails.find_one_and_update(
                    query,
                    {
                        "$set": {
                            "status": "reserved",
                            "reservedBy": run_id,
                            "reservedAt": utc_now(),
                        }
                    },
                    # Prefer never-attempted mailboxes. A failed mailbox is
                    # released to the back of the queue so a retry run does
                    # not immediately receive the same stale verification
                    # message again.
                    sort=[("lastAttemptAt", ASCENDING), ("importedAt", DESCENDING)],
                    return_document=ReturnDocument.AFTER,
                )
            )
            if document is None:
                await self.release_run_reservations(run_id)
                raise InsufficientEmailsError("可用邮箱数量不足")
            reserved.append(document)
        return reserved

    async def get_reserved_email(
        self,
        email_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        document = await self._guard(
            self.emails.find_one(
                {
                    "_id": email_id,
                    "status": "reserved",
                    "reservedBy": run_id,
                }
            )
        )
        if document is None:
            raise ResourceNotFoundError("预留邮箱不存在或不属于当前真实探测任务")
        return document

    async def release_email(self, email_id: str, run_id: str) -> None:
        await self._guard(
            self.emails.update_one(
                {"_id": email_id, "reservedBy": run_id},
                {
                    "$set": {
                        "status": "available",
                        "lastAttemptAt": utc_now(),
                    },
                    "$unset": {"reservedBy": "", "reservedAt": ""},
                },
            )
        )

    async def release_run_reservations(self, run_id: str) -> None:
        await self._guard(
            self.emails.update_many(
                {"reservedBy": run_id},
                {
                    "$set": {"status": "available"},
                    "$unset": {"reservedBy": "", "reservedAt": ""},
                },
            )
        )

    async def complete_mock_success(
        self,
        source: dict[str, Any],
        run_id: str,
        promotion_eligible: bool,
    ) -> AccountRecord:
        fragment = re.sub(r"[^a-zA-Z0-9]", "", source["email"].split("@")[0])[:12]
        fragment = fragment or "account"
        document = {
            "_id": str(uuid4()),
            "email": source["email"],
            "emailNormalized": normalize_email(source["email"]),
            "chatgptPassword": f"Mock!{fragment}#2026",
            "totpSecret": (f"MOCK{fragment.upper()}" + "X" * 20)[:20],
            "emailAccessUrl": source["accessUrl"],
            "createdAt": utc_now(),
            "accountType": "free",
            "phoneBound": None,
            "promotionEligible": promotion_eligible,
            "sourceEmailId": source["_id"],
        }
        if source.get("mailboxKind") == "mailcom_imap":
            document["mailboxKind"] = "mailcom_imap"
            document["mailboxPassword"] = source.get("mailboxPassword", "")
        await self._guard(
            self.accounts.update_one(
                {"emailNormalized": document["emailNormalized"]},
                {"$setOnInsert": document},
                upsert=True,
            )
        )
        await self._guard(
            self.emails.delete_one({"_id": source["_id"], "reservedBy": run_id})
        )
        stored = await self._guard(
            self.accounts.find_one({"emailNormalized": document["emailNormalized"]})
        )
        if stored is None:
            raise ResourceNotFoundError("账号写入后无法读取")
        return self._account_record(stored)

    async def complete_probe_profile_success(
        self,
        source: dict[str, Any],
        run_id: str,
        chatgpt_password: str = "",
        registration_country: str | None = None,
    ) -> AccountRecord:
        document = {
            "_id": str(uuid4()),
            "email": source["email"],
            "emailNormalized": normalize_email(source["email"]),
            "chatgptPassword": chatgpt_password,
            "totpSecret": "",
            "emailAccessUrl": source["accessUrl"],
            "createdAt": utc_now(),
            "accountType": "free",
            "phoneBound": None,
            "promotionEligible": None,
            "registrationCountry": (
                normalize_country_code(registration_country)
                if registration_country
                else None
            ),
            "sourceEmailId": source["_id"],
            "accessTokenConfigured": False,
            "accessTokenExpiresAt": None,
            "accessTokenUpdatedAt": None,
        }
        if source.get("mailboxKind") == "mailcom_imap":
            document["mailboxKind"] = "mailcom_imap"
            document["mailboxPassword"] = source.get("mailboxPassword", "")
        await self._guard(
            self.accounts.update_one(
                {"emailNormalized": document["emailNormalized"]},
                {"$setOnInsert": document},
                upsert=True,
            )
        )
        await self._guard(
            self.emails.delete_one(
                {
                    "_id": source["_id"],
                    "emailNormalized": document["emailNormalized"],
                    "reservedBy": run_id,
                }
            )
        )
        stored = await self._guard(
            self.accounts.find_one(
                {"emailNormalized": document["emailNormalized"]}
            )
        )
        if stored is None:
            raise ResourceNotFoundError("账号资料完成后无法读取账号记录")
        return self._account_record(stored)

    async def list_proxies(
        self, page: int, page_size: PageSize, query: str, country: str = ""
    ) -> Page[ProxyRecord]:
        mongo_query: dict[str, Any] = {}
        if query.strip():
            escaped = re.escape(query.strip())
            mongo_query["$or"] = [
                {"host": {"$regex": escaped, "$options": "i"}},
                {"username": {"$regex": escaped, "$options": "i"}},
            ]
        if country.strip():
            country_query = proxy_country_filter(country)
            if "$or" in mongo_query:
                mongo_query = {"$and": [mongo_query, country_query]}
            else:
                mongo_query.update(country_query)
        total = await self._guard(self.proxies.count_documents(mongo_query))
        cursor = (
            self.proxies.find(mongo_query)
            .sort("createdAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self._guard(cursor.to_list(length=page_size))
        return Page[ProxyRecord](
            items=[self._proxy_record(item) for item in documents],
            total=total,
            page=page,
            pageSize=page_size,
        )

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
        identity = {"host": host, "port": port, "username": username, "password": password}
        document = {
            "_id": str(uuid4()),
            **identity,
            "enabled": True,
            "status": "unknown",
            "latencyMs": None,
            "lastCheckedAt": None,
            "country": normalize_country_code(
                country or infer_proxy_country(username, host)
            ),
            "group": normalize_proxy_group(group),
            "scheme": scheme if scheme in SUPPORTED_PROXY_SCHEMES else "http",
            "createdAt": utc_now(),
        }
        updates = {"scheme": scheme if scheme in SUPPORTED_PROXY_SCHEMES else "http"}
        if group is not None:
            updates["group"] = normalize_proxy_group(group)
        if country is not None:
            updates["country"] = normalize_country_code(country)
        # MongoDB rejects an upsert when the same path appears in both
        # $setOnInsert and $set. Keep mutable fields exclusively in $set so a
        # repeated import can update the protocol/country without conflicting
        # with a first-time insert.
        insert_only = {
            key: value for key, value in document.items() if key not in updates
        }
        update: dict[str, Any] = {"$setOnInsert": insert_only, "$set": updates}
        result = await self._guard(self.proxies.update_one(identity, update, upsert=True))
        return result.upserted_id is not None or bool(getattr(result, "modified_count", 0))

    async def update_proxy(
        self,
        proxy_id: str,
        *,
        enabled: bool | None = None,
        country: str | None = None,
        group: str | None = None,
    ) -> ProxyRecord:
        changes: dict[str, Any] = {}
        if enabled is not None:
            changes["enabled"] = enabled
        if country is not None:
            changes["country"] = normalize_country_code(country)
        if group is not None:
            changes["group"] = normalize_proxy_group(group)
        document = await self._guard(
            self.proxies.find_one_and_update(
                {"_id": proxy_id},
                {"$set": changes},
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is None:
            raise ResourceNotFoundError("代理不存在")
        return self._proxy_record(document)

    async def proxy_country_summaries(self) -> list[ProxyCountrySummary]:
        cursor = self.proxies.find({}, {"country": 1, "username": 1, "host": 1, "enabled": 1})
        documents = await self._guard(cursor.to_list(length=None))
        counts: dict[str, list[int]] = {}
        for document in documents:
            country = normalize_country_code(document.get("country"))
            if country == "ZZ":
                country = infer_proxy_country(
                    str(document.get("username") or ""),
                    str(document.get("host") or ""),
                )
            values = counts.setdefault(country, [0, 0])
            values[0] += 1
            if bool(document.get("enabled", False)):
                values[1] += 1
        return [
            ProxyCountrySummary(country=country, total=values[0], enabled=values[1])
            for country, values in sorted(counts.items())
        ]

    async def proxy_group_summaries(self) -> list[ProxyGroupSummary]:
        cursor = self.proxies.find(
            {},
            {
                "country": 1,
                "group": 1,
                "username": 1,
                "host": 1,
                "enabled": 1,
                "status": 1,
                "scheme": 1,
            },
        )
        documents = await self._guard(cursor.to_list(length=None))
        counts: dict[tuple[str, str], dict[str, Any]] = {}
        for document in documents:
            country = normalize_country_code(document.get("country"))
            if country == "ZZ":
                country = infer_proxy_country(
                    str(document.get("username") or ""),
                    str(document.get("host") or ""),
                )
            group = normalize_proxy_group(document.get("group"))
            values = counts.setdefault(
                (country, group),
                {"total": 0, "enabled": 0, "available": 0, "quarantined": 0, "schemes": set()},
            )
            values["total"] += 1
            if bool(document.get("enabled", False)):
                values["enabled"] += 1
            status = str(document.get("status") or "unknown")
            if status == "available":
                values["available"] += 1
            elif status == "quarantined":
                values["quarantined"] += 1
            scheme = str(document.get("scheme") or "http").lower()
            values["schemes"].add(
                scheme if scheme in SUPPORTED_PROXY_SCHEMES else "http"
            )
        return [
            ProxyGroupSummary(
                country=country,
                group=group,
                total=values["total"],
                enabled=values["enabled"],
                available=values["available"],
                quarantined=values["quarantined"],
                schemes=sorted(values["schemes"]),
            )
            for (country, group), values in sorted(counts.items())
        ]

    async def update_proxy_group(
        self,
        country: str,
        group: str,
        *,
        new_country: str | None = None,
        new_group: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, int]:
        query = {"$and": [proxy_country_filter(country), proxy_group_filter(group)]}
        changes: dict[str, Any] = {}
        if new_country is not None:
            changes["country"] = normalize_country_code(new_country)
        if new_group is not None:
            changes["group"] = normalize_proxy_group(new_group)
        if enabled is not None:
            changes["enabled"] = enabled
        result = await self._guard(self.proxies.update_many(query, {"$set": changes}))
        return {"matched": int(result.matched_count), "modified": int(result.modified_count)}

    async def delete_proxy_group(self, country: str, group: str) -> DeleteResult:
        query = {"$and": [proxy_country_filter(country), proxy_group_filter(group)]}
        result = await self._guard(self.proxies.delete_many(query))
        return DeleteResult(deleted=int(result.deleted_count))

    async def enabled_proxy_urls(
        self,
        country: str,
        *,
        group: str | None = None,
        scheme: str | None = None,
    ) -> list[str]:
        normalized = normalize_country_code(country)
        if normalized == "ZZ":
            return []
        filters: list[dict[str, Any]] = [
            proxy_country_filter(normalized),
            {"enabled": True, "status": {"$ne": "quarantined"}},
        ]
        if group:
            filters.append(proxy_group_filter(group))
        query = {"$and": filters}
        cursor = self.proxies.find(query).sort("createdAt", ASCENDING)
        documents = await self._guard(cursor.to_list(length=None))
        result: list[str] = []
        for document in documents:
            host = str(document.get("host") or "").strip()
            port = int(document.get("port") or 0)
            username = quote(str(document.get("username") or ""), safe="")
            password = quote(str(document.get("password") or ""), safe="")
            if not host or port < 1:
                continue
            credentials = f"{username}:{password}@" if username or password else ""
            proxy_scheme = str(scheme or document.get("scheme") or "http").lower()
            if proxy_scheme not in SUPPORTED_PROXY_SCHEMES:
                proxy_scheme = "http"
            result.append(f"{proxy_scheme}://{credentials}{host}:{port}")
        return result

    async def delete_proxy(self, proxy_id: str) -> DeleteResult:
        result = await self._guard(self.proxies.delete_one({"_id": proxy_id}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def delete_proxies(self, ids: list[str]) -> DeleteResult:
        result = await self._guard(self.proxies.delete_many({"_id": {"$in": ids}}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def clear_proxies(self) -> DeleteResult:
        result = await self._guard(self.proxies.delete_many({}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def overview_stats(self) -> OverviewStats:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        available_email_query = await self._exclude_registered_accounts(
            registration_email_filter()
        )
        available_alias_query = await self._exclude_registered_accounts(
            registration_email_filter("mailcom_alias")
        )
        (
            accounts_total,
            accounts_today,
            totp_complete,
            plus_total,
            plus_bound,
            free_total,
            free_eligible,
            emails_available,
            email_aliases,
            proxies_total,
            proxies_enabled,
            proxies_available,
            proxies_quarantined,
        ) = await asyncio.gather(
            self._guard(self.accounts.count_documents({})),
            self._guard(self.accounts.count_documents({"createdAt": {"$gte": today}})),
            self._guard(self.accounts.count_documents({"totpSecret": {"$nin": ["", None]}})),
            self._guard(self.accounts.count_documents({"accountType": "plus"})),
            self._guard(
                self.accounts.count_documents({"accountType": "plus", "phoneBound": True})
            ),
            self._guard(self.accounts.count_documents({"accountType": "free"})),
            self._guard(
                self.accounts.count_documents(
                    {"accountType": "free", "promotionEligible": True}
                )
            ),
            self._guard(self.emails.count_documents(available_email_query)),
            self._guard(self.emails.count_documents(available_alias_query)),
            self._guard(self.proxies.count_documents({})),
            self._guard(self.proxies.count_documents({"enabled": True})),
            self._guard(self.proxies.count_documents({"status": "available"})),
            self._guard(self.proxies.count_documents({"status": "quarantined"})),
        )
        return OverviewStats(
            accounts=AccountStats(
                total=accounts_total,
                today=accounts_today,
                totpComplete=totp_complete,
                plus=PlusStats(
                    total=plus_total,
                    bound=plus_bound,
                    unbound=plus_total - plus_bound,
                ),
                free=FreeStats(
                    total=free_total,
                    eligible=free_eligible,
                    ineligible=free_total - free_eligible,
                ),
            ),
            emails=EmailStats(available=emails_available, aliases=email_aliases),
            proxies=ProxyStats(
                total=proxies_total,
                enabled=proxies_enabled,
                available=proxies_available,
                quarantined=proxies_quarantined,
            ),
        )

    @staticmethod
    def _account_record(document: dict[str, Any]) -> AccountRecord:
        return AccountRecord(
            id=str(document["_id"]),
            email=document["email"],
            chatgptPassword=document["chatgptPassword"],
            totpSecret=document["totpSecret"],
            emailAccessUrl=direct_mailbox_access_url(
                document["emailAccessUrl"], document["email"]
            ),
            createdAt=document["createdAt"],
            accountType=document["accountType"],
            phoneBound=document.get("phoneBound"),
            promotionEligible=document.get("promotionEligible"),
            accessTokenConfigured=bool(document.get("accessTokenConfigured", False)),
            accessTokenExpiresAt=document.get("accessTokenExpiresAt"),
            accessTokenUpdatedAt=document.get("accessTokenUpdatedAt"),
            planCheckStatus=document.get("planCheckStatus"),
            planCheckedAt=document.get("planCheckedAt"),
            planCheckErrorCode=document.get("planCheckErrorCode"),
            planCheckHttpStatus=document.get("planCheckHttpStatus"),
            planAccountId=document.get("planAccountId"),
            subscriptionPlan=document.get("subscriptionPlan"),
            hasActiveSubscription=document.get("hasActiveSubscription"),
            planExpiresAt=document.get("planExpiresAt"),
            planRenewsAt=document.get("planRenewsAt"),
            promotionCampaignId=document.get("promotionCampaignId"),
            checkoutType=document.get("checkoutType"),
            checkoutTypeCheckedAt=document.get("checkoutTypeCheckedAt"),
            registrationCountry=document.get("registrationCountry"),
        )

    @staticmethod
    def _email_record(document: dict[str, Any]) -> EmailRecord:
        return EmailRecord(
            id=str(document["_id"]),
            email=document["email"],
            accessUrl=direct_mailbox_access_url(
                document["accessUrl"], document["email"]
            ),
            importedAt=document["importedAt"],
            sourceType=(
                "mailcom_alias"
                if document.get("sourceType") == "mailcom_alias"
                else "manual"
            ),
            parentEmail=(
                str(document.get("parentEmail") or "") or None
            ),
        )

    async def _exclude_registered_accounts(
        self,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        registered = await self._guard(
            self.accounts.distinct(
                "emailNormalized",
                {"emailNormalized": {"$type": "string"}},
            )
        )
        normalized = [value for value in registered if isinstance(value, str) and value]
        if not normalized:
            return query
        return {
            "$and": [
                query,
                {"emailNormalized": {"$nin": normalized}},
            ]
        }

    @staticmethod
    def _proxy_record(document: dict[str, Any]) -> ProxyRecord:
        return ProxyRecord(
            id=str(document["_id"]),
            host=document["host"],
            port=document["port"],
            username=document["username"],
            password=document["password"],
            enabled=document["enabled"],
            status=document["status"],
            latencyMs=document.get("latencyMs"),
            lastCheckedAt=document.get("lastCheckedAt"),
            country=(
                normalize_country_code(document.get("country"))
                if normalize_country_code(document.get("country")) != "ZZ"
                else infer_proxy_country(
                    str(document.get("username") or ""),
                    str(document.get("host") or ""),
                )
            ),
            group=normalize_proxy_group(document.get("group")),
            scheme=(
                str(document.get("scheme") or "http").lower()
                if str(document.get("scheme") or "http").lower()
                in SUPPORTED_PROXY_SCHEMES
                else "http"
            ),
        )


class ResourceService:
    def __init__(self, store: MongoResourceStore) -> None:
        self.store = store

    async def import_emails(self, raw_text: str) -> ImportResult:
        total = imported = duplicates = errors = 0
        seen: set[str] = set()
        for raw_line in raw_text.lstrip("\ufeff").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            total += 1
            parts = line.split("----", 1)
            if len(parts) != 2:
                errors += 1
                continue
            email, credential = (part.strip() for part in parts)
            key = normalize_email(email)
            is_url = valid_url(credential)
            is_mailcom_password = (
                bool(credential)
                and "://" not in credential
                and len(credential) <= 1024
            )
            if (
                not EMAIL_PATTERN.fullmatch(email)
                or not (is_url or is_mailcom_password)
            ):
                errors += 1
                continue
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if is_url:
                inserted = await self.store.upsert_email(key, credential)
            else:
                inserted = await self.store.upsert_email(
                    key,
                    MAILCOM_WEBMAIL_URL,
                    mailbox_kind="mailcom_imap",
                    mailbox_password=credential,
                )
            if inserted:
                imported += 1
            else:
                duplicates += 1
        return ImportResult(
            total=total,
            imported=imported,
            duplicateCount=duplicates,
            errorCount=errors,
        )

    async def sync_mailcom_aliases(self, items: list[dict[str, Any]]) -> ImportResult:
        total = imported = duplicates = errors = 0
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("isAlias") is not True:
                continue
            total += 1
            email = str(item.get("email") or "").strip()
            parent_email = str(item.get("accountEmail") or "").strip()
            access_url = str(item.get("accessUrl") or "").strip()
            key = normalize_email(email)
            try:
                parsed = urlsplit(access_url)
                valid_local_url = (
                    parsed.scheme.casefold() == "http"
                    and (parsed.hostname or "").casefold() in {"127.0.0.1", "localhost"}
                    and (parsed.port or 80) == 3211
                    and parsed.path.rstrip("/").casefold() == "/api/mail/latest"
                )
            except ValueError:
                valid_local_url = False
            if (
                not EMAIL_PATTERN.fullmatch(email)
                or not EMAIL_PATTERN.fullmatch(parent_email)
                or not valid_local_url
            ):
                errors += 1
                continue
            expected_url = (
                "http://127.0.0.1:3211/api/mail/latest?"
                f"{urlencode({'email': email})}"
            )
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            inserted = await self.store.upsert_email(
                key,
                expected_url,
                source_type="mailcom_alias",
                parent_email=parent_email,
            )
            if inserted:
                imported += 1
            else:
                duplicates += 1
        return ImportResult(
            total=total,
            imported=imported,
            duplicateCount=duplicates,
            errorCount=errors,
        )

    async def import_proxies(
        self,
        raw_text: str,
        country: str | None = None,
        group: str | None = None,
    ) -> ImportResult:
        total = imported = duplicates = errors = 0
        seen: set[tuple[str, int, str, str, str]] = set()
        for line in re.split(r"\s+", raw_text.lstrip("\ufeff").strip()):
            if not line:
                continue
            total += 1
            scheme = "http"
            if "://" in line:
                try:
                    parsed = urlsplit(line)
                    scheme = parsed.scheme.lower()
                    host = str(parsed.hostname or "").strip()
                    port = int(parsed.port or 0)
                    username = unquote(parsed.username or "").strip()
                    password = unquote(parsed.password or "").strip()
                except (TypeError, ValueError):
                    errors += 1
                    continue
            else:
                parts = line.split(":", 3)
                if len(parts) != 4:
                    errors += 1
                    continue
                host, port_text, username, password = (part.strip() for part in parts)
                try:
                    port = int(port_text)
                except ValueError:
                    errors += 1
                    continue
            if not host or not username or not password or port < 1 or port > 65535:
                errors += 1
                continue
            if scheme not in SUPPORTED_PROXY_SCHEMES:
                errors += 1
                continue
            key = (host.lower(), port, username, password, scheme)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if await self.store.upsert_proxy(
                key[0], key[1], key[2], key[3], scheme=key[4], country=country, group=group
            ):
                imported += 1
            else:
                duplicates += 1
        return ImportResult(
            total=total,
            imported=imported,
            duplicateCount=duplicates,
            errorCount=errors,
        )

    async def create_account(self, incoming: AccountCreate) -> AccountRecord:
        if not EMAIL_PATTERN.fullmatch(incoming.email) or not valid_url(incoming.emailAccessUrl):
            raise ValueError("邮箱或接码地址格式无效")
        return await self.store.create_account(incoming)

    async def export_accounts(self, incoming: AccountExportInput) -> TextExport:
        ids = None if incoming.scope == "all" else incoming.ids
        if incoming.format == "access-tokens":
            documents = await self.store.access_tokens_for_export(ids)
            now = utc_now()
            tokens: list[str] = []
            missing = 0
            expired = 0
            for document in documents:
                token = document.get("accessToken")
                if (
                    not document.get("accessTokenConfigured")
                    or not isinstance(token, str)
                    or not token
                ):
                    missing += 1
                    continue
                expires_at = document.get("accessTokenExpiresAt")
                if (
                    not isinstance(expires_at, datetime)
                    or expires_at.tzinfo is None
                    or expires_at.astimezone(timezone.utc) <= now
                ):
                    expired += 1
                    continue
                tokens.append(token)
            return TextExport(
                content="\n".join(tokens),
                filename=(
                    f"accounts-{len(tokens)}-access-tokens-"
                    f"{export_timestamp()}.txt"
                ),
                count=len(tokens),
                format=incoming.format,
                skippedMissingCount=missing,
                skippedExpiredCount=expired,
            )
        records = await self.store.accounts_for_export(ids)
        if incoming.format == "credentials":
            content = "\n".join(
                f"{item.email}----{item.chatgptPassword}----{item.totpSecret}"
                for item in records
            )
            suffix = "credentials"
        else:
            content = "\n".join(
                f"{item.email}----{item.emailAccessUrl}" for item in records
            )
            suffix = "mail-links"
        return TextExport(
            content=content,
            filename=f"accounts-{len(records)}-{suffix}-{export_timestamp()}.txt",
            count=len(records),
            format=incoming.format,
        )

    async def export_emails(self, incoming: EmailExportInput) -> TextExport:
        ids = None if incoming.scope == "all" else incoming.ids
        records = await self.store.emails_for_export(ids)
        content = "\n".join(f"{item.email}----{item.accessUrl}" for item in records)
        return TextExport(
            content=content,
            filename=f"emails-{len(records)}-mail-links-{export_timestamp()}.txt",
            count=len(records),
        )
