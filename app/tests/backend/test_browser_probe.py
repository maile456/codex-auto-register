from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import backend.browser_probe as browser_probe_module
from backend.browser_automation import (
    AccessTokenExtractionError,
    AccessTokenExtractionResult,
    AutomationResult,
    EmailStepError,
    PasswordSubmitResult,
    PasswordSetupResult,
    ProfileCompletionResult,
    ProfileStepError,
    ProxyNavigationError,
    SecurityNavigationError,
    SecurityNavigationResult,
    TotpEnrollmentChallenge,
    TotpEnrollmentResult,
    VerificationStepError,
    VerificationSubmitResult,
)
from backend.browser_probe import ArtifactWriter, BrowserProbeRunner, ProbeFailure
from backend.chatgpt_plan import AccountPlanResult, PlanCheckError
from backend.errors import InsufficientEmailsError
from backend.mailbox_client import (
    MailboxClient,
    MailboxClientError,
    VerificationCodeResult,
    parse_mailbox_snapshot,
)
from backend.probe_store import ProxyLease
from backend.roxy_client import (
    RoxyApiError,
    RoxyConnectionInfo,
    RoxyOpenResult,
    RoxyWorkspace,
)
from backend.settings_store import StoredExecutionSettings


class FakeMongo:
    online = True

    async def start(self) -> None:
        self.online = True

    async def stop(self) -> None:
        self.online = False


class FakeStore:
    def __init__(self, proxies: list[ProxyLease], *, lock_available: bool = True) -> None:
        self.proxies = deque(proxies)
        self.lock_available = lock_available
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.successes: list[str] = []
        self.count_countries: list[str | None] = []
        self.acquire_countries: list[str | None] = []
        self.lock_released = False
        self.expired_probe_leases_cleared = 0

    async def ensure_indexes(self) -> None:
        return None

    async def clear_expired_probe_leases(self) -> int:
        self.expired_probe_leases_cleared += 1
        return 0

    async def acquire_probe_lock(self, _owner: str) -> bool:
        return self.lock_available

    async def heartbeat_probe_lock(self, _owner: str) -> bool:
        return True

    async def release_probe_lock(self, _owner: str) -> None:
        self.lock_released = True

    async def count_eligible_proxies(
        self, _country: str | None = None, _group: str | None = None
    ) -> int:
        self.count_countries.append(_country)
        return len(self.proxies)

    async def acquire_proxy(self, _owner: str, **_kwargs: object) -> ProxyLease | None:
        self.acquire_countries.append(_kwargs.get("country"))  # type: ignore[arg-type]
        if not self.proxies:
            return None
        proxy = self.proxies.popleft()
        self.acquired.append(proxy.id)
        return proxy

    async def release_proxy(self, proxy_id: str, _owner: str) -> None:
        self.released.append(proxy_id)

    async def record_proxy_success(self, proxy_id: str, _latency_ms: int) -> None:
        self.successes.append(proxy_id)


