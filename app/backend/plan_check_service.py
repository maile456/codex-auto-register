from __future__ import annotations

import asyncio
from urllib.parse import quote
from uuid import uuid4

from .chatgpt_plan import PlanCheckError, check_account_plan_curl
from .oai_iprocket_chain_bridge import ensure_background_server
from .oai_payment_extractor.transport import chain_bridge_proxy_url
from .probe_store import MongoProbeStore, ProxyLease
from .resource_models import AccountPlanCheckItem, AccountPlanCheckResult
from .resource_service import MongoResourceStore


CHAIN_BRIDGE_PROXY_SUFFIXES = (
    "ipipbright.net",
    "1024proxy.io",
    "iprocket.io",
    "iprocket.pro",
    "iproyal.net",
    "iproyal.com",
)


def requires_chain_bridge(host: str) -> bool:
    lowered = str(host or "").strip().lower().rstrip(".")
    return any(
        lowered == suffix or lowered.endswith("." + suffix)
        for suffix in CHAIN_BRIDGE_PROXY_SUFFIXES
    )


def proxy_url(lease: ProxyLease) -> str:
    if lease.username and requires_chain_bridge(lease.host):
        ensure_background_server()
        return chain_bridge_proxy_url(
            lease.host,
            lease.port,
            lease.username,
            lease.password,
            "http",
        )
    authority = f"{lease.host}:{lease.port}"
    if lease.username or lease.password:
        credentials = f"{quote(lease.username, safe='')}:{quote(lease.password, safe='')}"
        authority = f"{credentials}@{authority}"
    return f"http://{authority}"


class AccountPlanCheckService:
    def __init__(
        self,
        resources: MongoResourceStore,
        proxies: MongoProbeStore,
        *,
        max_concurrency: int = 3,
    ) -> None:
        self.resources = resources
        self.proxies = proxies
        self.max_concurrency = max(1, max_concurrency)

    async def check_accounts(self, ids: list[str]) -> AccountPlanCheckResult:
        unique_ids = list(dict.fromkeys(str(value) for value in ids))
        available = await self.proxies.count_eligible_proxies()
        semaphore = asyncio.Semaphore(
            max(1, min(self.max_concurrency, available))
        )

        async def check_one(account_id: str) -> AccountPlanCheckItem:
            async with semaphore:
                return await self._check_one(account_id)

        items = await asyncio.gather(*(check_one(account_id) for account_id in unique_ids))
        return self._result(list(items))

    async def _check_one(self, account_id: str) -> AccountPlanCheckItem:
        source = await self.resources.claim_account_plan_check(account_id)
        if source is None:
            return AccountPlanCheckItem(
                id=account_id,
                status="skipped",
                errorCode="account_missing_token_or_busy",
            )

        owner = f"plan:{uuid4()}"
        lease = await self.proxies.acquire_proxy(owner, lease_seconds=120)
        if lease is None:
            error = PlanCheckError("no_eligible_proxy")
            await self.resources.store_account_plan_failure(account_id, error)
            return AccountPlanCheckItem(
                id=account_id, status="failed", errorCode=error.code
            )

        try:
            token = str(source.get("accessToken") or "")
            try:
                result = await asyncio.to_thread(
                    check_account_plan_curl,
                    token,
                    proxy_url=proxy_url(lease),
                    timezone_offset_min="-480",
                )
            except PlanCheckError as exc:
                await self.resources.store_account_plan_failure(account_id, exc)
                if exc.http_status is not None:
                    await self.proxies.record_proxy_success(lease.id, exc.elapsed_ms)
                return AccountPlanCheckItem(
                    id=account_id, status="failed", errorCode=exc.code
                )
            except Exception:
                exc = PlanCheckError("plan_request_failed", retryable=True)
                await self.resources.store_account_plan_failure(account_id, exc)
                return AccountPlanCheckItem(
                    id=account_id, status="failed", errorCode=exc.code
                )
            try:
                await self.resources.store_account_plan_result(account_id, result)
            except Exception:
                error = PlanCheckError("plan_result_store_failed")
                try:
                    await self.resources.store_account_plan_failure(
                        account_id, error
                    )
                except Exception:
                    pass
                return AccountPlanCheckItem(
                    id=account_id,
                    status="failed",
                    errorCode=error.code,
                )
            await self.proxies.record_proxy_success(lease.id, result.elapsed_ms)
            return AccountPlanCheckItem(id=account_id, status="success")
        finally:
            await self.proxies.release_proxy(lease.id, owner)

    @staticmethod
    def _result(items: list[AccountPlanCheckItem]) -> AccountPlanCheckResult:
        return AccountPlanCheckResult(
            requested=len(items),
            succeeded=sum(item.status == "success" for item in items),
            failed=sum(item.status == "failed" for item in items),
            skipped=sum(item.status == "skipped" for item in items),
            items=items,
        )
