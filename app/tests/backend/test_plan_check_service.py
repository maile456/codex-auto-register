import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

from backend.chatgpt_plan import AccountPlanResult, PlanCheckError
from backend.plan_check_service import AccountPlanCheckService, proxy_url
from backend.probe_store import ProxyLease


class FakeResources:
    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = documents
        self.results: list[tuple[str, AccountPlanResult]] = []
        self.failures: list[tuple[str, str]] = []

    async def claim_account_plan_check(self, account_id: str):
        return self.documents.get(account_id)

    async def store_account_plan_result(
        self,
        account_id: str,
        result: AccountPlanResult,
    ) -> None:
        self.results.append((account_id, result))

    async def store_account_plan_failure(
        self,
        account_id: str,
        error: PlanCheckError,
    ) -> None:
        self.failures.append((account_id, error.code))


class FakeProxies:
    def __init__(self, lease: ProxyLease | None) -> None:
        self.lease = lease
        self.acquired: list[str] = []
        self.released: list[tuple[str, str]] = []
        self.successes: list[tuple[str, int]] = []

    async def count_eligible_proxies(self) -> int:
        return int(self.lease is not None)

    async def acquire_proxy(self, owner: str, **_kwargs: object):
        self.acquired.append(owner)
        return self.lease

    async def release_proxy(self, proxy_id: str, owner: str) -> None:
        self.released.append((proxy_id, owner))

    async def record_proxy_success(self, proxy_id: str, elapsed_ms: int) -> None:
        self.successes.append((proxy_id, elapsed_ms))


def plan_result() -> AccountPlanResult:
    return AccountPlanResult(
        checked_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        account_id="chatgpt-account",
        current_plan_type="free",
        subscription_plan="chatgptfreeplan",
        has_active_subscription=False,
        expires_at=None,
        renews_at=None,
        plus_trial_eligible=True,
        plus_trial_campaign_id="trial",
        elapsed_ms=321,
    )


def test_ipbright_plan_proxy_uses_existing_chain_bridge(monkeypatch) -> None:
    starts: list[bool] = []
    monkeypatch.setattr(
        "backend.plan_check_service.ensure_background_server",
        lambda: starts.append(True),
    )

    value = proxy_url(
        ProxyLease("proxy", "sp.ipipbright.net", 1000, "user", "secret")
    )
    parsed = urlsplit(value)

    assert starts == [True]
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == 18796
    assert (parsed.username or "").startswith("iprb_")


def test_regular_plan_proxy_keeps_direct_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.plan_check_service.ensure_background_server",
        lambda: (_ for _ in ()).throw(AssertionError("bridge must not start")),
    )

    assert proxy_url(
        ProxyLease("proxy", "proxy.test", 1000, "user", "secret")
    ) == "http://user:secret@proxy.test:1000"


def test_manual_plan_check_persists_success_and_releases_proxy(monkeypatch) -> None:
    resources = FakeResources({"account": {"accessToken": "TEST_AT"}})
    proxies = FakeProxies(ProxyLease("proxy", "proxy.test", 10000, "user", "secret"))
    monkeypatch.setattr(
        "backend.plan_check_service.check_account_plan_curl",
        lambda *_args, **_kwargs: plan_result(),
    )

    result = asyncio.run(
        AccountPlanCheckService(resources, proxies).check_accounts(["account"])
    )

    assert result.succeeded == 1
    assert resources.results[0][0] == "account"
    assert proxies.successes == [("proxy", 321)]
    assert proxies.released == [("proxy", proxies.acquired[0])]


def test_manual_plan_check_401_persists_failure_and_releases_proxy(monkeypatch) -> None:
    resources = FakeResources({"account": {"accessToken": "TEST_AT"}})
    proxies = FakeProxies(ProxyLease("proxy", "proxy.test", 10000, "", ""))

    def fail(*_args, **_kwargs):
        raise PlanCheckError(
            "access_token_unauthorized", http_status=401, elapsed_ms=111
        )

    monkeypatch.setattr("backend.plan_check_service.check_account_plan_curl", fail)
    result = asyncio.run(
        AccountPlanCheckService(resources, proxies).check_accounts(["account"])
    )

    assert result.failed == 1
    assert resources.failures == [("account", "access_token_unauthorized")]
    assert proxies.successes == [("proxy", 111)]
    assert proxies.released == [("proxy", proxies.acquired[0])]


def test_manual_plan_check_unexpected_error_is_stable_and_releases_proxy(
    monkeypatch,
) -> None:
    resources = FakeResources({"account": {"accessToken": "TEST_AT"}})
    proxies = FakeProxies(ProxyLease("proxy", "proxy.test", 10000, "", ""))

    def fail(*_args, **_kwargs):
        raise RuntimeError("PRIVATE_RESPONSE_OR_CREDENTIAL")

    monkeypatch.setattr("backend.plan_check_service.check_account_plan_curl", fail)
    result = asyncio.run(
        AccountPlanCheckService(resources, proxies).check_accounts(["account"])
    )

    assert result.items[0].errorCode == "plan_request_failed"
    assert resources.failures == [("account", "plan_request_failed")]
    assert proxies.released == [("proxy", proxies.acquired[0])]
    assert "PRIVATE_RESPONSE_OR_CREDENTIAL" not in result.model_dump_json()


def test_manual_plan_check_no_proxy_and_busy_accounts_are_reported() -> None:
    resources = FakeResources({"account": {"accessToken": "TEST_AT"}})
    proxies = FakeProxies(None)

    result = asyncio.run(
        AccountPlanCheckService(resources, proxies).check_accounts(
            ["account", "missing", "account"]
        )
    )

    assert result.requested == 2
    assert result.failed == 1
    assert result.skipped == 1
    assert resources.failures == [("account", "no_eligible_proxy")]
    assert proxies.released == []
