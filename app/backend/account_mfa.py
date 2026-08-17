from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from .browser_automation import (
    CdpBrowserAutomation,
    EmailStepError,
    PasswordStepError,
    TotpEnrollmentError,
)
from .browser_probe import DEFAULT_ARTIFACT_DIR
from .browser_probe import generate_account_password
from .mailbox_client import MailboxClient, MailboxClientError, mailbox_source_for_document
from .mongo_manager import MongoManager
from .probe_store import MongoProbeStore
from .resource_service import MongoResourceStore
from .roxy_client import RoxyApiError, RoxyClient, RoxyOpenResult
from .settings_store import SettingsStore


@dataclass(frozen=True, slots=True)
class AccountMfaResult:
    account_id: str
    status: str
    code: str


class PaidAccountMfaBackfill:
    def __init__(
        self,
        *,
        mongo: MongoManager | None = None,
        settings_store: SettingsStore | None = None,
        mailbox: MailboxClient | None = None,
        artifact_root: Path = DEFAULT_ARTIFACT_DIR / "paid-2fa-backfill",
    ) -> None:
        self.mongo = mongo or MongoManager()
        self.resources = MongoResourceStore(self.mongo)
        self.probes = MongoProbeStore(self.mongo)
        self.settings_store = settings_store or SettingsStore()
        self.mailbox = mailbox or MailboxClient()
        self.artifact_root = Path(artifact_root)

    async def _open_browser_with_recovery(
        self,
        roxy: RoxyClient,
        workspace_id: int,
        dir_id: str,
        *,
        headless: bool,
        timeout_seconds: float = 45.0,
    ) -> RoxyOpenResult:
        try:
            return await roxy.open_browser(
                workspace_id,
                dir_id,
                headless=headless,
            )
        except RoxyApiError as exc:
            initial_error = exc
            if exc.is_auth_failure or not exc.retryable:
                raise

        started = monotonic()
        deadline = started + max(0.0, timeout_seconds)
        while monotonic() < deadline:
            remaining = deadline - monotonic()
            request_timeout = max(0.001, min(3.0, remaining))
            try:
                connections = await asyncio.wait_for(
                    roxy.connection_info(
                        [dir_id],
                        timeout_seconds=request_timeout,
                    ),
                    timeout=request_timeout,
                )
            except (TimeoutError, RoxyApiError):
                connections = []
            connection = next(
                (item for item in connections if item.dir_id == dir_id),
                None,
            )
            if connection is not None:
                return RoxyOpenResult(
                    ws=connection.ws,
                    http=connection.http,
                    pid=connection.pid,
                    recovered=True,
                    recovery_elapsed_ms=max(
                        0,
                        int((monotonic() - started) * 1000),
                    ),
                )
            await asyncio.sleep(min(0.5, max(0.0, deadline - monotonic())))
        raise initial_error

    async def candidates(self, limit: int = 0) -> list[dict[str, Any]]:
        paid = await self.mongo.database["account_pipeline"].find(
            {"stage": "paid"},
            {"accountId": 1},
        ).to_list(length=None)
        account_ids = [str(item.get("accountId") or "") for item in paid]
        missing_totp = {"totpSecret": {"$in": ["", None]}}
        missing_password = {"chatgptPassword": {"$in": ["", None]}}
        query = {
            "$or": [
                {
                    "_id": {"$in": account_ids},
                    "$or": [missing_totp, missing_password],
                },
                {
                    "totpStatus": "enabled",
                    "chatgptPassword": {"$in": ["", None]},
                },
            ],
            "email": {"$type": "string", "$ne": ""},
            "emailAccessUrl": {"$type": "string", "$ne": ""},
        }
        cursor = self.resources.accounts.find(query).sort("createdAt", 1)
        if limit > 0:
            cursor = cursor.limit(limit)
        return await cursor.to_list(length=limit or None)

    async def run(self, *, limit: int = 0) -> list[AccountMfaResult]:
        await self.mongo.start()
        if not self.mongo.online:
            raise RuntimeError("MongoDB 当前不可用")
        try:
            await self.probes.ensure_indexes()
            documents = await self.candidates(limit)
            results: list[AccountMfaResult] = []
            for account in documents:
                results.append(await self._run_one(account))
            return results
        finally:
            await self.mongo.stop()

    async def _run_one(self, account: dict[str, Any]) -> AccountMfaResult:
        account_id = str(account["_id"])
        started_at = datetime.now(timezone.utc)
        await self.resources.accounts.update_one(
            {"_id": account_id},
            {
                "$set": {
                    "securityBackfillStatus": "running",
                    "securityBackfillStartedAt": started_at,
                    "securityBackfillError": None,
                }
            },
        )
        try:
            await self._enable_one(account)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            message = getattr(exc, "message", str(exc))
            await self.resources.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "securityBackfillStatus": "failed",
                        "securityBackfillError": str(code)[:120],
                        "securityBackfillErrorMessage": str(message)[:300],
                        "securityBackfillFinishedAt": datetime.now(timezone.utc),
                    }
                },
            )
            return AccountMfaResult(account_id, "failed", str(code))
        await self.resources.accounts.update_one(
            {"_id": account_id},
            {
                "$set": {
                    "securityBackfillStatus": "success",
                    "securityBackfillError": None,
                    "securityBackfillFinishedAt": datetime.now(timezone.utc),
                }
            },
        )
        return AccountMfaResult(account_id, "success", "account_security_configured")

    async def _enable_one(self, account: dict[str, Any]) -> None:
        account_id = str(account["_id"])
        email = str(account["email"])
        source = {
            "email": email,
            "accessUrl": str(account["emailAccessUrl"]),
            "mailboxKind": account.get("mailboxKind"),
            "mailboxPassword": account.get("mailboxPassword"),
        }
        access_url = mailbox_source_for_document(source)
        local_totp_secret = str(account.get("totpSecret") or "")
        local_password = str(account.get("chatgptPassword") or "")
        settings = self.settings_store.load()
        api_key = settings.roxyApiKey
        owner = f"probe:mfa:{account_id}:{uuid4().hex[:8]}"
        workspace_id: int | None = None
        proxy_id: str | None = None
        dir_id: str | None = None
        lock_acquired = False
        workspace_acquired = False

        try:
            lock_acquired = await self.probes.acquire_probe_lock(
                owner,
                lease_seconds=600,
            )
            if not lock_acquired:
                raise RuntimeError("真实浏览器控制器正在运行")
            async with RoxyClient(settings.roxyApiPort, api_key) as roxy:
                await roxy.health()
                workspaces = await roxy.workspaces()
                if not workspaces:
                    raise RuntimeError("Roxy workspace 不存在")
                workspace_id = workspaces[0].id
                workspace_acquired = await self.probes.acquire_workspace(
                    workspace_id,
                    owner,
                    lease_seconds=600,
                )
                if not workspace_acquired:
                    raise RuntimeError("Roxy workspace 正在使用")
                proxy = await self.probes.acquire_proxy(
                    owner,
                    lease_seconds=600,
                    country=str(account.get("registrationCountry") or "") or None,
                )
                if proxy is None:
                    raise RuntimeError("没有可用代理")
                proxy_id = proxy.id
                dir_id = await roxy.create_browser(workspace_id, owner, proxy)
                opened = await self._open_browser_with_recovery(
                    roxy,
                    workspace_id,
                    dir_id,
                    headless=settings.headless,
                )
                screenshot = self.artifact_root / account_id / "latest.png"
                async with CdpBrowserAutomation(opened.ws, screenshot) as automation:
                    baseline = await self.mailbox.get_snapshot(access_url, email)
                    login = await automation.submit_email_and_continue(email)
                    next_step = login.next_step
                    verification_requested_at = login.submitted_at_utc
                    if next_step == "password":
                        if local_password:
                            password_result = (
                                await automation.submit_existing_password_and_continue(
                                    local_password
                                )
                            )
                        else:
                            password_result = (
                                await automation.switch_password_page_to_email_code()
                            )
                        next_step = password_result.next_step
                        verification_requested_at = password_result.submitted_at_utc
                    if next_step == "totp":
                        if not local_totp_secret:
                            raise TotpEnrollmentError(
                                "existing_login",
                                "existing_totp_secret_unknown",
                                "账号已经要求 TOTP，但本地没有对应 Secret",
                            )
                        await automation.submit_totp_challenge(local_totp_secret)
                        await automation.complete_profile_if_needed()
                        next_step = "account_home"
                    if next_step == "verification":
                        if await automation.verification_factor() == "totp":
                            raise TotpEnrollmentError(
                                "existing_login",
                                "existing_totp_secret_unknown",
                                "账号已经要求 TOTP，但本地没有对应 Secret",
                            )
                        code = await self.mailbox.wait_for_new_code(
                            access_url,
                            email,
                            verification_requested_at,
                            baseline=baseline,
                        )
                        await automation.submit_verification_code_and_continue(
                            code.verification_code
                        )
                        await automation.complete_profile_if_needed()
                    elif next_step not in {"account_home", "transitioned"}:
                        raise EmailStepError(
                            "existing_login_next_step_unknown",
                            "已有账号登录后进入未知页面",
                        )

                    if not local_totp_secret:
                        reauth_baseline = await self.mailbox.get_snapshot(access_url, email)
                        challenge = await automation.begin_totp_enrollment(email)
                        reauth_code = await self.mailbox.wait_for_new_code(
                            access_url,
                            email,
                            challenge.requested_at_utc,
                            baseline=reauth_baseline,
                        )
                        enrolled = await automation.complete_totp_enrollment(
                            reauth_code.verification_code
                        )
                        await self.resources.store_account_totp(
                            account_id,
                            enrolled.secret,
                            enrolled.access_token,
                            enrolled.access_token_expires_at_utc,
                            enrolled.activated_at_utc,
                        )
                        local_totp_secret = enrolled.secret

                    if not local_password:
                        password_baseline = await self.mailbox.get_snapshot(
                            access_url, email
                        )

                        async def password_email_code(requested_at: datetime) -> str:
                            result = await self.mailbox.wait_for_new_code(
                                access_url,
                                email,
                                requested_at,
                                baseline=password_baseline,
                            )
                            return result.verification_code

                        generated_password = generate_account_password()
                        configured = await automation.add_password_in_settings(
                            generated_password,
                            local_totp_secret,
                            password_email_code,
                        )
                        await self.resources.store_account_password(
                            account_id,
                            generated_password,
                            configured.configured_at_utc,
                        )
        finally:
            if dir_id is not None and workspace_id is not None:
                with suppress(RoxyApiError):
                    async with RoxyClient(
                        settings.roxyApiPort,
                        api_key,
                    ) as cleanup_roxy:
                        with suppress(RoxyApiError):
                            await cleanup_roxy.close_browser(dir_id)
                        await cleanup_roxy.delete_browser(workspace_id, dir_id)
            if proxy_id is not None:
                with suppress(Exception):
                    await self.probes.release_proxy(proxy_id, owner)
            if workspace_acquired and workspace_id is not None:
                with suppress(Exception):
                    await self.probes.release_workspace(workspace_id, owner)
            if lock_acquired:
                with suppress(Exception):
                    await self.probes.release_probe_lock(owner)


