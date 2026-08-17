from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .alias_creator import AliasCreationError, MailComAliasCreator
from .crypto import DpapiCredentialCipher
from .imap_client import ImapMailboxService, MailboxError
from .server_sync import ServerSyncError, ServerSyncService
from .storage import AccountStore


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DEFAULT_DB_PATH = ROOT / "data" / "mailcom.db"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportPayload(ApiModel):
    rawText: str = Field(min_length=1, max_length=2 * 1024 * 1024)


class TestAllPayload(ApiModel):
    ids: list[str] = Field(default_factory=list, max_length=1000)


class AliasImportPayload(ApiModel):
    rawText: str = Field(min_length=1, max_length=1024 * 1024)


class AliasAutoCreatePayload(ApiModel):
    targetTotal: int = Field(default=10, ge=1, le=10)
    concurrency: int = Field(default=2, ge=1, le=4)


class ServerSyncPayload(ApiModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr


class ServerSyncResponse(ApiModel):
    ok: bool
    accounts: int
    aliases: int
    hostKeySha256: str


def _recipient_matches(value: str, email: str) -> bool:
    addresses = {
        address.strip().casefold()
        for _, address in getaddresses([value.replace("|", ",")])
        if address.strip()
    }
    return email.strip().casefold() in addresses


def _mailbox_http_error(exc: MailboxError) -> HTTPException:
    return HTTPException(
        status_code=502 if exc.retryable else 422,
        detail={"code": exc.code, "message": exc.message},
    )


def _message_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payment_confirmation(message: Any, requested_email: str, since: datetime) -> tuple[bool, str | None]:
    if not _recipient_matches(message.recipients, requested_email):
        return False, None
    received_at = _message_datetime(message.received_at)
    normalized_since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    # Allow a small clock skew because the payment API can report completion just after mail delivery.
    if received_at is None or received_at < normalized_since.astimezone(timezone.utc) - timedelta(minutes=2):
        return False, None
    text = " ".join(
        str(value or "")
        for value in (message.subject, message.sender, message.recipients, message.preview)
    )
    lowered = text.casefold()
    plus_marker = "chatgpt plus" in lowered or "plus subscription" in lowered
    provider_marker = "openai" in lowered or "chatgpt" in lowered
    success_marker = any(
        marker in lowered
        for marker in (
            "successfully subscribed",
            "subscription is active",
            "subscription confirmed",
            "正常に登録",
            "订阅成功",
            "訂閱成功",
            "erfolgreich abonniert",
            "abonnement confirmé",
            "başarıyla abone",
        )
    )
    order_match = re.search(r"\bsub_[a-z0-9]+\b", text, flags=re.IGNORECASE)
    return bool(plus_marker and provider_marker and success_marker and order_match), (
        order_match.group(0) if order_match else None
    )


def create_app(
    *,
    db_path: Path | None = None,
    cipher: Any | None = None,
    imap_service: ImapMailboxService | None = None,
    alias_creator: Any | None = None,
    server_sync_service: Any | None = None,
) -> FastAPI:
    application = FastAPI(
        title="MailCom Manager API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )
    store = AccountStore(
        db_path or Path(os.getenv("MAILCOM_MANAGER_DB", str(DEFAULT_DB_PATH))),
        cipher or DpapiCredentialCipher(),
    )
    mail = imap_service or ImapMailboxService()
    application.state.store = store
    application.state.mail = mail
    application.state.alias_creator = alias_creator or MailComAliasCreator()
    application.state.server_sync = server_sync_service or ServerSyncService()
    application.state.test_semaphore = asyncio.Semaphore(3)
    application.state.imap_semaphore = asyncio.Semaphore(3)
    # A single browser flow per account is still serialized by the bulk worker
    # assignment; this outer limit prevents an individual endpoint from
    # launching an unbounded number of Chromium sessions.
    application.state.alias_semaphore = asyncio.Semaphore(4)
    application.state.alias_bulk_lock = asyncio.Lock()
    application.state.alias_bulk_job = None
    application.state.alias_bulk_task = None

    @application.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "mailcom-manager",
            "storage": "sqlite-dpapi",
            "imapHost": getattr(mail, "host", "imap.mail.com"),
            "imapPort": getattr(mail, "port", 993),
            "imapRoute": mail.route,
        }

    @application.get("/api/stats")
    async def stats() -> dict[str, int]:
        return await asyncio.to_thread(store.stats)

    @application.post("/api/server-sync", response_model=ServerSyncResponse)
    async def server_sync(payload: ServerSyncPayload) -> ServerSyncResponse:
        snapshot = await asyncio.to_thread(store.sync_snapshot)
        try:
            result = await asyncio.to_thread(
                application.state.server_sync.push,
                snapshot,
                host=payload.host,
                port=payload.port,
                username=payload.username,
                password=payload.password.get_secret_value(),
            )
        except ServerSyncError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        return ServerSyncResponse(
            ok=True,
            accounts=result.accounts,
            aliases=result.aliases,
            hostKeySha256=result.host_key_sha256,
        )

    @application.post("/api/accounts/import")
    async def import_accounts(payload: ImportPayload) -> dict[str, Any]:
        total = imported = duplicates = 0
        errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line_number, source in enumerate(
            payload.rawText.lstrip("\ufeff").splitlines(), start=1
        ):
            value = source.strip()
            if not value:
                continue
            total += 1
            parts = value.split("----", 1)
            if len(parts) != 2:
                errors.append({"line": line_number, "message": "缺少 ---- 分隔符"})
                continue
            email, password = (part.strip() for part in parts)
            normalized = email.casefold()
            if not EMAIL_PATTERN.fullmatch(email):
                errors.append({"line": line_number, "message": "邮箱格式无效"})
                continue
            if not password or len(password) > 1024:
                errors.append({"line": line_number, "message": "邮箱密码为空或过长"})
                continue
            if normalized in seen:
                duplicates += 1
                continue
            seen.add(normalized)
            inserted = await asyncio.to_thread(store.import_account, email, password)
            if inserted:
                imported += 1
            else:
                duplicates += 1
        return {
            "total": total,
            "imported": imported,
            "duplicateCount": duplicates,
            "errorCount": len(errors),
            "errors": errors[:100],
        }

    @application.get("/api/accounts")
    async def list_accounts(
        q: str = Query(default="", max_length=320),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, alias="pageSize", ge=10, le=100),
    ) -> dict[str, Any]:
        items, total = await asyncio.to_thread(
            store.list_accounts,
            query=q,
            page=page,
            page_size=page_size,
        )
        return {"items": items, "total": total, "page": page, "pageSize": page_size}

    @application.get("/api/accounts/{account_id}/aliases")
    async def list_aliases(account_id: str) -> dict[str, Any]:
        account = await asyncio.to_thread(store.get_account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        items = await asyncio.to_thread(store.list_aliases, account_id)
        return {
            "accountId": account_id,
            "accountEmail": account["email"],
            "items": items,
        }

    @application.post("/api/accounts/{account_id}/aliases/import")
    async def import_aliases(
        account_id: str,
        payload: AliasImportPayload,
    ) -> dict[str, Any]:
        if await asyncio.to_thread(store.get_account, account_id) is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        total = imported = duplicates = 0
        errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line_number, source in enumerate(
            payload.rawText.lstrip("\ufeff").splitlines(), start=1
        ):
            value = source.strip()
            if not value:
                continue
            total += 1
            parts = [part.strip() for part in value.split("----", 1)]
            email = parts[0]
            label = parts[1] if len(parts) == 2 else ""
            normalized = email.casefold()
            if not EMAIL_PATTERN.fullmatch(email):
                errors.append({"line": line_number, "message": "分裂邮箱格式无效"})
                continue
            if normalized in seen:
                duplicates += 1
                continue
            seen.add(normalized)
            result = await asyncio.to_thread(
                store.import_alias,
                account_id,
                email,
                label,
            )
            if result == "inserted":
                imported += 1
            else:
                duplicates += 1
        return {
            "total": total,
            "imported": imported,
            "duplicateCount": duplicates,
            "errorCount": len(errors),
            "errors": errors[:100],
        }

    async def create_aliases_for_account(
        account_id: str,
        target_total: int,
        slot_semaphore: asyncio.Semaphore | None = None,
    ) -> dict[str, Any]:
        account = await asyncio.to_thread(store.get_account, account_id)
        credentials = await asyncio.to_thread(store.get_credentials, account_id)
        if account is None or credentials is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        email, password = credentials

        async def persist(alias: str) -> None:
            await asyncio.to_thread(
                store.import_alias,
                account_id,
                alias,
                "mail.com 自动创建",
            )

        async def create_remote_aliases() -> Any:
            return await application.state.alias_creator.create_to_total(
                email,
                password,
                target_total,
                persist,
            )

        try:
            # The job-level slot limits this bulk run; the application-level
            # semaphore also accounts for manually triggered jobs.
            async with application.state.alias_semaphore:
                if slot_semaphore is None:
                    result = await create_remote_aliases()
                else:
                    async with slot_semaphore:
                        result = await create_remote_aliases()
        except AliasCreationError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": str(exc),
                    "message": "mail.com 真实别名创建失败，请稍后重试",
                },
            ) from exc

        imported_existing = 0
        for alias in result.remote_after:
            if alias.casefold() == email.casefold():
                continue
            stored = await asyncio.to_thread(
                store.import_alias,
                account_id,
                alias,
                "mail.com 真实别名",
            )
            if stored == "inserted":
                imported_existing += 1
        aliases = await asyncio.to_thread(store.list_aliases, account_id)
        return {
            "accountId": account_id,
            "remoteBefore": len(result.remote_before),
            "created": len(result.created),
            "importedExisting": imported_existing,
            "aliasCount": len(aliases),
            "targetTotal": target_total,
        }

    @application.post("/api/accounts/{account_id}/aliases/auto-create")
    async def auto_create_aliases(
        account_id: str,
        payload: AliasAutoCreatePayload,
    ) -> dict[str, Any]:
        return await create_aliases_for_account(account_id, payload.targetTotal)

    def public_bulk_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if job is None:
            return None
        return {
            **job,
            "errors": [dict(item) for item in job.get("errors", [])],
        }

    async def run_bulk_alias_job(
        job: dict[str, Any],
        accounts: list[dict[str, Any]],
        target_total: int,
        concurrency: int,
    ) -> None:
        job["status"] = "running"
        job["startedAt"] = datetime.now(timezone.utc).isoformat()
        concurrency = max(1, min(4, int(concurrency)))
        job["concurrency"] = concurrency
        job["activeAccounts"] = []
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for account in accounts:
            queue.put_nowait(account)
        slots = asyncio.Semaphore(concurrency)

        async def process_account(account: dict[str, Any]) -> None:
            email = str(account["email"])
            job["activeAccounts"].append(email)
            job["currentAccount"] = email
            try:
                result = await create_aliases_for_account(
                    str(account["id"]), target_total, slots
                )
                job["succeeded"] += 1
                job["created"] += int(result.get("created") or 0)
                job["importedExisting"] += int(result.get("importedExisting") or 0)
            except HTTPException as exc:
                job["failed"] += 1
                detail = exc.detail
                message = (
                    detail.get("message") if isinstance(detail, dict) else str(detail)
                )
                job["errors"].append({"email": email, "message": str(message)[:300]})
            except Exception as exc:
                job["failed"] += 1
                job["errors"].append(
                    {"email": email, "message": type(exc).__name__}
                )
            finally:
                job["completed"] += 1
                if email in job["activeAccounts"]:
                    job["activeAccounts"].remove(email)
                job["progress"] = round(
                    job["completed"] / max(1, job["total"]) * 100, 1
                )
                if len(job["errors"]) > 100:
                    job["errors"] = job["errors"][-100:]

        async def worker() -> None:
            while True:
                try:
                    account = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await process_account(account)
                finally:
                    queue.task_done()
                    # Keep a small gap per worker so the provider is not hit
                    # with a burst immediately after a browser closes.
                    await asyncio.sleep(1)

        workers = [
            asyncio.create_task(worker(), name=f"mailcom-alias-worker-{index}")
            for index in range(min(concurrency, len(accounts)))
        ]
        await asyncio.gather(*workers)
        job["currentAccount"] = None
        job["activeAccounts"] = []
        job["finishedAt"] = datetime.now(timezone.utc).isoformat()
        job["status"] = "completed_with_errors" if job["failed"] else "completed"

    @application.post("/api/aliases/auto-create-all", status_code=202)
    async def auto_create_all_aliases(
        payload: AliasAutoCreatePayload,
    ) -> dict[str, Any]:
        async with application.state.alias_bulk_lock:
            active_task = application.state.alias_bulk_task
            active_job = application.state.alias_bulk_job
            if active_task is not None and not active_task.done() and active_job:
                return public_bulk_job(active_job) or {}
            accounts = await asyncio.to_thread(
                store.underfilled_accounts,
                max(0, payload.targetTotal - 1),
            )
            now = datetime.now(timezone.utc).isoformat()
            job = {
                "id": uuid4().hex,
                "status": "queued",
                "targetTotal": payload.targetTotal,
                "concurrency": payload.concurrency,
                "total": len(accounts),
                "completed": 0,
                "succeeded": 0,
                "failed": 0,
                "created": 0,
                "importedExisting": 0,
                "progress": 0.0 if accounts else 100.0,
                "currentAccount": None,
                "activeAccounts": [],
                "createdAt": now,
                "startedAt": None,
                "finishedAt": now if not accounts else None,
                "errors": [],
            }
            application.state.alias_bulk_job = job
            if accounts:
                application.state.alias_bulk_task = asyncio.create_task(
                    run_bulk_alias_job(
                        job, accounts, payload.targetTotal, payload.concurrency
                    ),
                    name="mailcom-bulk-alias-create",
                )
            else:
                job["status"] = "completed"
                application.state.alias_bulk_task = None
            return public_bulk_job(job) or {}

    @application.get("/api/aliases/auto-create-all/active")
    async def active_bulk_alias_job() -> dict[str, Any]:
        return {"job": public_bulk_job(application.state.alias_bulk_job)}

    @application.get("/api/aliases/auto-create-all/{job_id}")
    async def bulk_alias_job(job_id: str) -> dict[str, Any]:
        job = application.state.alias_bulk_job
        if job is None or job.get("id") != job_id:
            raise HTTPException(status_code=404, detail="批量分裂任务不存在")
        return public_bulk_job(job) or {}

    @application.delete("/api/aliases/{alias_id}")
    async def delete_alias(alias_id: str) -> dict[str, Any]:
        deleted = await asyncio.to_thread(store.delete_alias, alias_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="分裂邮箱不存在")
        return {"deleted": True}

    async def run_test(account_id: str) -> dict[str, Any]:
        credentials = await asyncio.to_thread(store.get_credentials, account_id)
        if credentials is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        email, password = credentials
        async with application.state.test_semaphore:
            try:
                result = await asyncio.to_thread(mail.test, email, password)
            except MailboxError as exc:
                await asyncio.to_thread(
                    store.update_check,
                    account_id,
                    status="failed",
                    message_count=None,
                    error=exc.message,
                )
                return {
                    "id": account_id,
                    "email": email,
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message},
                }
        await asyncio.to_thread(
            store.update_check,
            account_id,
            status="online",
            message_count=int(result["messageCount"]),
            error=None,
        )
        return {
            "id": account_id,
            "email": email,
            "ok": True,
            "messageCount": int(result["messageCount"]),
        }

    @application.post("/api/accounts/{account_id}/test")
    async def test_account(account_id: str) -> dict[str, Any]:
        return await run_test(account_id)

    @application.post("/api/accounts/test-all")
    async def test_all(payload: TestAllPayload) -> dict[str, Any]:
        if payload.ids:
            ids = list(dict.fromkeys(payload.ids))
        else:
            items, _ = await asyncio.to_thread(
                store.list_accounts, page=1, page_size=100
            )
            ids = [str(item["id"]) for item in items]
        results = await asyncio.gather(*(run_test(value) for value in ids))
        return {
            "total": len(results),
            "online": sum(1 for item in results if item.get("ok")),
            "failed": sum(1 for item in results if not item.get("ok")),
            "items": results,
        }

    @application.get("/api/accounts/{account_id}/messages")
    async def list_messages(
        account_id: str,
        folder: str = Query(default="INBOX", pattern="^(INBOX|Spam|Junk)$"),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        credentials = await asyncio.to_thread(store.get_credentials, account_id)
        if credentials is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        email, password = credentials
        try:
            items = await asyncio.to_thread(
                mail.messages,
                email,
                password,
                folder=folder,
                limit=limit,
            )
        except MailboxError as exc:
            raise _mailbox_http_error(exc) from exc
        return {
            "accountId": account_id,
            "email": email,
            "folder": folder,
            "items": [item.public() for item in items],
        }

    @application.get("/api/accounts/{account_id}/latest-code")
    async def latest_code(account_id: str) -> dict[str, Any]:
        credentials = await asyncio.to_thread(store.get_credentials, account_id)
        if credentials is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        email, password = credentials
        try:
            for folder in ("INBOX", "Spam", "Junk"):
                messages = await asyncio.to_thread(
                    mail.messages,
                    email,
                    password,
                    folder=folder,
                    limit=20,
                )
                match = next(
                    (item for item in messages if item.verification_code is not None),
                    None,
                )
                if match is not None:
                    return {
                        "found": True,
                        "email": email,
                        "message": match.public(),
                    }
        except MailboxError as exc:
            raise _mailbox_http_error(exc) from exc
        return {"found": False, "email": email, "message": None}

    @application.get("/api/mail/latest")
    async def latest_mail_by_email(
        email: str = Query(min_length=3, max_length=320),
    ) -> dict[str, Any]:
        if not EMAIL_PATTERN.fullmatch(email.strip()):
            raise HTTPException(status_code=422, detail="邮箱格式无效")
        credentials = await asyncio.to_thread(store.get_mailbox_credentials, email)
        if credentials is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        requested_email, login_email, password, is_alias = credentials
        all_messages = []
        try:
            async with application.state.imap_semaphore:
                for folder in ("INBOX", "Spam", "Junk"):
                    all_messages.extend(
                        await asyncio.to_thread(
                            mail.messages,
                            login_email,
                            password,
                            folder=folder,
                            limit=20,
                        )
                    )
        except MailboxError as exc:
            raise _mailbox_http_error(exc) from exc

        if is_alias:
            all_messages = [
                item
                for item in all_messages
                if _recipient_matches(item.recipients, requested_email)
            ]

        def received_key(item: Any) -> str:
            return str(item.received_at or "")

        latest_message = max(all_messages, key=received_key, default=None)
        code_messages = [
            item for item in all_messages if item.verification_code is not None
        ]
        latest_verification = max(code_messages, key=received_key, default=None)
        selected = latest_verification or latest_message
        return {
            "ok": True,
            "email": requested_email,
            "isAlias": is_alias,
            "found": latest_verification is not None,
            "subject": selected.subject if selected is not None else "",
            "body": selected.preview if selected is not None else "",
            "receivedAt": selected.received_at if selected is not None else None,
            "folder": selected.folder if selected is not None else None,
            "verification_code": (
                latest_verification.verification_code
                if latest_verification is not None
                else None
            ),
            "message": selected.public() if selected is not None else None,
        }

    @application.get("/api/mail/payment-confirmation")
    async def payment_confirmation_by_email(
        email: str = Query(min_length=3, max_length=320),
        since: datetime = Query(),
    ) -> dict[str, Any]:
        if not EMAIL_PATTERN.fullmatch(email.strip()):
            raise HTTPException(status_code=422, detail="邮箱格式无效")
        credentials = await asyncio.to_thread(store.get_mailbox_credentials, email)
        if credentials is None:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        requested_email, login_email, password, is_alias = credentials
        all_messages = []
        try:
            async with application.state.imap_semaphore:
                for folder in ("INBOX", "Spam", "Junk"):
                    all_messages.extend(
                        await asyncio.to_thread(
                            mail.messages,
                            login_email,
                            password,
                            folder=folder,
                            limit=30,
                        )
                    )
        except MailboxError as exc:
            raise _mailbox_http_error(exc) from exc

        if is_alias:
            all_messages = [
                item for item in all_messages
                if _recipient_matches(item.recipients, requested_email)
            ]
        all_messages.sort(key=lambda item: str(item.received_at or ""), reverse=True)
        for item in all_messages:
            confirmed, order_id = _payment_confirmation(item, requested_email, since)
            if confirmed:
                return {
                    "ok": True,
                    "email": requested_email,
                    "status": "confirmed",
                    "found": True,
                    "subject": item.subject,
                    "receivedAt": item.received_at,
                    "orderId": order_id,
                    "folder": item.folder,
                    "message": item.public(),
                }
        return {
            "ok": True,
            "email": requested_email,
            "status": "waiting",
            "found": False,
            "subject": all_messages[0].subject if all_messages else "",
            "receivedAt": None,
            "orderId": None,
            "folder": None,
            "message": None,
        }

    @application.get(
        "/api/export/registration-lines",
        response_class=PlainTextResponse,
    )
    async def export_registration_lines(request: Request) -> PlainTextResponse:
        emails = await asyncio.to_thread(store.all_mailboxes)
        base = str(request.base_url).rstrip("/")
        lines = [
            f"{email}----{base}/api/mail/latest?{urlencode({'email': email})}"
            for email in emails
        ]
        return PlainTextResponse(
            "\n".join(lines),
            media_type="text/plain; charset=utf-8",
        )

    @application.get("/api/export/registration-items")
    async def export_registration_items(request: Request) -> dict[str, Any]:
        items = await asyncio.to_thread(store.registration_items)
        base = str(request.base_url).rstrip("/")
        return {
            "items": [
                {
                    **item,
                    "accessUrl": (
                        f"{base}/api/mail/latest?"
                        f"{urlencode({'email': item['email']})}"
                    ),
                }
                for item in items
            ]
        }

    @application.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: str) -> dict[str, Any]:
        deleted = await asyncio.to_thread(store.delete_account, account_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="邮箱不存在")
        return {"deleted": True}

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


app = create_app()