class FakeRoxy:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.opened_headless: list[bool] = []
        self.closed: list[str] = []
        self.deleted: list[str] = []
        self.workspace_timeouts: list[float | None] = []
        self.health_calls = 0
        self.connection_info_calls: list[tuple[list[str] | None, float | None]] = []

    async def __aenter__(self) -> "FakeRoxy":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def health(self) -> None:
        self.health_calls += 1
        return None

    async def workspaces(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> list[RoxyWorkspace]:
        self.workspace_timeouts.append(timeout_seconds)
        return [RoxyWorkspace(id=1, name="Local")]

    async def create_browser(self, _workspace: int, _probe: str, proxy: ProxyLease) -> str:
        dir_id = f"dir-{proxy.id}-{len(self.created)}"
        self.created.append(dir_id)
        return dir_id

    async def open_browser(self, _workspace: int, _dir_id: str, *, headless: bool) -> RoxyOpenResult:
        self.opened_headless.append(headless)
        return RoxyOpenResult(ws="ws://127.0.0.1:1/devtools/browser/test", http="", pid=1)

    async def close_browser(self, dir_id: str) -> None:
        self.closed.append(dir_id)

    async def delete_browser(self, _workspace: int, dir_id: str) -> None:
        self.deleted.append(dir_id)

    async def connection_info(
        self,
        dir_ids: list[str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> list[RoxyConnectionInfo]:
        self.connection_info_calls.append((dir_ids, timeout_seconds))
        return []


class CreateRetryRoxy(FakeRoxy):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0

    async def create_browser(
        self,
        workspace: int,
        probe_id: str,
        proxy: ProxyLease,
    ) -> str:
        self.create_calls += 1
        if self.create_calls == 1:
            raise RoxyApiError(
                "PRIVATE_CREATE_FAILURE",
                operation="browser_create",
                api_code=101,
                error_kind="api",
            )
        return await super().create_browser(workspace, probe_id, proxy)

    async def browsers(
        self,
        _workspace: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[object]:
        _ = timeout_seconds
        return []


class OpenRecoveryRoxy(FakeRoxy):
    def __init__(
        self,
        connection_outcomes: list[object],
        *,
        open_error: RoxyApiError | None = None,
    ) -> None:
        super().__init__()
        self.connection_outcomes = deque(connection_outcomes)
        self.last_connection_outcome = connection_outcomes[-1]
        self.open_error = open_error or RoxyApiError(
            "PRIVATE_OPEN_FAILURE",
            operation="browser_open",
            elapsed_ms=132,
            error_kind="transport",
        )
        self.open_calls = 0

    async def open_browser(
        self,
        _workspace: int,
        _dir_id: str,
        *,
        headless: bool,
    ) -> RoxyOpenResult:
        self.open_calls += 1
        self.opened_headless.append(headless)
        raise self.open_error

    async def connection_info(
        self,
        dir_ids: list[str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> list[RoxyConnectionInfo]:
        self.connection_info_calls.append((dir_ids, timeout_seconds))
        outcome = (
            self.connection_outcomes.popleft()
            if self.connection_outcomes
            else self.last_connection_outcome
        )
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, list)
        return outcome


class WorkspaceSequenceRoxy(FakeRoxy):
    def __init__(self, outcomes: list[object]) -> None:
        super().__init__()
        self.outcomes = deque(outcomes)
        self.last_outcome = outcomes[-1]

    async def workspaces(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> list[RoxyWorkspace]:
        self.workspace_timeouts.append(timeout_seconds)
        outcome = self.outcomes.popleft() if self.outcomes else self.last_outcome
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, list)
        return outcome


class ReadinessSequenceRoxy(WorkspaceSequenceRoxy):
    def __init__(
        self,
        workspace_outcomes: list[object],
        health_outcomes: list[object],
    ) -> None:
        super().__init__(workspace_outcomes)
        self.health_outcomes = deque(health_outcomes)
        self.last_health_outcome = health_outcomes[-1]

    async def health(self) -> None:
        self.health_calls += 1
        outcome = (
            self.health_outcomes.popleft()
            if self.health_outcomes
            else self.last_health_outcome
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return None


class FakeWorkspaceClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.elapsed += seconds


class FakeResourceStore:
    def __init__(
        self,
        *,
        available: bool = True,
        events: list[str] | None = None,
    ) -> None:
        self.available = available
        self.events = events
        self.document = {
            "_id": "email-id",
            "email": "person@example.com",
            "accessUrl": "https://mail.example/PRIVATE_ACCESS_URL",
        }
        self.reserved_by: list[str] = []
        self.released: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.completed_countries: list[str | None] = []
        self.stored_tokens: list[tuple[str, str, datetime]] = []
        self.stored_plans: list[tuple[str, AccountPlanResult]] = []
        self.plan_failures: list[tuple[str, str]] = []

    async def reserve_emails(self, count: int, owner: str) -> list[dict[str, object]]:
        assert count == 1
        if not self.available:
            raise InsufficientEmailsError("none")
        self.reserved_by.append(owner)
        return [dict(self.document)]

    async def release_email(self, email_id: str, owner: str) -> None:
        self.released.append((email_id, owner))

    async def complete_probe_profile_success(
        self,
        source: dict[str, object],
        owner: str,
        chatgpt_password: str = "",
        registration_country: str | None = None,
    ) -> SimpleNamespace:
        if self.events is not None:
            self.events.append("account_persisted")
        email_id = str(source["_id"])
        self.completed.append((email_id, owner))
        self.completed_countries.append(registration_country)
        assert chatgpt_password == "" or len(chatgpt_password) >= 12
        return SimpleNamespace(id="account-id")

    async def store_account_access_token(
        self,
        account_id: str,
        access_token: str,
        expires_at: datetime,
    ) -> datetime:
        if self.events is not None:
            self.events.append("access_token_stored")
        self.stored_tokens.append((account_id, access_token, expires_at))
        return FIXED_RECEIVED_AT

    async def store_account_plan_result(
        self,
        account_id: str,
        result: AccountPlanResult,
    ) -> None:
        self.stored_plans.append((account_id, result))

    async def store_account_plan_failure(
        self,
        account_id: str,
        error: PlanCheckError,
    ) -> None:
        self.plan_failures.append((account_id, error.code))


FIXED_SUBMITTED_AT = datetime(2026, 8, 9, 1, 30, tzinfo=timezone.utc)
FIXED_RECEIVED_AT = datetime(2026, 8, 9, 1, 30, 5, tzinfo=timezone.utc)


class FakeMailboxClient:
    def __init__(
        self,
        events: list[str] | None = None,
        error: MailboxClientError | None = None,
    ) -> None:
        self.verification = VerificationCodeResult(
            verification_code="222222",
            received_at_utc=FIXED_RECEIVED_AT,
            received_offset="+00:00",
            wait_ms=4_000,
            mail_age_ms=1_000,
        )
        self.events = events
        self.error = error
        self.wait_calls: list[tuple[str, str, datetime]] = []

    async def wait_for_new_code(
        self,
        access_url: str,
        email: str,
        submitted_at_utc: datetime,
        *,
        baseline=None,
    ) -> VerificationCodeResult:
        if self.events is not None:
            self.events.append("mailbox_request")
        self.wait_calls.append((access_url, email, submitted_at_utc))
        if self.error is not None:
            raise self.error
        return self.verification


class MsgTimeMailboxClient(MailboxClient):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.wait_calls: list[tuple[str, str, datetime]] = []

    async def get_snapshot(self, _access_url: str, _email: str):
        return parse_mailbox_snapshot(
            json.dumps(
                {
                    "attachments": [],
                    "mailbox": "INBOX",
                    "msg": (
                        "<html><head><title>Your temporary ChatGPT verification code"
                        "</title></head><body><p>Enter this temporary verification code "
                        "to continue:</p><p>222222</p></body></html>"
                    ),
                    "status": True,
                    "time": "Sun, 09 Aug 2026 01:30:05 +0000 (UTC)",
                }
            ),
            "text/plain",
        )

    async def wait_for_new_code(
        self,
        access_url: str,
        email: str,
        submitted_at_utc: datetime,
        *,
        baseline=None,
    ) -> VerificationCodeResult:
        self.events.append("mailbox_request")
        self.wait_calls.append((access_url, email, submitted_at_utc))
        return await super().wait_for_new_code(
            access_url,
            email,
            submitted_at_utc,
            baseline=baseline,
            utc_now=lambda: FIXED_RECEIVED_AT + timedelta(seconds=1),
            monotonic_now=lambda: 0.0,
        )


class FakeAutomation:
    def __init__(
        self,
        outcome: object,
        submitted_emails: list[str],
        events: list[str] | None = None,
        verification_outcome: object | None = None,
        profile_outcome: object | None = None,
        security_outcome: object | None = None,
        access_token_outcome: object | None = None,
        plan_check_outcome: object | None = None,
        password_outcome: object | None = None,
    ) -> None:
        self.outcome = outcome
        self.submitted_emails = submitted_emails
        self.events = events
        self.verification_outcome = verification_outcome or VerificationSubmitResult(
            final_url="https://auth.openai.com/about-you",
            next_step="transitioned",
            pre_continue_delay_ms=2_250,
            submitted_at_utc=FIXED_RECEIVED_AT,
        )
        self.password_outcome = password_outcome or PasswordSubmitResult(
            final_url="https://auth.openai.com/email-verification",
            next_step="verification",
            pre_continue_delay_ms=2_000,
            submitted_at_utc=FIXED_RECEIVED_AT,
        )
        self.profile_outcome = profile_outcome or ProfileCompletionResult(
            final_url="https://chatgpt.com/",
            next_step="account_created",
            skipped=False,
            skip_reason=None,
            full_name="Private ProfileName",
            age=31,
            name_to_age_delay_ms=1_500,
            age_to_finish_delay_ms=2_500,
            submitted_at_utc=FIXED_RECEIVED_AT,
            form_variant="birthday",
            locator_strategy="semantic_labels",
            submit_variant="continue",
        )
        self.security_outcome = security_outcome or SecurityNavigationResult(
            final_url="https://auth.openai.com/passkey-enroll",
            delays_ms=(5_000,),
            requested_at_utc=FIXED_RECEIVED_AT,
            opened_new_page=False,
            navigation_mode="direct_settings",
            redirect_state="final_after_trusted_intermediate",
            redirect_poll_count=4,
            redirect_elapsed_ms=750,
        )
        self.access_token_outcome = access_token_outcome or AccessTokenExtractionResult(
            access_token="TEST_ACCESS_TOKEN_DO_NOT_LOG",
            expires_at_utc=datetime(2026, 8, 10, tzinfo=timezone.utc),
            extracted_at_utc=FIXED_RECEIVED_AT,
            final_url="https://chatgpt.com/",
            homepage_restored=True,
        )
        self.plan_check_outcome = plan_check_outcome or AccountPlanResult(
            checked_at=FIXED_RECEIVED_AT,
            account_id="account-id",
            current_plan_type="free",
            subscription_plan="chatgptfreeplan",
            has_active_subscription=False,
            expires_at=None,
            renews_at=None,
            plus_trial_eligible=False,
            plus_trial_campaign_id=None,
        )
        self.submitted_verification_codes: list[str] = []
        self.submitted_passwords: list[str] = []

    async def __aenter__(self) -> "FakeAutomation":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def submit_email_and_continue(self, email: str) -> AutomationResult:
        self.submitted_emails.append(email)
        if self.events is not None:
            self.events.append("continue_completed")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        assert isinstance(self.outcome, AutomationResult)
        return self.outcome

    async def submit_verification_code_and_continue(
        self,
        verification_code: str,
    ) -> VerificationSubmitResult:
        self.submitted_verification_codes.append(verification_code)
        if self.events is not None:
            self.events.append("verification_continue_completed")
        if isinstance(self.verification_outcome, BaseException):
            raise self.verification_outcome
        assert isinstance(self.verification_outcome, VerificationSubmitResult)
        return self.verification_outcome

    async def submit_password_and_continue(
        self,
        password: str,
    ) -> PasswordSubmitResult:
        self.submitted_passwords.append(password)
        if self.events is not None:
            self.events.append("password_continue_completed")
        if isinstance(self.password_outcome, BaseException):
            raise self.password_outcome
        assert isinstance(self.password_outcome, PasswordSubmitResult)
        return self.password_outcome

    async def complete_profile_if_needed(self) -> ProfileCompletionResult:
        if self.events is not None:
            self.events.append("profile_completed")
        if isinstance(self.profile_outcome, BaseException):
            raise self.profile_outcome
        assert isinstance(self.profile_outcome, ProfileCompletionResult)
        return self.profile_outcome

    async def extract_chatgpt_access_token(self) -> AccessTokenExtractionResult:
        if self.events is not None:
            self.events.append("access_token_extracted")
        if isinstance(self.access_token_outcome, BaseException):
            raise self.access_token_outcome
        assert isinstance(self.access_token_outcome, AccessTokenExtractionResult)
        return self.access_token_outcome

    async def extract_chatgpt_account_plan(
        self,
        _access_token: str,
    ) -> AccountPlanResult:
        if self.events is not None:
            self.events.append("plan_checked")
        if isinstance(self.plan_check_outcome, BaseException):
            raise self.plan_check_outcome
        assert isinstance(self.plan_check_outcome, AccountPlanResult)
        return self.plan_check_outcome

    async def navigate_to_security_key_setup(self) -> SecurityNavigationResult:
        if self.events is not None:
            self.events.append("security_navigation")
        if isinstance(self.security_outcome, BaseException):
            raise self.security_outcome
        assert isinstance(self.security_outcome, SecurityNavigationResult)
        return self.security_outcome


class TotpAutomation(FakeAutomation):
    async def begin_totp_enrollment(self, _email: str) -> TotpEnrollmentChallenge:
        if self.events is not None:
            self.events.append("totp_reauth_started")
        return TotpEnrollmentChallenge(
            requested_at_utc=FIXED_RECEIVED_AT,
            final_url="https://auth.openai.com/email-verification",
        )

    async def complete_totp_enrollment(self, code: str) -> TotpEnrollmentResult:
        assert code == "222222"
        if self.events is not None:
            self.events.append("totp_activated")
        return TotpEnrollmentResult(
            secret="JBSWY3DPEHPK3PXP",
            access_token="REFRESHED_TOKEN_DO_NOT_LOG",
            access_token_expires_at_utc=datetime(2026, 8, 11, tzinfo=timezone.utc),
            activated_at_utc=FIXED_RECEIVED_AT,
            final_url="https://chatgpt.com/",
        )

    async def add_password_in_settings(
        self,
        password: str,
        secret: str,
        _email_code_provider,
    ) -> PasswordSetupResult:
        assert len(password) >= 12
        assert secret == "JBSWY3DPEHPK3PXP"
        if self.events is not None:
            self.events.append("password_configured")
        return PasswordSetupResult(
            final_url="https://chatgpt.com/#settings/Account",
            configured_at_utc=FIXED_RECEIVED_AT,
            email_reauth_used=False,
            totp_reauth_used=True,
        )


class TotpResourceStore(FakeResourceStore):
    def __init__(self, *, events: list[str] | None = None) -> None:
        super().__init__(events=events)
        self.stored_totp: list[tuple[str, str, str]] = []
        self.stored_passwords: list[tuple[str, str]] = []

    async def store_account_totp(
        self,
        account_id: str,
        secret: str,
        access_token: str,
        _expires_at: datetime,
        _activated_at: datetime,
    ) -> datetime:
        if self.events is not None:
            self.events.append("totp_stored")
        self.stored_totp.append((account_id, secret, access_token))
        return FIXED_RECEIVED_AT

    async def store_account_password(
        self,
        account_id: str,
        password: str,
        _configured_at: datetime,
    ) -> datetime:
        if self.events is not None:
            self.events.append("password_stored")
        self.stored_passwords.append((account_id, password))
        return FIXED_RECEIVED_AT


class RecordingArtifactWriter(ArtifactWriter):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.writes: list[dict[str, object]] = []

    def write_result(self, result: dict[str, object]) -> None:
        self.writes.append(json.loads(json.dumps(result)))
        super().write_result(result)


def settings() -> StoredExecutionSettings:
    return StoredExecutionSettings(
        browserExecutablePath=r"D:\RoxyBrowser\RoxyBrowser.exe",
        roxyApiKey=SecretStr("TEST_KEY_DO_NOT_LOG"),
        roxyApiPort=50000,
        headless=True,
        proxyRetryCount=1,
        concurrency=1,
        taskTimeoutSeconds=300,
    )


def proxy(identifier: str) -> ProxyLease:
    return ProxyLease(identifier, f"{identifier}.example.com", 10000, "user", "secret")


def navigation_error(
    code: str,
    message: str,
    *,
    elapsed_ms: int,
    net_error: str | None = None,
) -> ProxyNavigationError:
    login_navigation = code.startswith("login_navigation_")
    return ProxyNavigationError(
        stage="login_navigation" if login_navigation else "ip_navigation",
        code=code,
        message=message,
        exception_type="TimeoutError" if code.endswith("_timeout") else "Error",
        elapsed_ms=elapsed_ms,
        timeout_ms=90_000,
        net_error=net_error,
    )


def test_runner_retries_same_proxy_then_rotates_and_cleans_every_window(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    artifacts = RecordingArtifactWriter(tmp_path)
    submitted_emails: list[str] = []
    mailbox = FakeMailboxClient()
    outcomes = deque(
        [
            navigation_error(
                "ip_navigation_timeout",
                "代理出口 IP 页面导航超时",
                elapsed_ms=90_001,
            ),
            navigation_error(
                "ip_navigation_failed",
                "代理出口 IP 页面导航失败",
                elapsed_ms=321,
                net_error="net::ERR_PROXY_CONNECTION_FAILED",
            ),
            AutomationResult(
                "203.0.*.*",
                "https://chatgpt.com/auth/create-account/password",
                123,
                "password",
                2_250,
                FIXED_SUBMITTED_AT,
                email_pre_fill_delays_ms=(1_500,),
            ),
        ]
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=artifacts,
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            outcomes.popleft(), submitted_emails
        ),  # type: ignore[arg-type]
        registration_country="TR",
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["code"] == "account_access_token_extracted"
    assert result["proxyId"] == "p2"
    assert result["emailId"] == "email-id"
    assert result["nextStep"] == "password"
    assert result["passwordSubmitted"] is True
    assert result["passwordNextStep"] == "verification"
    assert result["preContinueDelayMs"] == 2_250
    assert result["emailFillAttempts"] == 1
    assert result["emailFormResetCount"] == 0
    assert result["loginChallengeObserved"] is False
    assert result["emailFormReadyWaitMs"] == 0
    assert result["emailPreContinueStableWaitsMs"] == []
    assert result["emailFormStabilityResetCount"] == 0
    assert result["emailPreFillDelaysMs"] == [1_500]
    assert result["emailContinueAttempts"] == 1
    assert result["emailPostSubmitResetCount"] == 0
    assert result["attempts"] == 3
    assert [item["attempt"] for item in result["attemptErrors"]] == [1, 2]
    assert [item["proxyId"] for item in result["attemptErrors"]] == ["p1", "p1"]
    assert [item["code"] for item in result["attemptErrors"]] == [
        "ip_navigation_timeout",
        "ip_navigation_failed",
    ]
    assert result["headless"] is True
    assert result["roxyOpenRecovered"] is False
    assert result["roxyOpenRecoveryMs"] == 0
    assert fake_store.acquired == ["p1", "p2"]
    assert fake_store.count_countries == ["TR"]
    assert fake_store.acquire_countries == ["TR", "TR"]
    assert fake_store.released == ["p1", "p2"]
    assert fake_store.successes == ["p2"]
    assert submitted_emails == ["person@example.com"] * 3
    assert len(mailbox.wait_calls) == 1
    assert len(fake_resources.reserved_by) == 1
    assert fake_resources.released == []
    assert fake_resources.completed == [("email-id", runner.owner)]
    assert fake_resources.completed_countries == ["TR"]
    assert len(fake_roxy.created) == 3
    assert fake_roxy.opened_headless == [True, True, True]
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_store.lock_released is True
    assert [item["status"] for item in artifacts.writes] == [
        "running",
        "running",
        "success",
    ]
    assert len(artifacts.writes[0]["attemptErrors"]) == 1
    assert len(artifacts.writes[1]["attemptErrors"]) == 2
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "TEST_KEY_DO_NOT_LOG" not in raw
    assert "secret" not in raw
    assert "ws://" not in raw
    assert "person@example.com" not in raw
    assert "PRIVATE_ACCESS_URL" not in raw
    assert "chatgptPassword" not in raw


def test_runner_retries_transient_roxy_browser_create_failure(tmp_path: Path) -> None:
    fake_roxy = CreateRetryRoxy()
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    recovery_clock = FakeWorkspaceClock()
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            automation_result, []
        ),  # type: ignore[arg-type]
        recovery_sleep=recovery_clock.sleep,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["code"] == "account_access_token_extracted"
    assert fake_roxy.create_calls == 2
    assert recovery_clock.sleep_calls == [0.5]
    assert fake_resources.completed == [("email-id", runner.owner)]


@pytest.mark.parametrize(
    "error_code",
    [
        "email_continue_timeout",
        "email_form_reset",
        "email_post_submit_reset",
    ],
)
def test_runner_rotates_proxy_after_transient_email_submission_failure(
    error_code: str,
    tmp_path: Path,
) -> None:
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    submitted_emails: list[str] = []
    outcomes = deque(
        [
            EmailStepError(
                error_code,
                "等待邮箱提交后的下一步页面超时",
            ),
            AutomationResult(
                "203.0.*.*",
                "https://auth.openai.com/email-verification",
                123,
                "verification",
                1_500,
                FIXED_SUBMITTED_AT,
            ),
        ]
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            outcomes.popleft(), submitted_emails
        ),  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["code"] == "account_access_token_extracted"
    assert result["attempts"] == 2
    assert result["attemptErrors"][0]["code"] == error_code
    assert fake_store.acquired == ["p1", "p2"]
    assert fake_store.released == ["p1", "p2"]
    assert submitted_emails == ["person@example.com", "person@example.com"]
    assert fake_resources.completed == [("email-id", runner.owner)]


def test_runner_records_terminal_transient_email_failure(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            EmailStepError(
                "email_post_submit_reset",
                "邮箱提交后被网页重复重置",
            ),
            [],
        ),  # type: ignore[arg-type]
        max_registration_proxy_rotations=0,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(EmailStepError):
        asyncio.run(runner.run())

    assert len(runner.attempt_errors) == 1
    assert runner.attempt_errors[0]["attempt"] == 1
    assert runner.attempt_errors[0]["proxyId"] == "p1"
    assert runner.attempt_errors[0]["code"] == "email_post_submit_reset"


def test_verification_result_is_redacted_by_default(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_roxy = FakeRoxy()
    events: list[str] = []
    fake_resources = FakeResourceStore(events=events)
    fake_mailbox = MsgTimeMailboxClient(events)
    submitted_emails: list[str] = []
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://chatgpt.com/auth/verify-email",
        123,
        "verification",
        1_750,
        FIXED_SUBMITTED_AT,
        email_fill_attempts=2,
        email_form_reset_count=1,
        email_pre_fill_delays_ms=(3_250, 4_750),
        email_continue_attempts=2,
        email_post_submit_reset_count=1,
        email_pre_continue_delays_ms=(4_250, 3_500),
        email_continue_click_failures=1,
        email_continue_recovery_state="stable_form_retry",
        email_continue_attempt_states=("click_exception", "click_succeeded"),
        email_continue_dispatch_observed=True,
        email_continue_click_exception_types=("RuntimeError",),
        email_continue_recovery_elapsed_ms=1_234,
        login_challenge_observed=True,
        email_form_ready_wait_ms=7_500,
        email_pre_continue_stable_waits_ms=(5_000, 6_500),
        email_form_stability_reset_count=2,
    )
    fake_automation = FakeAutomation(
        automation_result,
        submitted_emails,
        events,
    )
    hold_calls: list[float] = []

    async def record_hold(seconds: float) -> None:
        events.append("hold")
        hold_calls.append(seconds)

    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=60,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
        hold_sleep=record_hold,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["code"] == "account_access_token_extracted"
    assert result["verificationCodeReceived"] is True
    assert result["verificationCodeLength"] == 6
    assert result["verificationWaitMs"] == 0
    assert result["emailFillAttempts"] == 2
    assert result["emailFormResetCount"] == 1
    assert result["loginChallengeObserved"] is True
    assert result["emailFormReadyWaitMs"] == 7_500
    assert result["emailPreContinueStableWaitsMs"] == [5_000, 6_500]
    assert result["emailFormStabilityResetCount"] == 2
    assert result["emailPreFillDelaysMs"] == [3_250, 4_750]
    assert result["emailContinueAttempts"] == 2
    assert result["emailPostSubmitResetCount"] == 1
    assert result["emailPreContinueDelaysMs"] == [4_250, 3_500]
    assert result["emailContinueClickFailures"] == 1
    assert result["emailContinueRecoveryState"] == "stable_form_retry"
    assert result["emailContinueAttemptStates"] == [
        "click_exception",
        "click_succeeded",
    ]
    assert result["emailContinueDispatchObserved"] is True
    assert result["emailContinueClickExceptionTypes"] == ["RuntimeError"]
    assert result["emailContinueRecoveryElapsedMs"] == 1_234
    assert result["submittedAtUtc"] == "2026-08-09T01:30:00+00:00"
    assert result["mailReceivedAtUtc"] == "2026-08-09T01:30:05+00:00"
    assert result["mailReceivedOffset"] == "+00:00"
    assert result["mailAgeMs"] == 1_000
    assert result["verificationSubmitted"] is True
    assert result["verificationPreContinueDelayMs"] == 2_250
    assert result["verificationSubmittedAtUtc"] == "2026-08-09T01:30:05+00:00"
    assert result["verificationNextStep"] == "transitioned"
    assert result["verificationContinueAttempts"] == 1
    assert result["verificationClickCompleted"] is True
    assert result["verificationClickExceptionType"] is None
    assert result["verificationPostClickState"] == "transitioned"
    assert result["verificationWaitElapsedMs"] == 0
    assert result["verificationUrlChanged"] is True
    assert result["verificationInputVisibleAtEnd"] is False
    assert result["verificationButtonVisibleAtEnd"] is False
    assert result["finalUrl"] == "https://chatgpt.com/"
    assert result["accountId"] == "account-id"
    assert result["accountSetupPending"] is True
    assert result["profileSkipped"] is False
    assert result["profileFinishSubmitted"] is True
    assert result["profileNameToAgeDelayMs"] == 1_500
    assert result["profileAgeToFinishDelayMs"] == 2_500
    assert result["profileSubmittedAtUtc"] == "2026-08-09T01:30:05+00:00"
    assert result["profileNextStep"] == "account_created"
    assert result["profileFormVariant"] == "birthday"
    assert result["profileLocatorStrategy"] == "semantic_labels"
    assert result["profileSubmitVariant"] == "continue"
    assert result["accessTokenExtracted"] is True
    assert result["accessTokenExpiresAt"] == "2026-08-10T00:00:00+00:00"
    assert result["accessTokenUpdatedAt"] == "2026-08-09T01:30:05+00:00"
    assert not any(key.startswith("security") for key in result)
    assert "verificationCode" not in result
    assert events == [
        "continue_completed",
        "mailbox_request",
        "verification_continue_completed",
        "profile_completed",
        "account_persisted",
        "access_token_extracted",
        "access_token_stored",
        "plan_checked",
        "hold",
    ]
    assert hold_calls == [60]
    assert fake_automation.submitted_verification_codes == ["222222"]
    assert fake_mailbox.wait_calls == [
        (
            "https://mail.example/PRIVATE_ACCESS_URL",
            "person@example.com",
            FIXED_SUBMITTED_AT,
        )
    ]
    assert fake_resources.completed == [("email-id", runner.owner)]
    assert fake_resources.stored_tokens == [
        (
            "account-id",
            "TEST_ACCESS_TOKEN_DO_NOT_LOG",
            datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
    ]
    assert fake_resources.released == []
    persisted = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert persisted == result
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "222222" not in raw
    assert "111111" not in raw
    assert "person@example.com" not in raw
    assert "PRIVATE_ACCESS_URL" not in raw
    assert "temporary ChatGPT verification code" not in raw
    assert "<html>" not in raw
    assert "Private ProfileName" not in raw
    assert '"fullName":' not in raw
    assert '"age":' not in raw
    assert "TEST_ACCESS_TOKEN_DO_NOT_LOG" not in raw


def test_registration_enables_and_persists_totp(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_roxy = FakeRoxy()
    events: list[str] = []
    fake_resources = TotpResourceStore(events=events)
    fake_mailbox = MsgTimeMailboxClient(events)
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    fake_automation = TotpAutomation(
        automation_result,
        [],
        events,
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["status"] == "success"
    assert result["code"] == "account_security_configured"
    assert result["totpConfigured"] is True
    assert result["passwordConfigured"] is True
    assert result["totpActivatedAt"] == "2026-08-09T01:30:05+00:00"
    assert fake_resources.stored_totp == [
        (
            "account-id",
            "JBSWY3DPEHPK3PXP",
            "REFRESHED_TOKEN_DO_NOT_LOG",
        )
    ]
    assert len(fake_resources.stored_passwords) == 1
    assert fake_resources.stored_passwords[0][0] == "account-id"
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "JBSWY3DPEHPK3PXP" not in raw
    assert "REFRESHED_TOKEN_DO_NOT_LOG" not in raw
    assert len(fake_mailbox.wait_calls) == 2


def test_existing_profile_is_skipped_persisted_and_holds_on_home_page(
    tmp_path: Path,
) -> None:
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    events: list[str] = []
    fake_resources = FakeResourceStore(events=events)
    fake_roxy = FakeRoxy()
    fake_mailbox = FakeMailboxClient(events)
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    profile_completion = ProfileCompletionResult(
        final_url="https://chatgpt.com/",
        next_step="account_created",
        skipped=True,
        skip_reason="already_configured",
        full_name=None,
        age=None,
        name_to_age_delay_ms=None,
        age_to_finish_delay_ms=None,
        submitted_at_utc=None,
    )
    fake_automation = FakeAutomation(
        automation_result,
        [],
        events,
        profile_outcome=profile_completion,
        security_outcome=SecurityNavigationError(
            "security_settings",
            "security_settings_navigation_failed",
            "关闭状态下不应调用 Security 导航",
        ),
    )
    hold_calls: list[float] = []

    async def record_hold(seconds: float) -> None:
        events.append("hold")
        hold_calls.append(seconds)

    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=60,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
        hold_sleep=record_hold,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["status"] == "success"
    assert result["code"] == "account_access_token_extracted"
    assert result["profileSkipped"] is True
    assert result["profileSkipReason"] == "already_configured"
    assert result["profileFinishSubmitted"] is False
    assert result["profileNextStep"] == "account_created"
    assert "profileNameToAgeDelayMs" not in result
    assert "profileAgeToFinishDelayMs" not in result
    assert "profileSubmittedAtUtc" not in result
    assert result["finalUrl"] == "https://chatgpt.com/"
    assert not any(key.startswith("security") for key in result)
    assert events == [
        "continue_completed",
        "mailbox_request",
        "verification_continue_completed",
        "profile_completed",
        "account_persisted",
        "access_token_extracted",
        "access_token_stored",
        "plan_checked",
        "hold",
    ]
    assert fake_resources.completed == [("email-id", runner.owner)]
    assert fake_resources.released == []
    assert fake_store.acquired == ["p1"]
    assert fake_store.released == ["p1"]
    assert fake_store.successes == ["p1"]
    assert hold_calls == [60]
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_store.lock_released is True
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "person@example.com" not in raw
    assert "PRIVATE_ACCESS_URL" not in raw
    assert '"fullName":' not in raw
    assert '"age":' not in raw


def test_security_navigation_failure_is_partial_success_after_account_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_probe_module, "SECURITY_NAVIGATION_ENABLED", True)
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    events: list[str] = []
    fake_resources = FakeResourceStore(events=events)
    fake_roxy = FakeRoxy()
    fake_mailbox = FakeMailboxClient(events)
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    fake_automation = FakeAutomation(
        automation_result,
        [],
        events,
        security_outcome=SecurityNavigationError(
            "security_settings",
            "security_settings_navigation_failed",
            "ChatGPT Passkeys 设置页加载失败",
            redirect_state="trusted_intermediate_timeout",
            redirect_poll_count=7,
            redirect_elapsed_ms=45_000,
        ),
    )
    hold_calls: list[float] = []

    async def record_hold(seconds: float) -> None:
        hold_calls.append(seconds)

    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=60,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
        hold_sleep=record_hold,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["status"] == "partial_success"
    assert result["code"] == "account_created_security_navigation_failed"
    assert result["accountId"] == "account-id"
    assert result["accountSetupPending"] is True
    assert result["securityKeySetupPageReached"] is False
    assert result["securityStage"] == "security_settings"
    assert result["securityCode"] == "security_settings_navigation_failed"
    assert result["securityRedirectState"] == "trusted_intermediate_timeout"
    assert result["securityRedirectPollCount"] == 7
    assert result["securityRedirectElapsedMs"] == 45_000
    assert result["message"] == "ChatGPT Passkeys 设置页加载失败"
    assert result["finalUrl"] == "https://chatgpt.com/"
    assert events == [
        "continue_completed",
        "mailbox_request",
        "verification_continue_completed",
        "profile_completed",
        "account_persisted",
        "access_token_extracted",
        "access_token_stored",
        "plan_checked",
        "security_navigation",
    ]
    assert fake_resources.completed == [("email-id", runner.owner)]
    assert fake_resources.released == []
    assert fake_store.acquired == ["p1"]
    assert fake_store.released == ["p1"]
    assert fake_store.successes == ["p1"]
    assert hold_calls == []


def test_access_token_failure_is_partial_success_after_account_persisted(
    tmp_path: Path,
) -> None:
    fake_store = FakeStore([proxy("p1")])
    events: list[str] = []
    fake_resources = FakeResourceStore(events=events)
    fake_roxy = FakeRoxy()
    fake_mailbox = FakeMailboxClient(events)
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    fake_automation = FakeAutomation(
        automation_result,
        [],
        events,
        access_token_outcome=AccessTokenExtractionError(
            "session_response",
            "access_token_missing",
            "ChatGPT Session 响应缺少 AccessToken",
            homepage_restored=True,
        ),
    )
    hold_calls: list[float] = []

    async def record_hold(seconds: float) -> None:
        events.append("hold")
        hold_calls.append(seconds)

    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=60,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
        hold_sleep=record_hold,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["status"] == "partial_success"
    assert result["code"] == "account_created_access_token_failed"
    assert result["accountId"] == "account-id"
    assert result["accessTokenExtracted"] is False
    assert result["accessTokenStage"] == "session_response"
    assert result["accessTokenCode"] == "access_token_missing"
    assert result["accessTokenHomepageRestored"] is True
    assert result["message"] == "ChatGPT Session 响应缺少 AccessToken"
    assert fake_resources.completed == [("email-id", runner.owner)]
    assert fake_resources.stored_tokens == []
    assert events == [
        "continue_completed",
        "mailbox_request",
        "verification_continue_completed",
        "profile_completed",
        "account_persisted",
        "access_token_extracted",
        "hold",
    ]
    assert hold_calls == [60]
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_store.lock_released is True
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "person@example.com" not in raw
    assert "PRIVATE_ACCESS_URL" not in raw
    assert "Private ProfileName" not in raw


def test_plan_check_failure_does_not_downgrade_registered_account(
    tmp_path: Path,
) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    fake_automation = FakeAutomation(
        automation_result,
        [],
        plan_check_outcome=PlanCheckError(
            "plan_http_failed", http_status=503, retryable=True
        ),
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["status"] == "success"
    assert result["code"] == "account_access_token_extracted"
    assert result["planCheckStatus"] == "failed"
    assert result["planCheckErrorCode"] == "plan_http_failed"
    assert result["planCheckHttpStatus"] == 503
    assert fake_resources.plan_failures == [("account-id", "plan_http_failed")]
    assert fake_resources.stored_tokens
    assert fake_store.released == ["p1"]


def test_profile_failure_does_not_persist_consume_or_rotate_resources(
    tmp_path: Path,
) -> None:
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    events: list[str] = []
    fake_resources = FakeResourceStore(events=events)
    fake_roxy = FakeRoxy()
    fake_mailbox = FakeMailboxClient(events)
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    fake_automation = FakeAutomation(
        automation_result,
        [],
        events,
        profile_outcome=ProfileStepError(
            "profile_form_reset",
            "账号资料表单在提交前被网页重置",
            form_variant="birthday",
            locator_strategy="semantic_labels",
            submit_variant="continue",
        ),
    )
    hold_calls: list[float] = []

    async def record_hold(seconds: float) -> None:
        hold_calls.append(seconds)

    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=60,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
        hold_sleep=record_hold,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(ProfileStepError) as exc_info:
        asyncio.run(runner.run())

    assert exc_info.value.code == "profile_form_reset"
    assert exc_info.value.form_variant == "birthday"
    assert exc_info.value.locator_strategy == "semantic_labels"
    assert exc_info.value.submit_variant == "continue"
    assert events == [
        "continue_completed",
        "mailbox_request",
        "verification_continue_completed",
        "profile_completed",
    ]
    assert fake_resources.completed == []
    assert fake_resources.released == [("email-id", runner.owner)]
    assert fake_store.acquired == ["p1"]
    assert fake_store.released == ["p1"]
    assert fake_store.successes == []
    assert hold_calls == []
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_store.lock_released is True


def test_verification_rejection_does_not_retry_or_rotate_and_cleans_resources(
    tmp_path: Path,
) -> None:
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    events: list[str] = []
    fake_mailbox = FakeMailboxClient(events)
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://auth.openai.com/email-verification",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    fake_automation = FakeAutomation(
        automation_result,
        [],
        events,
        verification_outcome=VerificationStepError(
            "verification_code_rejected",
            "网站未接受该验证码或验证码已过期",
        ),
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: fake_automation,  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(VerificationStepError) as exc_info:
        asyncio.run(runner.run())

    assert exc_info.value.code == "verification_code_rejected"
    assert events == [
        "continue_completed",
        "mailbox_request",
        "verification_continue_completed",
    ]
    assert fake_automation.submitted_verification_codes == ["222222"]
    assert fake_store.acquired == ["p1"]
    assert fake_store.released == ["p1"]
    assert fake_store.successes == []
    assert len(fake_roxy.created) == 1
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_resources.released == [("email-id", runner.owner)]
    assert fake_store.lock_released is True


def test_mailbox_failure_happens_after_continue_and_cleans_resources(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    events: list[str] = []
    fake_mailbox = FakeMailboxClient(
        events,
        MailboxClientError(
            "mailbox_target_blocked",
            "接码地址指向了非公网目标",
        ),
    )
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://chatgpt.com/auth/verify-email",
        123,
        "verification",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            automation_result, [], events
        ),  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(MailboxClientError) as exc_info:
        asyncio.run(runner.run())

    assert exc_info.value.code == "mailbox_target_blocked"
    assert events == ["continue_completed", "mailbox_request"]
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_store.released == ["p1"]
    assert fake_resources.released == [("email-id", runner.owner)]
    assert fake_store.lock_released is True


def test_debug_mode_includes_verification_code_in_result_file(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    fake_mailbox = FakeMailboxClient()
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://chatgpt.com/auth/verify-email",
        123,
        "verification",
        2_000,
        FIXED_SUBMITTED_AT,
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        debug_show_code=True,
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            automation_result, []
        ),  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["verificationCode"] == "222222"
    persisted = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert persisted["verificationCode"] == "222222"
    assert "person@example.com" not in (tmp_path / "latest.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("arguments", "expected_debug"),
    [([], False), (["--debug-show-code"], True)],
)
def test_cli_debug_flag_controls_terminal_and_latest_json(
    arguments: list[str],
    expected_debug: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_writer = ArtifactWriter(tmp_path)
    artifact_writer.write_result(
        {"status": "success", "verificationCode": "999999"}
    )
    captured: dict[str, bool] = {}

    class FakeSettingsStore:
        def load(self) -> StoredExecutionSettings:
            return settings()

    class FakeCliRunner:
        attempt_errors: list[dict[str, object]] = []
        email_id = "email-id"

        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured["debug"] = bool(kwargs["debug_show_code"])

        async def run(self) -> tuple[dict[str, object], int]:
            result: dict[str, object] = {
                "status": "success",
                "code": "verification_continue_accepted",
                "verificationCodeReceived": True,
                "verificationCodeLength": 6,
            }
            if captured["debug"]:
                result["verificationCode"] = "222222"
            return result, 0

    monkeypatch.setattr(browser_probe_module, "SettingsStore", FakeSettingsStore)
    monkeypatch.setattr(browser_probe_module, "BrowserProbeRunner", FakeCliRunner)
    monkeypatch.setattr(browser_probe_module, "ArtifactWriter", lambda: artifact_writer)
    args = browser_probe_module.build_parser().parse_args(arguments)

    exit_code = asyncio.run(browser_probe_module.async_main(args))

    terminal_result = json.loads(capsys.readouterr().out)
    persisted = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert captured["debug"] is expected_debug
    assert (terminal_result.get("verificationCode") == "222222") is expected_debug
    assert (persisted.get("verificationCode") == "222222") is expected_debug
    assert "999999" not in (tmp_path / "latest.json").read_text(encoding="utf-8")


def test_cli_persists_safe_email_click_recovery_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_writer = ArtifactWriter(tmp_path)

    class FakeSettingsStore:
        def load(self) -> StoredExecutionSettings:
            return settings()

    class FakeCliRunner:
        attempt_errors: list[dict[str, object]] = []
        email_id = "email-id"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def run(self) -> tuple[dict[str, object], int]:
            raise EmailStepError(
                "continue_click_failed",
                "Continue 按钮点击失败",
                click_attempts=2,
                click_failures=2,
                recovery_state="stable_form_retry",
                attempt_states=("click_exception", "click_exception"),
                dispatch_observed=False,
                exception_types=("TimeoutError", "RuntimeError"),
                recovery_elapsed_ms=2_345,
                screenshot_captured=True,
            )

    monkeypatch.setattr(browser_probe_module, "SettingsStore", FakeSettingsStore)
    monkeypatch.setattr(browser_probe_module, "BrowserProbeRunner", FakeCliRunner)
    monkeypatch.setattr(browser_probe_module, "ArtifactWriter", lambda: artifact_writer)

    exit_code = asyncio.run(
        browser_probe_module.async_main(
            browser_probe_module.build_parser().parse_args([])
        )
    )

    terminal_result = json.loads(capsys.readouterr().out)
    persisted = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 7
    assert terminal_result == persisted
    assert persisted["emailContinueAttempts"] == 2
    assert persisted["emailContinueClickFailures"] == 2
    assert persisted["emailContinueRecoveryState"] == "stable_form_retry"
    assert persisted["emailContinueAttemptStates"] == [
        "click_exception",
        "click_exception",
    ]
    assert persisted["emailContinueDispatchObserved"] is False
    assert persisted["emailContinueClickExceptionTypes"] == [
        "TimeoutError",
        "RuntimeError",
    ]
    assert persisted["emailContinueRecoveryElapsedMs"] == 2_345
    assert persisted["screenshotCaptured"] is True
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "person@example.com" not in raw
    assert "ws://" not in raw


def test_cli_persists_safe_roxy_workspace_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_writer = ArtifactWriter(tmp_path)

    class FakeSettingsStore:
        def load(self) -> StoredExecutionSettings:
            return settings()

    class FakeCliRunner:
        attempt_errors: list[dict[str, object]] = []
        email_id = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def run(self) -> tuple[dict[str, object], int]:
            raise ProbeFailure(
                "roxy_workspace_not_ready",
                "Roxy API 已启动，但 workspace 在等待时间内仍未就绪",
                4,
                stage="roxy_workspace",
                operation="workspace_list",
                retry_count=3,
                elapsed_ms=3_000,
                api_code=901,
            )

    monkeypatch.setattr(browser_probe_module, "SettingsStore", FakeSettingsStore)
    monkeypatch.setattr(browser_probe_module, "BrowserProbeRunner", FakeCliRunner)
    monkeypatch.setattr(browser_probe_module, "ArtifactWriter", lambda: artifact_writer)

    exit_code = asyncio.run(
        browser_probe_module.async_main(
            browser_probe_module.build_parser().parse_args([])
        )
    )

    terminal_result = json.loads(capsys.readouterr().out)
    persisted = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 4
    assert terminal_result == persisted
    assert persisted == {
        "status": "failed",
        "code": "roxy_workspace_not_ready",
        "message": "Roxy API 已启动，但 workspace 在等待时间内仍未就绪",
        "stage": "roxy_workspace",
        "operation": "workspace_list",
        "retryCount": 3,
        "elapsedMs": 3_000,
        "apiCode": 901,
        "attemptErrors": [],
    }
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "TEST_KEY_DO_NOT_LOG" not in raw
    assert "PRIVATE_RESPONSE_BODY" not in raw


def test_cli_persists_safe_roxy_browser_recovery_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_writer = ArtifactWriter(tmp_path)

    class FakeSettingsStore:
        def load(self) -> StoredExecutionSettings:
            return settings()

    class FakeCliRunner:
        attempt_errors: list[dict[str, object]] = []
        email_id = "email-id"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def run(self) -> tuple[dict[str, object], int]:
            raise ProbeFailure(
                "roxy_browser_not_ready",
                "Roxy 已响应，但浏览器窗口在等待时间内未生成 CDP 连接",
                4,
                stage="roxy_browser",
                operation="browser_open",
                retry_count=0,
                elapsed_ms=132,
                error_kind="transport",
                recovery_poll_count=30,
                recovery_elapsed_ms=15_000,
            )

    monkeypatch.setattr(browser_probe_module, "SettingsStore", FakeSettingsStore)
    monkeypatch.setattr(browser_probe_module, "BrowserProbeRunner", FakeCliRunner)
    monkeypatch.setattr(browser_probe_module, "ArtifactWriter", lambda: artifact_writer)

    exit_code = asyncio.run(
        browser_probe_module.async_main(
            browser_probe_module.build_parser().parse_args([])
        )
    )

    terminal_result = json.loads(capsys.readouterr().out)
    persisted = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 4
    assert terminal_result == persisted
    assert persisted == {
        "status": "failed",
        "code": "roxy_browser_not_ready",
        "message": "Roxy 已响应，但浏览器窗口在等待时间内未生成 CDP 连接",
        "stage": "roxy_browser",
        "operation": "browser_open",
        "retryCount": 0,
        "elapsedMs": 132,
        "errorKind": "transport",
        "recoveryPollCount": 30,
        "recoveryElapsedMs": 15_000,
        "attemptErrors": [],
        "emailId": "email-id",
    }
    serialized = json.dumps(persisted)
    assert "TEST_ROXY_KEY_DO_NOT_LOG" not in serialized
    assert "ws://" not in serialized


def test_runner_clears_previous_debug_result_before_new_run(tmp_path: Path) -> None:
    artifacts = ArtifactWriter(tmp_path)
    artifacts.write_result(
        {"status": "success", "verificationCode": "999999"}
    )
    fake_store = FakeStore([proxy("p1")])
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=artifacts,
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: FakeRoxy(),  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = FakeResourceStore(available=False)  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 5
    assert result["code"] == "no_available_email"
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "999999" not in raw
    assert "verificationCode" not in raw


def test_workspace_retries_before_reserving_email_or_proxy(tmp_path: Path) -> None:
    workspace_error = RoxyApiError(
        "workspace not ready",
        operation="workspace_list",
        api_code=901,
        error_kind="api",
    )
    fake_roxy = WorkspaceSequenceRoxy(
        [
            workspace_error,
            [],
            [RoxyWorkspace(id=1, name="Local")],
        ]
    )
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    clock = FakeWorkspaceClock()
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://chatgpt.com/auth/create-account/password",
        123,
        "password",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            automation_result, []
        ),  # type: ignore[arg-type]
        monotonic_now=clock.monotonic,
        workspace_sleep=clock.sleep,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["code"] == "account_access_token_extracted"
    assert fake_roxy.workspace_timeouts == [3, 3, 3, 3, 3]
    assert fake_roxy.health_calls == 6
    assert clock.sleep_calls == [1, 1, 1, 1]
    assert len(fake_resources.reserved_by) == 1
    assert fake_store.acquired == ["p1"]
    assert len(fake_roxy.created) == 1
    assert fake_resources.released == []
    assert fake_resources.completed == [("email-id", runner.owner)]
    assert fake_store.released == ["p1"]
    assert fake_store.lock_released is True


def test_workspace_timeout_has_safe_diagnostics_and_no_resource_side_effects(
    tmp_path: Path,
) -> None:
    fake_roxy = WorkspaceSequenceRoxy(
        [
            RoxyApiError(
                "PRIVATE_RESPONSE_BODY",
                operation="workspace_list",
                api_code=901,
                error_kind="api",
            )
        ]
    )
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    clock = FakeWorkspaceClock()
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        workspace_ready_timeout_seconds=3,
        monotonic_now=clock.monotonic,
        workspace_sleep=clock.sleep,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(ProbeFailure) as exc_info:
        asyncio.run(runner.run())

    failure = exc_info.value
    assert failure.code == "roxy_workspace_not_ready"
    assert failure.result_fields() == {
        "stage": "roxy_workspace",
        "operation": "workspace_list",
        "retryCount": 3,
        "elapsedMs": 3_000,
        "apiCode": 901,
        "errorKind": "api",
    }
    assert fake_roxy.workspace_timeouts == [3, 2, 1]
    assert fake_resources.reserved_by == []
    assert fake_store.acquired == []
    assert fake_roxy.created == []
    assert fake_store.lock_released is True
    assert "PRIVATE_RESPONSE_BODY" not in failure.message


def test_workspace_auth_failure_is_immediate_and_does_not_touch_resources(
    tmp_path: Path,
) -> None:
    fake_roxy = WorkspaceSequenceRoxy(
        [
            RoxyApiError(
                "PRIVATE_RESPONSE_BODY",
                operation="workspace_list",
                http_status=401,
                retryable=False,
                error_kind="http",
            )
        ]
    )
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    clock = FakeWorkspaceClock()
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        monotonic_now=clock.monotonic,
        workspace_sleep=clock.sleep,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(ProbeFailure) as exc_info:
        asyncio.run(runner.run())

    failure = exc_info.value
    assert failure.code == "roxy_auth_failed"
    assert failure.result_fields() == {
        "stage": "roxy_workspace",
        "operation": "workspace_list",
        "retryCount": 0,
        "elapsedMs": 0,
        "httpStatus": 401,
        "errorKind": "http",
    }
    assert len(fake_roxy.workspace_timeouts) == 1
    assert clock.sleep_calls == []
    assert fake_resources.reserved_by == []
    assert fake_store.acquired == []
    assert fake_roxy.created == []


def test_multiple_workspaces_without_selection_fail_without_retry(tmp_path: Path) -> None:
    fake_roxy = WorkspaceSequenceRoxy(
        [
            [
                RoxyWorkspace(id=1, name="One"),
                RoxyWorkspace(id=2, name="Two"),
            ]
        ]
    )
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    clock = FakeWorkspaceClock()
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        monotonic_now=clock.monotonic,
        workspace_sleep=clock.sleep,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(ProbeFailure) as exc_info:
        asyncio.run(runner.run())

    assert exc_info.value.code == "workspace_required"
    assert len(fake_roxy.workspace_timeouts) == 1
    assert clock.sleep_calls == []
    assert fake_resources.reserved_by == []
    assert fake_store.acquired == []


def test_health_and_workspace_must_be_stable_three_times_before_resources(
    tmp_path: Path,
) -> None:
    fake_roxy = ReadinessSequenceRoxy(
        [[RoxyWorkspace(id=1, name="Local")]],
        [
            None,
            None,
            RoxyApiError(
                "PRIVATE_HEALTH_FLAP",
                operation="health",
                error_kind="transport",
            ),
            None,
            None,
            None,
        ],
    )
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    clock = FakeWorkspaceClock()
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://chatgpt.com/auth/create-account/password",
        123,
        "password",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            automation_result, []
        ),  # type: ignore[arg-type]
        monotonic_now=clock.monotonic,
        workspace_sleep=clock.sleep,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["roxyOpenRecovered"] is False
    assert fake_roxy.health_calls == 6
    assert len(fake_roxy.workspace_timeouts) == 4
    assert clock.sleep_calls == [1, 1, 1, 1]
    assert len(fake_resources.reserved_by) == 1
    assert fake_store.acquired == ["p1"]
    assert fake_store.expired_probe_leases_cleared == 1
    assert len(fake_roxy.created) == 1


@pytest.mark.parametrize(
    "open_error",
    [
        RoxyApiError(
            "PRIVATE_OPEN_TRANSPORT_FAILURE",
            operation="browser_open",
            elapsed_ms=132,
            error_kind="transport",
        ),
        RoxyApiError(
            "PRIVATE_OPEN_API_FAILURE",
            operation="browser_open",
            api_code=500,
            elapsed_ms=132,
            error_kind="api",
        ),
    ],
)
def test_browser_open_failure_recovers_existing_async_window(
    tmp_path: Path,
    open_error: RoxyApiError,
) -> None:
    matching = RoxyConnectionInfo(
        dir_id="dir-p1-0",
        ws="ws://127.0.0.1:52314/devtools/browser/PRIVATE_WS",
        http="127.0.0.1:52314",
        pid=123,
    )
    other = RoxyConnectionInfo(
        dir_id="different-dir",
        ws="ws://127.0.0.1:52315/devtools/browser/OTHER_PRIVATE_WS",
        http="127.0.0.1:52315",
        pid=124,
    )
    fake_roxy = OpenRecoveryRoxy(
        [
            RoxyApiError(
                "PRIVATE_CONNECTION_INFO_FAILURE",
                operation="browser_connection_info",
                error_kind="transport",
            ),
            [other],
            [matching],
        ],
        open_error=open_error,
    )
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    ready_clock = FakeWorkspaceClock()
    recovery_clock = FakeWorkspaceClock()
    automation_result = AutomationResult(
        "203.0.*.*",
        "https://chatgpt.com/auth/create-account/password",
        123,
        "password",
        1_500,
        FIXED_SUBMITTED_AT,
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            automation_result, []
        ),  # type: ignore[arg-type]
        monotonic_now=ready_clock.monotonic,
        workspace_sleep=ready_clock.sleep,
        recovery_monotonic_now=recovery_clock.monotonic,
        recovery_sleep=recovery_clock.sleep,
        browser_open_recovery_timeout_seconds=2,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 0
    assert result["roxyOpenRecovered"] is True
    assert result["roxyOpenRecoveryMs"] == 1_000
    assert fake_roxy.open_calls == 1
    assert fake_roxy.created == ["dir-p1-0"]
    assert fake_roxy.closed == ["dir-p1-0"]
    assert fake_roxy.deleted == ["dir-p1-0"]
    assert len(fake_roxy.connection_info_calls) == 3
    assert all(call[0] == ["dir-p1-0"] for call in fake_roxy.connection_info_calls)
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert "PRIVATE_WS" not in raw
    assert "OTHER_PRIVATE_WS" not in raw
    assert "PRIVATE_OPEN_FAILURE" not in raw
    assert "PRIVATE_OPEN_TRANSPORT_FAILURE" not in raw
    assert "PRIVATE_OPEN_API_FAILURE" not in raw


def test_browser_open_not_ready_does_not_retry_or_rotate_and_cleans_resources(
    tmp_path: Path,
) -> None:
    fake_roxy = OpenRecoveryRoxy([[]])
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    fake_resources = FakeResourceStore()
    ready_clock = FakeWorkspaceClock()
    recovery_clock = FakeWorkspaceClock()
    automation_created = False

    def automation_factory(*_args: object, **_kwargs: object):
        nonlocal automation_created
        automation_created = True
        raise AssertionError("Playwright must not start")

    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=automation_factory,  # type: ignore[arg-type]
        monotonic_now=ready_clock.monotonic,
        workspace_sleep=ready_clock.sleep,
        recovery_monotonic_now=recovery_clock.monotonic,
        recovery_sleep=recovery_clock.sleep,
        browser_open_recovery_timeout_seconds=1,
        browser_cleanup_observation_seconds=1,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(ProbeFailure) as exc_info:
        asyncio.run(runner.run())

    failure = exc_info.value
    assert failure.code == "roxy_browser_not_ready"
    assert failure.result_fields() == {
        "stage": "roxy_browser",
        "operation": "browser_open",
        "retryCount": 0,
        "elapsedMs": 132,
        "errorKind": "transport",
        "recoveryPollCount": 2,
        "recoveryElapsedMs": 1_000,
    }
    assert fake_roxy.open_calls == 1
    assert fake_roxy.created == ["dir-p1-0"]
    assert fake_roxy.closed == ["dir-p1-0"]
    assert fake_roxy.deleted == ["dir-p1-0"]
    assert fake_store.acquired == ["p1"]
    assert fake_store.released == ["p1"]
    assert fake_resources.released == [("email-id", runner.owner)]
    assert fake_store.lock_released is True
    assert automation_created is False
    assert "PRIVATE_OPEN_FAILURE" not in json.dumps(failure.result_fields())


def test_delayed_window_appearing_during_failure_cleanup_is_closed_again(
    tmp_path: Path,
) -> None:
    delayed = RoxyConnectionInfo(
        dir_id="dir-p1-0",
        ws="ws://127.0.0.1:52314/devtools/browser/PRIVATE_DELAYED_WS",
        http="127.0.0.1:52314",
        pid=123,
    )
    fake_roxy = OpenRecoveryRoxy([[], [], [], [delayed], []])
    fake_store = FakeStore([proxy("p1")])
    ready_clock = FakeWorkspaceClock()
    recovery_clock = FakeWorkspaceClock()
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        monotonic_now=ready_clock.monotonic,
        workspace_sleep=ready_clock.sleep,
        recovery_monotonic_now=recovery_clock.monotonic,
        recovery_sleep=recovery_clock.sleep,
        browser_open_recovery_timeout_seconds=1,
        browser_cleanup_observation_seconds=1,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = FakeResourceStore()  # type: ignore[assignment]

    with pytest.raises(ProbeFailure) as exc_info:
        asyncio.run(runner.run())

    assert exc_info.value.code == "roxy_browser_not_ready"
    assert fake_roxy.open_calls == 1
    assert fake_roxy.closed == ["dir-p1-0", "dir-p1-0"]
    assert fake_roxy.deleted == ["dir-p1-0", "dir-p1-0"]
    assert fake_store.released == ["p1"]


def test_connection_info_auth_failure_is_immediate_and_redacted(
    tmp_path: Path,
) -> None:
    auth_error = RoxyApiError(
        "PRIVATE_AUTH_RESPONSE",
        operation="browser_connection_info",
        http_status=401,
        retryable=False,
        error_kind="http",
    )
    fake_roxy = OpenRecoveryRoxy([auth_error])
    fake_store = FakeStore([proxy("p1")])
    ready_clock = FakeWorkspaceClock()
    recovery_clock = FakeWorkspaceClock()
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        monotonic_now=ready_clock.monotonic,
        workspace_sleep=ready_clock.sleep,
        recovery_monotonic_now=recovery_clock.monotonic,
        recovery_sleep=recovery_clock.sleep,
        browser_cleanup_observation_seconds=0.5,
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = FakeResourceStore()  # type: ignore[assignment]

    with pytest.raises(ProbeFailure) as exc_info:
        asyncio.run(runner.run())

    failure = exc_info.value
    assert failure.code == "roxy_auth_failed"
    assert failure.result_fields()["errorKind"] == "http"
    assert fake_roxy.open_calls == 1
    assert fake_store.acquired == ["p1"]
    assert fake_store.released == ["p1"]
    assert "PRIVATE_AUTH_RESPONSE" not in failure.message


def test_runner_preserves_every_attempt_error_when_pool_is_exhausted(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    submitted_emails: list[str] = []
    outcomes = deque(
        [
            navigation_error(
                "login_navigation_timeout",
                "ChatGPT 登录页导航超时",
                elapsed_ms=90_001,
            ),
            navigation_error(
                "login_navigation_failed",
                "ChatGPT 登录页导航失败",
                elapsed_ms=842,
                net_error="net::ERR_CONNECTION_RESET",
            ),
        ]
    )
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            outcomes.popleft(), submitted_emails
        ),  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 6
    assert result["code"] == "proxy_pool_exhausted"
    assert result["attempts"] == 2
    assert result["emailId"] == "email-id"
    assert [item["attempt"] for item in result["attemptErrors"]] == [1, 2]
    assert [item["code"] for item in result["attemptErrors"]] == [
        "login_navigation_timeout",
        "login_navigation_failed",
    ]
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert submitted_emails == ["person@example.com", "person@example.com"]
    assert fake_resources.released == [("email-id", runner.owner)]
    persisted = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert persisted == result


def test_runner_stops_before_browser_when_no_email_is_available(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore(available=False)
    fake_roxy = FakeRoxy()
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code == 5
    assert result["code"] == "no_available_email"
    assert fake_roxy.created == []
    assert fake_store.acquired == []
    assert fake_resources.released == []


@pytest.mark.parametrize(
    "error_code",
    ["email_rejected"],
)
def test_email_step_failure_does_not_rotate_proxy_and_releases_email(
    error_code: str,
    tmp_path: Path,
) -> None:
    fake_store = FakeStore([proxy("p1"), proxy("p2")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    fake_mailbox = FakeMailboxClient()
    submitted_emails: list[str] = []
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=fake_mailbox,  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            EmailStepError(error_code, "邮箱步骤未完成"), submitted_emails
        ),  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(EmailStepError) as exc_info:
        asyncio.run(runner.run())

    assert exc_info.value.code == error_code
    assert fake_store.acquired == ["p1"]
    assert fake_store.released == ["p1"]
    assert len(fake_roxy.created) == 1
    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_resources.released == [("email-id", runner.owner)]
    assert fake_mailbox.wait_calls == []
    assert fake_store.lock_released is True


def test_cancellation_cleans_browser_proxy_email_and_lock(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")])
    fake_resources = FakeResourceStore()
    fake_roxy = FakeRoxy()
    submitted_emails: list[str] = []
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=lambda *_args, **_kwargs: fake_roxy,  # type: ignore[arg-type]
        automation_factory=lambda *_args, **_kwargs: FakeAutomation(
            asyncio.CancelledError(), submitted_emails
        ),  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]
    runner.resources = fake_resources  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run())

    assert fake_roxy.closed == fake_roxy.created
    assert fake_roxy.deleted == fake_roxy.created
    assert fake_store.released == ["p1"]
    assert fake_resources.released == [("email-id", runner.owner)]
    assert fake_store.lock_released is True


def test_second_runner_stops_before_roxy_or_proxy_when_lock_is_busy(tmp_path: Path) -> None:
    fake_store = FakeStore([proxy("p1")], lock_available=False)
    roxy_created = False

    def roxy_factory(*_args: object, **_kwargs: object):
        nonlocal roxy_created
        roxy_created = True
        return FakeRoxy()

    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
        roxy_factory=roxy_factory,  # type: ignore[arg-type]
    )
    runner.store = fake_store  # type: ignore[assignment]

    result, exit_code = asyncio.run(runner.run())

    assert exit_code != 0
    assert result["code"] == "probe_already_running"
    assert roxy_created is False
    assert fake_store.acquired == []
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["code"] == "probe_already_running"


def test_roxy_auto_launch_discards_third_party_console_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StartingRoxy:
        def __init__(self) -> None:
            self.health_calls = 0

        async def health(self) -> None:
            self.health_calls += 1
            if self.health_calls == 1:
                raise RoxyApiError("not ready")

    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command: list[str], **kwargs: object) -> object:
        popen_calls.append((command, kwargs))
        return object()

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(browser_probe_module.Path, "is_file", lambda _self: True)
    monkeypatch.setattr(browser_probe_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser_probe_module.asyncio, "sleep", no_delay)
    runner = BrowserProbeRunner(
        settings(),
        workspace_id=None,
        hold_seconds=0,
        artifact_writer=ArtifactWriter(tmp_path),
        mongo_manager=FakeMongo(),  # type: ignore[arg-type]
        mailbox_client=FakeMailboxClient(),  # type: ignore[arg-type]
    )
    roxy = StartingRoxy()

    asyncio.run(runner._ensure_roxy_online(roxy))  # type: ignore[arg-type]

    assert roxy.health_calls == 2
    assert len(popen_calls) == 1
    _command, kwargs = popen_calls[0]
    assert kwargs["stdin"] == browser_probe_module.subprocess.DEVNULL
    assert kwargs["stdout"] == browser_probe_module.subprocess.DEVNULL
    assert kwargs["stderr"] == browser_probe_module.subprocess.DEVNULL
    assert kwargs["close_fds"] is True