async def _wait_until_registration_idle(poll_seconds: float = 15.0) -> None:
    mongo = MongoManager()
    await mongo.start()
    if not mongo.online:
        raise RuntimeError("MongoDB 当前不可用")
    try:
        while True:
            active = await mongo.database["runs"].count_documents(
                {"status": {"$in": ["queued", "running", "waiting_for_database"]}}
            )
            if not active:
                return
            await asyncio.sleep(poll_seconds)
    finally:
        await mongo.stop()


async def _main(limit: int, *, wait_until_idle: bool, canary_then_all: bool) -> int:
    if wait_until_idle:
        await _wait_until_registration_idle()
    service = PaidAccountMfaBackfill()
    if canary_then_all:
        canary = await service.run(limit=1)
        if canary and canary[0].status == "success":
            results = canary + await service.run(limit=0)
        else:
            results = canary
    else:
        results = await service.run(limit=limit)
    summary = {
        "requested": len(results),
        "succeeded": sum(item.status == "success" for item in results),
        "failed": sum(item.status == "failed" for item in results),
        "results": [
            {"accountId": item.account_id, "status": item.status, "code": item.code}
            for item in results
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="为缺少密码或 2FA 的账号补跑安全设置")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--wait-until-idle", action="store_true")
    parser.add_argument("--canary-then-all", action="store_true")
    arguments = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            _main(
                max(0, arguments.limit),
                wait_until_idle=arguments.wait_until_idle,
                canary_then_all=arguments.canary_then_all,
            )
        )
    )
