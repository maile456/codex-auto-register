from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .browser_automation import (
    AccessTokenExtractionError,
    AccessTokenExtractionResult,
    AutomationResult,
    CdpBrowserAutomation,
    CdpConnectionError,
    EmailStepError,
    PasswordStepError,
    PasswordSetupResult,
    PasswordSubmitResult,
    ProfileCompletionResult,
    ProfileStepError,
    ProxyNavigationError,
    SecurityNavigationError,
    SecurityNavigationResult,
    TargetChallengeError,
    TargetNotReachedError,
    VerificationStepError,
    VerificationSubmitResult,
    TotpEnrollmentError,
    TotpEnrollmentResult,
)
from .chatgpt_plan import AccountPlanResult, PlanCheckError
from .errors import InsufficientEmailsError, MongoUnavailableError, ResourceNotFoundError
from .mailbox_client import (
    MailboxClient,
    MailboxClientError,
    MailboxSnapshot,
    VerificationCodeResult,
    mailbox_source_for_document,
)
from .mongo_manager import MongoManager
from .probe_store import MongoProbeStore, ProxyLease
from .resource_service import MongoResourceStore
from .roxy_client import (
    MANAGED_BROWSER_PREFIX,
    RoxyApiError,
    RoxyClient,
    RoxyOpenResult,
    RoxyWorkspace,
)
from .settings_store import CorruptSettingsError, SettingsStore, StoredExecutionSettings


DEFAULT_ARTIFACT_DIR = Path(
    os.environ.get(
        "AUTOREGISTER_PROBE_ARTIFACT_DIR",
        r"D:\AutoRegister\data\browser-probe",
    )
)

# TODO: 后续重新实现 Security / Passkey 阶段时再启用。
SECURITY_NAVIGATION_ENABLED = False
TRANSIENT_REGISTRATION_PROXY_ERRORS = frozenset(
    {
        "email_form_reset",
        "email_post_submit_reset",
        "email_continue_timeout",
        "continue_click_failed",
        "email_next_step_unknown",
        "email_form_not_stable",
        "verification_continue_click_failed",
        "verification_retry_form_missing",
        "verification_next_step_unknown",
        "verification_form_unchanged_after_click",
    }
)


def generate_account_password() -> str:
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghjkmnpqrstuvwxyz"
    digits = "23456789"
    symbols = "!@#$%&*?"
    alphabet = upper + lower + digits + symbols
    characters = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(symbols),
        *(secrets.choice(alphabet) for _ in range(10)),
    ]
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


@dataclass(frozen=True, slots=True)
class ProbeFailure(Exception):
    code: str
    message: str
    exit_code: int
    stage: str | None = None
    operation: str | None = None
    retry_count: int | None = None
    elapsed_ms: int | None = None
    http_status: int | None = None
    api_code: int | None = None
    error_kind: str | None = None
    recovery_poll_count: int | None = None
    recovery_elapsed_ms: int | None = None

    def result_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self.stage is not None:
            fields["stage"] = self.stage
        if self.operation is not None:
            fields["operation"] = self.operation
        if self.retry_count is not None:
            fields["retryCount"] = self.retry_count
        if self.elapsed_ms is not None:
            fields["elapsedMs"] = self.elapsed_ms
        if self.http_status is not None:
            fields["httpStatus"] = self.http_status
        if self.api_code is not None:
            fields["apiCode"] = self.api_code
        if self.error_kind is not None:
            fields["errorKind"] = self.error_kind
        if self.recovery_poll_count is not None:
            fields["recoveryPollCount"] = self.recovery_poll_count
        if self.recovery_elapsed_ms is not None:
            fields["recoveryElapsedMs"] = self.recovery_elapsed_ms
        return fields


@dataclass(frozen=True, slots=True)
class ProbeAttemptResult:
    automation: AutomationResult
    password_submission: PasswordSubmitResult | None
    verification: VerificationCodeResult | None
    verification_submission: VerificationSubmitResult | None
    profile_completion: ProfileCompletionResult | None
    account_id: str | None
    access_token_extraction: AccessTokenExtractionResult | None
    access_token_error: AccessTokenExtractionError | None
    access_token_updated_at: datetime | None
    plan_check_result: AccountPlanResult | None
    plan_check_error: PlanCheckError | None
    totp_enrollment: TotpEnrollmentResult | None
    totp_error: TotpEnrollmentError | None
    password_setup: PasswordSetupResult | None
    password_setup_error: PasswordStepError | None
    security_navigation: SecurityNavigationResult | None
    security_error: SecurityNavigationError | None
    roxy_open_recovered: bool
    roxy_open_recovery_ms: int


class ArtifactWriter:
    def __init__(self, directory: Path = DEFAULT_ARTIFACT_DIR) -> None:
        self.directory = Path(directory)
        self.screenshot_path = self.directory / "latest.png"
        self.result_path = self.directory / "latest.json"
        self.temp_result_path = self.directory / "latest.json.tmp"

    def write_result(self, result: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.temp_result_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(self.temp_result_path, self.result_path)

    def clear_previous_screenshot(self) -> None:
        self.screenshot_path.unlink(missing_ok=True)

    def clear_previous_result(self) -> None:
        self.result_path.unlink(missing_ok=True)
        self.temp_result_path.unlink(missing_ok=True)


class BrowserProbeRunner:
    def __init__(
        self,
        settings: StoredExecutionSettings,
        *,
        workspace_id: int | None,
        hold_seconds: int,
        artifact_writer: ArtifactWriter | None = None,
        mongo_manager: MongoManager | None = None,
        roxy_factory: Callable[..., RoxyClient] = RoxyClient,
        automation_factory: Callable[..., CdpBrowserAutomation] = CdpBrowserAutomation,
        mailbox_client: MailboxClient | None = None,
        debug_show_code: bool = False,
        workspace_ready_timeout_seconds: float = 30,
        workspace_retry_interval_seconds: float = 1,
        workspace_request_timeout_seconds: float = 3,
        workspace_stable_successes_required: int = 3,
        browser_open_recovery_timeout_seconds: float = 45,
        browser_open_recovery_interval_seconds: float = 0.5,
        browser_connection_timeout_seconds: float = 3,
        browser_cleanup_observation_seconds: float = 15,
        monotonic_now: Callable[[], float] = monotonic,
        workspace_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        recovery_monotonic_now: Callable[[], float] = monotonic,
        recovery_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hold_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        reserved_email_id: str | None = None,
        reservation_owner: str | None = None,
        controller_lock_enabled: bool = True,
        worker_owner: str | None = None,
        workspace_lease_preacquired: bool = False,
        progress_callback: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
        password_factory: Callable[[], str] | None = None,
        max_registration_proxy_rotations: int = 9,
        registration_country: str | None = None,
        registration_proxy_group: str | None = None,
    ) -> None:
        self.settings = settings
        self.requested_workspace_id = workspace_id
        self.hold_seconds = hold_seconds
        self.artifacts = artifact_writer or ArtifactWriter()
        self.mongo = mongo_manager or MongoManager()
        self.store = MongoProbeStore(self.mongo)
        self.resources = MongoResourceStore(self.mongo)
        self.roxy_factory = roxy_factory
        self.automation_factory = automation_factory
        self.mailbox = mailbox_client or MailboxClient()
        self.debug_show_code = debug_show_code
        self.workspace_ready_timeout_seconds = workspace_ready_timeout_seconds
        self.workspace_retry_interval_seconds = workspace_retry_interval_seconds
        self.workspace_request_timeout_seconds = workspace_request_timeout_seconds
        self.workspace_stable_successes_required = max(
            1, workspace_stable_successes_required
        )
        self.browser_open_recovery_timeout_seconds = max(
            0, browser_open_recovery_timeout_seconds
        )
        self.browser_open_recovery_interval_seconds = max(
            0.001, browser_open_recovery_interval_seconds
        )
        self.browser_connection_timeout_seconds = max(
            0.001, browser_connection_timeout_seconds
        )
        self.browser_cleanup_observation_seconds = max(
            0, browser_cleanup_observation_seconds
        )
        self.monotonic_now = monotonic_now
        self.workspace_sleep = workspace_sleep
        self.recovery_monotonic_now = recovery_monotonic_now
        self.recovery_sleep = recovery_sleep
        self.hold_sleep = hold_sleep
        self.probe_id = str(uuid4())
        self.owner = worker_owner or f"probe:{self.probe_id}"
        self.reservation_owner = reservation_owner or self.owner
        self.pre_reserved_email_id = reserved_email_id
        self.controller_lock_enabled = controller_lock_enabled
        self.workspace_lease_preacquired = workspace_lease_preacquired
        self.progress_callback = progress_callback
        self.password_factory = password_factory or generate_account_password
        self.max_registration_proxy_rotations = max(
            0, max_registration_proxy_rotations
        )
        normalized_country = str(registration_country or "").strip().upper()
        if normalized_country and not re.fullmatch(r"[A-Z]{2}", normalized_country):
            raise ValueError("注册国家必须是两位国家码")
        self.registration_country = normalized_country or None
        self.registration_proxy_group = " ".join(
            str(registration_proxy_group or "").split()
        ) or None
        self.email_id: str | None = None
        self.email_consumed = False
        self.egress_ip: str | None = None
        self.current_proxy_id: str | None = None
        self.attempt_errors: list[dict[str, Any]] = []
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def run(self) -> tuple[dict[str, Any], int]:
        self._validate_settings()
        self.email_id = None
        self.email_consumed = False
        self.egress_ip = None
        self.current_proxy_id = None
        self.attempt_errors.clear()
        self.artifacts.clear_previous_result()
        self.artifacts.clear_previous_screenshot()
        await self.mongo.start()
        if not self.mongo.online:
            await self.mongo.stop()
            raise ProbeFailure("mongodb_unavailable", "MongoDB 当前不可用", 2)

        lock_acquired = False
        reserved_email_id: str | None = self.pre_reserved_email_id
        try:
            await self.store.ensure_indexes()
            await self.store.clear_expired_probe_leases()
            if self.controller_lock_enabled:
                lock_acquired = await self.store.acquire_probe_lock(self.owner)
                if not lock_acquired:
                    return self._failure(
                        "probe_already_running",
                        "已有真实浏览器探测控制器正在运行",
                        3,
                    )
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"probe-lock-heartbeat-{self.probe_id[:8]}",
            )

            async with self.roxy_factory(
                self.settings.roxyApiPort,
                self.settings.roxyApiKey,
            ) as roxy:
                await self._emit_progress("roxy_starting")
                await self._ensure_roxy_online(roxy)
                workspace = await self._wait_for_workspace_ready(roxy)
                pool_size = await self.store.count_eligible_proxies(
                    self.registration_country
                )
                if pool_size < 1:
                    return self._failure(
                        "no_eligible_proxy",
                        "没有可用于探测的已启用代理",
                        5,
                    )

                if self.pre_reserved_email_id is not None:
                    reserved_email = await self.resources.get_reserved_email(
                        self.pre_reserved_email_id,
                        self.reservation_owner,
                    )
                else:
                    try:
                        reserved_emails = await self.resources.reserve_emails(
                            1, self.reservation_owner
                        )
                    except InsufficientEmailsError:
                        return self._failure(
                            "no_available_email",
                            "邮箱池中没有可用邮箱",
                            5,
                        )
                    reserved_email = reserved_emails[0]
                reserved_email_id = str(reserved_email["_id"])
                self.email_id = reserved_email_id
                excluded: set[str] = set()
                last_proxy_id: str | None = None
                total_attempts = 0
                registration_proxy_rotations = 0
                for _ in range(pool_size):
                    lease = await self.store.acquire_proxy(
                        self.owner,
                        excluded_ids=excluded,
                        lease_seconds=180,
                        country=self.registration_country,
                        group=self.registration_proxy_group,
                    )
                    if lease is None:
                        break
                    excluded.add(lease.id)
                    last_proxy_id = lease.id
                    self.current_proxy_id = lease.id
                    try:
                        for attempt in range(self.settings.proxyRetryCount + 1):
                            total_attempts += 1
                            try:
                                attempt_result = await self._attempt(
                                    roxy,
                                    workspace.id,
                                    lease,
                                    reserved_email,
                                )
                            except ProxyNavigationError as exc:
                                self.attempt_errors.append(
                                    exc.as_attempt_error(
                                        attempt=total_attempts,
                                        proxy_id=lease.id,
                                    )
                                )
                                self._write_attempt_failure(
                                    workspace.id,
                                    lease.id,
                                    total_attempts,
                                )
                                if attempt < self.settings.proxyRetryCount:
                                    continue
                                break
                            except (EmailStepError, VerificationStepError) as exc:
                                if exc.code not in TRANSIENT_REGISTRATION_PROXY_ERRORS:
                                    raise
                                self.attempt_errors.append(
                                    {
                                        "attempt": total_attempts,
                                        "proxyId": lease.id,
                                        "stage": (
                                            "verification_submission"
                                            if isinstance(exc, VerificationStepError)
                                            else "email_submission"
                                        ),
                                        "code": exc.code,
                                        "message": exc.message,
                                        "exceptionType": type(exc).__name__,
                                    }
                                )
                                self._write_attempt_failure(
                                    workspace.id,
                                    lease.id,
                                    total_attempts,
                                )
                                if (
                                    registration_proxy_rotations
                                    >= self.max_registration_proxy_rotations
                                ):
                                    raise
                                registration_proxy_rotations += 1
                                break
                            automation_result = attempt_result.automation
                            password_submission = attempt_result.password_submission
                            verification = attempt_result.verification
                            verification_submission = (
                                attempt_result.verification_submission
                            )
                            profile_completion = attempt_result.profile_completion
                            account_id = attempt_result.account_id
                            access_token_extraction = (
                                attempt_result.access_token_extraction
                            )
                            access_token_error = attempt_result.access_token_error
                            access_token_updated_at = (
                                attempt_result.access_token_updated_at
                            )
                            plan_check_result = attempt_result.plan_check_result
                            plan_check_error = attempt_result.plan_check_error
                            totp_enrollment = attempt_result.totp_enrollment
                            totp_error = attempt_result.totp_error
                            password_setup = attempt_result.password_setup
                            password_setup_error = attempt_result.password_setup_error
                            security_navigation = (
                                attempt_result.security_navigation
                            )
                            security_error = attempt_result.security_error
                            roxy_open_recovered = (
                                attempt_result.roxy_open_recovered
                            )
                            roxy_open_recovery_ms = (
                                attempt_result.roxy_open_recovery_ms
                            )
                            await self.store.record_proxy_success(
                                lease.id,
                                automation_result.latency_ms,
                            )
                            result = {
                                "status": (
                                    "partial_success"
                                    if access_token_error is not None
                                    or totp_error is not None
                                    or password_setup_error is not None
                                    or security_error is not None
                                    else "success"
                                ),
                                "code": (
                                    "account_created_access_token_failed"
                                    if access_token_error is not None
                                    else (
                                        "account_created_2fa_failed"
                                        if totp_error is not None
                                        else (
                                            "account_created_password_failed"
                                            if password_setup_error is not None
                                            else (
                                                "account_created_security_navigation_failed"
                                                if security_error is not None
                                                else (
                                                    "account_security_configured"
                                                    if totp_enrollment is not None
                                                    and (
                                                        password_setup is not None
                                                        or password_submission is not None
                                                    )
                                                    else (
                                                        "account_2fa_enabled"
                                                        if totp_enrollment is not None
                                                        else (
                                                            "account_password_configured"
                                                            if password_setup is not None
                                                            or password_submission is not None
                                                            else (
                                                                "account_access_token_extracted"
                                                                if access_token_extraction is not None
                                                                and access_token_updated_at is not None
                                                                else (
                                                                    "security_key_setup_page_reached"
                                                                    if security_navigation is not None
                                                                    else (
                                                                        "account_profile_completed"
                                                                        if profile_completion is not None
                                                                        else (
                                                                            "verification_continue_accepted"
                                                                            if verification_submission is not None
                                                                            else "email_continue_accepted"
                                                                        )
                                                                    )
                                                                )
                                                            )
                                                        )
                                                    )
                                                )
                                            )
                                        )
                                    )
                                ),
                                "probeId": self.probe_id,
                                "proxyId": lease.id,
                                "workspaceId": workspace.id,
                                "emailId": self.email_id,
                                "headless": self.settings.headless,
                                "roxyOpenRecovered": roxy_open_recovered,
                                "roxyOpenRecoveryMs": roxy_open_recovery_ms,
                                "attempts": total_attempts,
                                "attemptErrors": list(self.attempt_errors),
                                "registrationCountry": self.registration_country,
                                "egressIpMasked": automation_result.egress_ip_masked,
                                "finalUrl": (
                                    security_navigation.final_url
                                    if security_navigation is not None
                                    else (
                                        access_token_extraction.final_url
                                        if access_token_extraction is not None
                                        else (
                                            profile_completion.final_url
                                            if profile_completion is not None
                                            else (
                                                verification_submission.final_url
                                                if verification_submission is not None
                                                else automation_result.final_url
                                            )
                                        )
                                    )
                                ),
                                "nextStep": automation_result.next_step,
                                "preContinueDelayMs": automation_result.pre_continue_delay_ms,
                                "emailFillAttempts": automation_result.email_fill_attempts,
                                "emailFormResetCount": automation_result.email_form_reset_count,
                                "loginChallengeObserved": (
                                    automation_result.login_challenge_observed
                                ),
                                "emailFormReadyWaitMs": (
                                    automation_result.email_form_ready_wait_ms
                                ),
                                "emailPreContinueStableWaitsMs": list(
                                    automation_result.email_pre_continue_stable_waits_ms
                                ),
                                "emailFormStabilityResetCount": (
                                    automation_result.email_form_stability_reset_count
                                ),
                                "emailPreFillDelaysMs": list(
                                    automation_result.email_pre_fill_delays_ms
                                ),
                                "emailContinueAttempts": (
                                    automation_result.email_continue_attempts
                                ),
                                "emailPostSubmitResetCount": (
                                    automation_result.email_post_submit_reset_count
                                ),
                                "emailPreContinueDelaysMs": list(
                                    automation_result.email_pre_continue_delays_ms
                                ),
                                "emailContinueClickFailures": (
                                    automation_result.email_continue_click_failures
                                ),
                                "emailContinueRecoveryState": (
                                    automation_result.email_continue_recovery_state
                                ),
                                "emailContinueAttemptStates": list(
                                    automation_result.email_continue_attempt_states
                                ),
                                "emailContinueDispatchObserved": (
                                    automation_result.email_continue_dispatch_observed
                                ),
                                "emailContinueClickExceptionTypes": list(
                                    automation_result.email_continue_click_exception_types
                                ),
                                "emailContinueRecoveryElapsedMs": (
                                    automation_result.email_continue_recovery_elapsed_ms
                                ),
                                "submittedAtUtc": automation_result.submitted_at_utc.isoformat(),
                                "screenshot": str(self.artifacts.screenshot_path),
                            }
                            if verification is not None:
                                result.update(
                                    {
                                        "verificationCodeReceived": True,
                                        "verificationCodeLength": len(
                                            verification.verification_code
                                        ),
                                        "verificationWaitMs": verification.wait_ms,
                                        "mailReceivedAtUtc": (
                                            verification.received_at_utc.isoformat()
                                            if verification.received_at_utc is not None
                                            else None
                                        ),
                                        "mailReceivedOffset": verification.received_offset,
                                        "mailAgeMs": verification.mail_age_ms,
                                    }
                                )
                                if self.debug_show_code:
                                    result["verificationCode"] = verification.verification_code
                            if password_submission is not None:
                                result.update(
                                    {
                                        "passwordSubmitted": True,
                                        "passwordNextStep": password_submission.next_step,
                                        "passwordPreContinueDelayMs": (
                                            password_submission.pre_continue_delay_ms
                                        ),
                                        "passwordClickCompleted": (
                                            password_submission.click_completed
                                        ),
                                        "passwordClickExceptionType": (
                                            password_submission.click_exception_type
                                        ),
                                    }
                                )
                            if verification_submission is not None:
                                result.update(
                                    {
                                        "verificationSubmitted": True,
                                        "verificationPreContinueDelayMs": (
                                            verification_submission.pre_continue_delay_ms
                                        ),
                                        "verificationSubmittedAtUtc": (
                                            verification_submission.submitted_at_utc.isoformat()
                                        ),
                                        "verificationNextStep": (
                                            verification_submission.next_step
                                        ),
                                        "verificationContinueAttempts": (
                                            verification_submission.continue_attempts
                                        ),
                                        "verificationClickCompleted": (
                                            verification_submission.click_completed
                                        ),
                                        "verificationClickExceptionType": (
                                            verification_submission.click_exception_type
                                        ),
                                        "verificationPostClickState": (
                                            verification_submission.post_click_state
                                        ),
                                        "verificationWaitElapsedMs": (
                                            verification_submission.wait_elapsed_ms
                                        ),
                                        "verificationUrlChanged": (
                                            verification_submission.url_changed
                                        ),
                                        "verificationInputVisibleAtEnd": (
                                            verification_submission.input_visible_at_end
                                        ),
                                        "verificationButtonVisibleAtEnd": (
                                            verification_submission.button_visible_at_end
                                        ),
                                    }
                                )
                            if profile_completion is not None:
                                result.update(
                                    {
                                        "accountId": account_id,
                                        "accountSetupPending": True,
                                        "profileSkipped": profile_completion.skipped,
                                        "profileFinishSubmitted": (
                                            not profile_completion.skipped
                                        ),
                                        "profileNextStep": (
                                            profile_completion.next_step
                                        ),
                                        "profileFormVariant": (
                                            profile_completion.form_variant
                                        ),
                                        "profileLocatorStrategy": (
                                            profile_completion.locator_strategy
                                        ),
                                        "profileSubmitVariant": (
                                            profile_completion.submit_variant
                                        ),
                                    }
                                )
                                if profile_completion.skipped:
                                    result["profileSkipReason"] = (
                                        profile_completion.skip_reason
                                    )
                                else:
                                    result.update(
                                        {
                                            "profileNameToAgeDelayMs": (
                                                profile_completion.name_to_age_delay_ms
                                            ),
                                            "profileAgeToFinishDelayMs": (
                                                profile_completion.age_to_finish_delay_ms
                                            ),
                                            "profileSubmittedAtUtc": (
                                                profile_completion.submitted_at_utc.isoformat()
                                                if profile_completion.submitted_at_utc
                                                is not None
                                                else None
                                            ),
                                        }
                                    )
                            if access_token_extraction is not None:
                                result.update(
                                    {
                                        "accessTokenExtracted": (
                                            access_token_updated_at is not None
                                        ),
                                        "accessTokenExpiresAt": (
                                            access_token_extraction.expires_at_utc.isoformat()
                                        ),
                                    }
                                )
                                if access_token_updated_at is not None:
                                    result["accessTokenUpdatedAt"] = (
                                        access_token_updated_at.isoformat()
                                    )
                            if access_token_error is not None:
                                result.update(
                                    {
                                        "message": access_token_error.message,
                                        "accessTokenExtracted": (
                                            access_token_updated_at is not None
                                        ),
                                        "accessTokenStage": access_token_error.stage,
                                        "accessTokenCode": access_token_error.code,
                                        "accessTokenHomepageRestored": (
                                            access_token_error.homepage_restored
                                        ),
                                    }
                                )
                            if plan_check_result is not None:
                                result.update(
                                    {
                                        "planCheckStatus": "success",
                                        "promotionEligible": (
                                            plan_check_result.plus_trial_eligible
                                        ),
                                        "currentPlanType": (
                                            plan_check_result.current_plan_type
                                        ),
                                        "subscriptionPlan": (
                                            plan_check_result.subscription_plan
                                        ),
                                        "hasActiveSubscription": (
                                            plan_check_result.has_active_subscription
                                        ),
                                        "planCheckedAt": (
                                            plan_check_result.checked_at.isoformat()
                                        ),
                                    }
                                )
                            elif plan_check_error is not None:
                                result.update(
                                    {
                                        "planCheckStatus": "failed",
                                        "planCheckErrorCode": plan_check_error.code,
                                        "planCheckHttpStatus": (
                                            plan_check_error.http_status
                                        ),
                                    }
                                )
                            if totp_enrollment is not None:
                                result.update(
                                    {
                                        "totpConfigured": True,
                                        "totpMode": "enabled",
                                        "totpActivatedAt": (
                                            totp_enrollment.activated_at_utc.isoformat()
                                        ),
                                    }
                                )
                            elif totp_error is not None:
                                result.update(
                                    {
                                        "message": totp_error.message,
                                        "totpConfigured": False,
                                        "totpMode": "failed",
                                        "totpStage": totp_error.stage,
                                        "totpCode": totp_error.code,
                                        "totpHttpStatus": totp_error.http_status,
                                    }
                                )
                            elif not self.settings.enableRegistrationTotp:
                                result.update(
                                    {
                                        "totpConfigured": False,
                                        "totpMode": "disabled",
                                        "totpSetupSkippedReason": "disabled_by_settings",
                                    }
                                )
                            if (
                                password_setup is not None
                                or password_submission is not None
                            ):
                                result["passwordConfigured"] = True
                                result["passwordMode"] = (
                                    "signup"
                                    if password_submission is not None
                                    else "settings"
                                )
                                if password_setup is not None:
                                    result["passwordConfiguredAt"] = (
                                        password_setup.configured_at_utc.isoformat()
                                    )
                                    result["passwordEmailReauthUsed"] = (
                                        password_setup.email_reauth_used
                                    )
                                    result["passwordTotpReauthUsed"] = (
                                        password_setup.totp_reauth_used
                                    )
                            elif password_setup_error is not None:
                                result.update(
                                    {
                                        "message": password_setup_error.message,
                                        "passwordConfigured": False,
                                        "passwordCode": password_setup_error.code,
                                    }
                                )
                            elif account_id is not None:
                                result.update(
                                    {
                                        "passwordConfigured": False,
                                        "passwordMode": "passwordless",
                                        "passwordSetupSkippedReason": (
                                            "disabled_by_settings"
                                        ),
                                    }
                                )
                            if security_navigation is not None:
                                result.update(
                                    {
                                        "securityKeySetupPageReached": True,
                                        "securityNavigationDelaysMs": list(
                                            security_navigation.delays_ms
                                        ),
                                        "securityNavigationRequestedAtUtc": (
                                            security_navigation.requested_at_utc.isoformat()
                                        ),
                                        "securityPageOpenedInNewTab": (
                                            security_navigation.opened_new_page
                                        ),
                                        "securityNavigationMode": (
                                            security_navigation.navigation_mode
                                        ),
                                        "securityRedirectState": (
                                            security_navigation.redirect_state
                                        ),
                                        "securityRedirectPollCount": (
                                            security_navigation.redirect_poll_count
                                        ),
                                        "securityRedirectElapsedMs": (
                                            security_navigation.redirect_elapsed_ms
                                        ),
                                    }
                                )
                            if security_error is not None:
                                result.update(
                                    {
                                        "message": security_error.message,
                                        "securityKeySetupPageReached": False,
                                        "securityStage": security_error.stage,
                                        "securityCode": security_error.code,
                                    }
                                )
                                if security_error.redirect_state is not None:
                                    result["securityRedirectState"] = (
                                        security_error.redirect_state
                                    )
                                if security_error.redirect_poll_count is not None:
                                    result["securityRedirectPollCount"] = (
                                        security_error.redirect_poll_count
                                    )
                                if security_error.redirect_elapsed_ms is not None:
                                    result["securityRedirectElapsedMs"] = (
                                        security_error.redirect_elapsed_ms
                                    )
                            self.artifacts.write_result(result)
                            return result, 0
                    finally:
                        await self.store.release_proxy(lease.id, self.owner)
                        self.current_proxy_id = None

                return self._failure(
                    "proxy_pool_exhausted",
                    "本轮代理池均未能打开目标页面",
                    6,
                    proxy_id=last_proxy_id,
                    attempts=total_attempts,
                )
        finally:
            await self._emit_progress("cleanup")
            if (
                reserved_email_id is not None
                and not self.email_consumed
                and self.mongo.online
            ):
                with suppress(Exception):
                    await self.resources.release_email(
                        reserved_email_id, self.reservation_owner
                    )
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
                self._heartbeat_task = None
            if lock_acquired and self.mongo.online:
                with suppress(Exception):
                    await self.store.release_probe_lock(self.owner)
            if (
                self.workspace_lease_preacquired
                and self.requested_workspace_id is not None
                and self.mongo.online
            ):
                with suppress(Exception):
                    await self.store.release_workspace(
                        self.requested_workspace_id, self.owner
                    )
            await self.mongo.stop()

    def _validate_settings(self) -> None:
        if not self.settings.roxyApiKey.get_secret_value():
            raise ProbeFailure(
                "roxy_api_key_missing",
                "请先在设置页填写 Roxy API Key",
                2,
            )
        if not self.settings.browserExecutablePath.strip():
            raise ProbeFailure(
                "browser_path_missing",
                "请先配置指纹浏览器地址",
                2,
            )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            if self.controller_lock_enabled:
                if not await self.store.heartbeat_probe_lock(self.owner):
                    return
            if (
                self.workspace_lease_preacquired
                and self.requested_workspace_id is not None
            ):
                if not await self.store.heartbeat_workspace(
                    self.requested_workspace_id,
                    self.owner,
                    lease_seconds=180,
                ):
                    return
            if self.current_proxy_id is not None:
                if not await self.store.heartbeat_proxy(
                    self.current_proxy_id,
                    self.owner,
                    lease_seconds=180,
                ):
                    return

    async def _emit_progress(self, stage: str, **details: Any) -> None:
        if self.progress_callback is None:
            return
        with suppress(Exception):
            await self.progress_callback(stage, details)

    async def _ensure_roxy_online(self, roxy: RoxyClient) -> None:
        started = monotonic()
        last_error: RoxyApiError | None = None
        try:
            await roxy.health()
            return
        except RoxyApiError as exc:
            if exc.is_auth_failure:
                raise self._roxy_auth_failure(
                    exc,
                    stage="roxy_health",
                    retry_count=0,
                    elapsed_ms=int((monotonic() - started) * 1000),
                ) from None
            last_error = exc

        executable = Path(self.settings.browserExecutablePath)
        if not executable.is_file():
            raise ProbeFailure(
                "browser_executable_missing",
                "配置的指纹浏览器地址不存在",
                2,
            )
        try:
            subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise ProbeFailure(
                "browser_launch_failed",
                "无法启动配置的指纹浏览器",
                4,
            ) from None

        for retry_count in range(1, 41):
            await asyncio.sleep(0.5)
            try:
                await roxy.health()
                return
            except RoxyApiError as exc:
                if exc.is_auth_failure:
                    raise self._roxy_auth_failure(
                        exc,
                        stage="roxy_health",
                        retry_count=retry_count,
                        elapsed_ms=int((monotonic() - started) * 1000),
                    ) from None
                last_error = exc
                continue
        raise ProbeFailure(
            "roxy_api_unavailable",
            "Roxy API 未就绪，请确认客户端已登录并开启 API",
            4,
            stage="roxy_health",
            operation=last_error.operation if last_error is not None else "health",
            retry_count=40,
            elapsed_ms=int((monotonic() - started) * 1000),
            http_status=last_error.http_status if last_error is not None else None,
            api_code=last_error.api_code if last_error is not None else None,
            error_kind=last_error.error_kind if last_error is not None else None,
        )

    async def _wait_for_workspace_ready(self, roxy: RoxyClient) -> RoxyWorkspace:
        started = self.monotonic_now()
        deadline = started + self.workspace_ready_timeout_seconds
        retry_count = 0
        last_error: RoxyApiError | None = None
        stable_successes = 0
        stable_workspace_id: int | None = None

        while True:
            remaining = deadline - self.monotonic_now()
            if remaining <= 0:
                break
            request_timeout = max(
                0.001,
                min(self.workspace_request_timeout_seconds, remaining),
            )
            try:
                await asyncio.wait_for(
                    roxy.health(),
                    timeout=request_timeout,
                )
            except TimeoutError:
                last_error = RoxyApiError(
                    "Roxy health 请求超时",
                    operation="health",
                    elapsed_ms=int(request_timeout * 1000),
                    error_kind="transport",
                )
                stable_successes = 0
                stable_workspace_id = None
            except RoxyApiError as exc:
                if exc.is_auth_failure:
                    raise self._roxy_auth_failure(
                        exc,
                        stage="roxy_health",
                        retry_count=retry_count,
                        elapsed_ms=int(
                            (self.monotonic_now() - started) * 1000
                        ),
                    ) from None
                last_error = exc
                stable_successes = 0
                stable_workspace_id = None
            else:
                try:
                    workspaces = await asyncio.wait_for(
                        roxy.workspaces(timeout_seconds=request_timeout),
                        timeout=request_timeout,
                    )
                except TimeoutError:
                    last_error = RoxyApiError(
                        "Roxy workspace 请求超时",
                        operation="workspace_list",
                        elapsed_ms=int(request_timeout * 1000),
                        error_kind="transport",
                    )
                    stable_successes = 0
                    stable_workspace_id = None
                except RoxyApiError as exc:
                    if exc.is_auth_failure:
                        raise self._roxy_auth_failure(
                            exc,
                            stage="roxy_workspace",
                            retry_count=retry_count,
                            elapsed_ms=int(
                                (self.monotonic_now() - started) * 1000
                            ),
                        ) from None
                    last_error = exc
                    stable_successes = 0
                    stable_workspace_id = None
                else:
                    try:
                        workspace = self._select_workspace(workspaces)
                    except ProbeFailure as exc:
                        if exc.code == "workspace_required":
                            raise
                        if exc.code not in {
                            "workspace_missing",
                            "workspace_not_found",
                        }:
                            raise
                        last_error = None
                        stable_successes = 0
                        stable_workspace_id = None
                    else:
                        if workspace.id == stable_workspace_id:
                            stable_successes += 1
                        else:
                            stable_workspace_id = workspace.id
                            stable_successes = 1
                        last_error = None
                        if (
                            stable_successes
                            >= self.workspace_stable_successes_required
                        ):
                            return workspace

            retry_count += 1
            remaining = deadline - self.monotonic_now()
            if remaining <= 0:
                break
            await self.workspace_sleep(
                min(self.workspace_retry_interval_seconds, remaining)
            )

        elapsed_ms = max(0, int((self.monotonic_now() - started) * 1000))
        raise ProbeFailure(
            "roxy_workspace_not_ready",
            "Roxy API 已启动，但 health 与 workspace 未达到连续稳定条件",
            4,
            stage=(
                "roxy_health"
                if last_error is not None and last_error.operation == "health"
                else "roxy_workspace"
            ),
            operation=(
                last_error.operation
                if last_error is not None
                else "workspace_list"
            ),
            retry_count=retry_count,
            elapsed_ms=elapsed_ms,
            http_status=last_error.http_status if last_error is not None else None,
            api_code=last_error.api_code if last_error is not None else None,
            error_kind=last_error.error_kind if last_error is not None else None,
        )

    @staticmethod
    def _roxy_auth_failure(
        error: RoxyApiError,
        *,
        stage: str,
        retry_count: int,
        elapsed_ms: int,
    ) -> ProbeFailure:
        return ProbeFailure(
            "roxy_auth_failed",
            "Roxy API 拒绝了当前凭据，请重新生成并保存 API Key",
            4,
            stage=stage,
            operation=error.operation,
            retry_count=retry_count,
            elapsed_ms=max(0, elapsed_ms),
            http_status=error.http_status,
            api_code=error.api_code,
            error_kind=error.error_kind,
        )

    def _select_workspace(self, workspaces: list[RoxyWorkspace]) -> RoxyWorkspace:
        if self.requested_workspace_id is not None:
            for workspace in workspaces:
                if workspace.id == self.requested_workspace_id:
                    return workspace
            raise ProbeFailure(
                "workspace_not_found",
                "指定的 Roxy workspace 不存在",
                4,
            )
        if len(workspaces) == 1:
            return workspaces[0]
        if not workspaces:
            raise ProbeFailure(
                "workspace_missing",
                "Roxy 账号中没有可用 workspace",
                4,
            )
        raise ProbeFailure(
            "workspace_required",
            "检测到多个 Roxy workspace，请使用 --workspace-id 指定",
            4,
        )

    async def _open_browser_with_recovery(
        self,
        roxy: RoxyClient,
        workspace_id: int,
        dir_id: str,
    ) -> RoxyOpenResult:
        try:
            return await roxy.open_browser(
                workspace_id,
                dir_id,
                headless=self.settings.headless,
            )
        except RoxyApiError as open_error:
            initial_open_error = open_error
            if initial_open_error.is_auth_failure:
                raise self._roxy_auth_failure(
                    initial_open_error,
                    stage="roxy_browser",
                    retry_count=0,
                    elapsed_ms=initial_open_error.elapsed_ms,
                ) from None
            if not initial_open_error.retryable:
                raise ProbeFailure(
                    "roxy_browser_not_ready",
                    "Roxy 未能启动临时浏览器窗口",
                    4,
                    stage="roxy_browser",
                    operation=initial_open_error.operation,
                    retry_count=0,
                    elapsed_ms=initial_open_error.elapsed_ms,
                    http_status=initial_open_error.http_status,
                    api_code=initial_open_error.api_code,
                    error_kind=initial_open_error.error_kind,
                    recovery_poll_count=0,
                    recovery_elapsed_ms=0,
                ) from None

        started = self.recovery_monotonic_now()
        deadline = started + self.browser_open_recovery_timeout_seconds
        poll_count = 0
        while self.recovery_monotonic_now() < deadline:
            remaining = deadline - self.recovery_monotonic_now()
            timeout_seconds = max(
                0.001,
                min(self.browser_connection_timeout_seconds, remaining),
            )
            poll_count += 1
            try:
                connections = await asyncio.wait_for(
                    roxy.connection_info(
                        [dir_id],
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                pass
            except RoxyApiError as exc:
                if exc.is_auth_failure:
                    raise self._roxy_auth_failure(
                        exc,
                        stage="roxy_browser",
                        retry_count=0,
                        elapsed_ms=max(
                            0,
                            int(
                                (self.recovery_monotonic_now() - started)
                                * 1000
                            ),
                        ),
                    ) from None
            else:
                connection = next(
                    (
                        item
                        for item in connections
                        if item.dir_id == dir_id
                    ),
                    None,
                )
                if connection is not None:
                    recovery_elapsed_ms = max(
                        0,
                        int(
                            (self.recovery_monotonic_now() - started) * 1000
                        ),
                    )
                    return RoxyOpenResult(
                        ws=connection.ws,
                        http=connection.http,
                        pid=connection.pid,
                        recovered=True,
                        recovery_elapsed_ms=recovery_elapsed_ms,
                    )

            remaining = deadline - self.recovery_monotonic_now()
            if remaining <= 0:
                break
            await self.recovery_sleep(
                min(self.browser_open_recovery_interval_seconds, remaining)
            )

        recovery_elapsed_ms = max(
            0,
            int((self.recovery_monotonic_now() - started) * 1000),
        )
        raise ProbeFailure(
            "roxy_browser_not_ready",
            "Roxy 已响应，但浏览器窗口在等待时间内未生成 CDP 连接",
            4,
            stage="roxy_browser",
            operation=initial_open_error.operation,
            retry_count=0,
            elapsed_ms=initial_open_error.elapsed_ms,
            http_status=initial_open_error.http_status,
            api_code=initial_open_error.api_code,
            error_kind=initial_open_error.error_kind,
            recovery_poll_count=poll_count,
            recovery_elapsed_ms=recovery_elapsed_ms,
        )

    async def _create_browser_with_recovery(
        self,
        roxy: RoxyClient,
        workspace_id: int,
        proxy: ProxyLease,
    ) -> str:
        expected_name = f"{MANAGED_BROWSER_PREFIX}{self.probe_id[:8]}"
        last_error: RoxyApiError | None = None
        for attempt in range(3):
            try:
                return await roxy.create_browser(
                    workspace_id,
                    self.probe_id,
                    proxy,
                )
            except RoxyApiError as exc:
                if exc.is_auth_failure or not exc.retryable:
                    raise
                last_error = exc

            try:
                browsers = await roxy.browsers(
                    workspace_id,
                    timeout_seconds=self.browser_connection_timeout_seconds,
                )
            except (AttributeError, RoxyApiError):
                browsers = []
            recovered = next(
                (
                    browser.dir_id
                    for browser in browsers
                    if browser.window_name == expected_name
                ),
                None,
            )
            if recovered is not None:
                return recovered
            if attempt < 2:
                await self.recovery_sleep(
                    self.browser_open_recovery_interval_seconds
                )

        if last_error is None:
            raise RuntimeError("Roxy 创建窗口恢复状态缺失")
        raise last_error

    async def _cleanup_browser(
        self,
        roxy: RoxyClient,
        workspace_id: int,
        dir_id: str,
        *,
        observe_delayed_open: bool,
    ) -> None:
        delete_error: RoxyApiError | None = None
        with suppress(RoxyApiError):
            await roxy.close_browser(dir_id)
        try:
            await roxy.delete_browser(workspace_id, dir_id)
        except RoxyApiError as exc:
            delete_error = exc

        if (
            not observe_delayed_open
            and delete_error is not None
            and delete_error.retryable
        ):
            for _ in range(2):
                await self.recovery_sleep(
                    self.browser_open_recovery_interval_seconds
                )
                try:
                    await roxy.delete_browser(workspace_id, dir_id)
                except RoxyApiError as exc:
                    delete_error = exc
                    if not exc.retryable:
                        break
                else:
                    delete_error = None
                    break

        if not observe_delayed_open:
            if delete_error is not None:
                raise ProbeFailure(
                    "browser_cleanup_failed",
                    "临时 Roxy 窗口删除失败",
                    8,
                    stage="roxy_browser_cleanup",
                    operation=delete_error.operation,
                    http_status=delete_error.http_status,
                    api_code=delete_error.api_code,
                    error_kind=delete_error.error_kind,
                ) from None
            return

        started = self.recovery_monotonic_now()
        deadline = started + self.browser_cleanup_observation_seconds
        final_connections: list[Any] | None = None
        last_query_error: RoxyApiError | None = None
        absent_confirmations = 0
        while True:
            remaining = max(0, deadline - self.recovery_monotonic_now())
            timeout_seconds = max(
                0.001,
                min(
                    self.browser_connection_timeout_seconds,
                    max(0.001, remaining),
                ),
            )
            try:
                connections = await asyncio.wait_for(
                    roxy.connection_info(
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                final_connections = None
                last_query_error = RoxyApiError(
                    "Roxy 延迟窗口清理查询超时",
                    operation="browser_connection_info",
                    error_kind="transport",
                )
            except RoxyApiError as exc:
                final_connections = None
                last_query_error = exc
            else:
                last_query_error = None
                final_connections = [
                    item for item in connections if item.dir_id == dir_id
                ]
                if final_connections:
                    absent_confirmations = 0
                    with suppress(RoxyApiError):
                        await roxy.close_browser(dir_id)
                    try:
                        await roxy.delete_browser(workspace_id, dir_id)
                    except RoxyApiError as exc:
                        delete_error = exc
                    else:
                        delete_error = None
                else:
                    absent_confirmations += 1

            remaining = deadline - self.recovery_monotonic_now()
            if remaining <= 0:
                break
            await self.recovery_sleep(
                min(self.browser_open_recovery_interval_seconds, remaining)
            )

        if final_connections:
            try:
                confirmations = await asyncio.wait_for(
                    roxy.connection_info(
                        timeout_seconds=self.browser_connection_timeout_seconds,
                    ),
                    timeout=self.browser_connection_timeout_seconds,
                )
            except TimeoutError:
                last_query_error = RoxyApiError(
                    "Roxy 延迟窗口最终清理确认超时",
                    operation="browser_connection_info",
                    error_kind="transport",
                )
            except RoxyApiError as exc:
                last_query_error = exc
            else:
                last_query_error = None
                final_connections = [
                    item for item in confirmations if item.dir_id == dir_id
                ]

        query_unconfirmed = (
            last_query_error is not None and absent_confirmations < 2
        )
        if final_connections or query_unconfirmed or delete_error is not None:
            diagnostic_error = last_query_error or delete_error
            raise ProbeFailure(
                "browser_cleanup_failed",
                "无法确认延迟启动的 Roxy 临时窗口已清理",
                8,
                stage="roxy_browser_cleanup",
                operation=(
                    diagnostic_error.operation
                    if diagnostic_error is not None
                    else "browser_connection_info"
                ),
                http_status=(
                    diagnostic_error.http_status
                    if diagnostic_error is not None
                    else None
                ),
                api_code=(
                    diagnostic_error.api_code
                    if diagnostic_error is not None
                    else None
                ),
                error_kind=(
                    diagnostic_error.error_kind
                    if diagnostic_error is not None
                    else "contract"
                ),
                recovery_elapsed_ms=max(
                    0,
                    int(
                        (self.recovery_monotonic_now() - started) * 1000
                    ),
                ),
            )

    async def _attempt(
        self,
        roxy: RoxyClient,
        workspace_id: int,
        proxy: ProxyLease,
        email_source: dict[str, Any],
    ) -> ProbeAttemptResult:
        email = str(email_source["email"])
        access_url = mailbox_source_for_document(email_source)
        dir_id: str | None = None
        open_requested = False
        open_completed = False
        primary_error: BaseException | None = None
        try:
            dir_id = await self._create_browser_with_recovery(
                roxy,
                workspace_id,
                proxy,
            )
            await self._emit_progress("roxy_starting", dirId=dir_id)
            open_requested = True
            opened = await self._open_browser_with_recovery(
                roxy,
                workspace_id,
                dir_id,
            )
            open_completed = True
            await self._emit_progress("proxy_check", dirId=dir_id)
            async with self.automation_factory(
                opened.ws,
                self.artifacts.screenshot_path,
                signup_screen_hint=(
                    "signup"
                    if self.settings.requireRegistrationPassword
                    else "login_or_signup"
                ),
            ) as automation:
                await self._emit_progress("login", dirId=dir_id)
                mailbox_baseline: MailboxSnapshot | None = None
                baseline_reader = getattr(self.mailbox, "get_snapshot", None)
                if callable(baseline_reader):
                    try:
                        mailbox_baseline = await baseline_reader(access_url, email)
                    except MailboxClientError as exc:
                        if not exc.retryable:
                            raise
                        mailbox_baseline = MailboxSnapshot(
                            fingerprint="",
                            verification_code=None,
                            received_at_utc=None,
                            received_offset=None,
                        )
                result = await automation.submit_email_and_continue(email)
                if (
                    self.settings.requireRegistrationPassword
                    and result.next_step != "password"
                ):
                    raise PasswordStepError(
                        "registration_password_not_offered",
                        "注册服务未提供密码创建步骤，已停止无密码注册",
                    )
                self.egress_ip = result.egress_ip
                await self._emit_progress(
                    "email",
                    dirId=dir_id,
                    egressIp=result.egress_ip,
                )
                verification: VerificationCodeResult | None = None
                password_submission: PasswordSubmitResult | None = None
                verification_submission: VerificationSubmitResult | None = None
                profile_completion: ProfileCompletionResult | None = None
                account_id: str | None = None
                access_token_extraction: AccessTokenExtractionResult | None = None
                access_token_error: AccessTokenExtractionError | None = None
                access_token_updated_at: datetime | None = None
                plan_check_result: AccountPlanResult | None = None
                plan_check_error: PlanCheckError | None = None
                totp_enrollment: TotpEnrollmentResult | None = None
                totp_error: TotpEnrollmentError | None = None
                totp_verification: VerificationCodeResult | None = None
                password_setup: PasswordSetupResult | None = None
                password_setup_error: PasswordStepError | None = None
                security_navigation: SecurityNavigationResult | None = None
                security_error: SecurityNavigationError | None = None
                account_password = ""
                registration_next_step = result.next_step
                verification_requested_at = result.submitted_at_utc
                if result.next_step == "password":
                    if self.settings.requireRegistrationPassword:
                        await self._emit_progress(
                            "password",
                            dirId=dir_id,
                            egressIp=result.egress_ip,
                        )
                        account_password = self.password_factory()
                        password_submission = (
                            await automation.submit_password_and_continue(
                                account_password
                            )
                        )
                        registration_next_step = password_submission.next_step
                        verification_requested_at = (
                            password_submission.submitted_at_utc
                        )
                    else:
                        passwordless_submission = (
                            await automation.switch_password_page_to_email_code()
                        )
                        registration_next_step = passwordless_submission.next_step
                        verification_requested_at = (
                            passwordless_submission.submitted_at_utc
                        )
                        if registration_next_step == "totp":
                            raise EmailStepError(
                                "existing_account_totp_required",
                                "该邮箱已存在账号并要求认证器验证码",
                            )
                        if registration_next_step != "verification":
                            raise PasswordStepError(
                                "passwordless_email_code_not_reached",
                                "关闭注册密码后，密码页面未能切换到邮箱验证码页面",
                            )
                if registration_next_step != "verification":
                    if registration_next_step == "totp":
                        raise EmailStepError(
                            "existing_account_totp_required",
                            "该邮箱已存在账号并要求认证器验证码",
                        )
                    raise EmailStepError(
                        "registration_next_step_unknown",
                        "邮箱提交后未进入可识别的密码或验证码页面",
                    )
                if registration_next_step == "verification":
                    await self._emit_progress(
                        "verification",
                        dirId=dir_id,
                        egressIp=result.egress_ip,
                    )
                    wait_options: dict[str, Any] = {}
                    if mailbox_baseline is not None:
                        wait_options["baseline"] = mailbox_baseline
                    verification = await self.mailbox.wait_for_new_code(
                        access_url,
                        email,
                        verification_requested_at,
                        **wait_options,
                    )
                    verification_submission = (
                        await automation.submit_verification_code_and_continue(
                            verification.verification_code
                        )
                    )
                    await self._emit_progress("profile", dirId=dir_id)
                    profile_completion = await automation.complete_profile_if_needed()
                    account = await self.resources.complete_probe_profile_success(
                        email_source,
                        self.reservation_owner,
                        chatgpt_password=account_password,
                        registration_country=self.registration_country,
                        registration_proxy_group=self.registration_proxy_group,
                    )
                    account_id = account.id
                    self.email_consumed = True
                    await self._emit_progress("access_token", dirId=dir_id)
                    try:
                        access_token_extraction = (
                            await automation.extract_chatgpt_access_token()
                        )
                    except AccessTokenExtractionError as exc:
                        access_token_error = exc
                    else:
                        try:
                            access_token_updated_at = (
                                await self.resources.store_account_access_token(
                                    account.id,
                                    access_token_extraction.access_token,
                                    access_token_extraction.expires_at_utc,
                                )
                            )
                        except (MongoUnavailableError, ResourceNotFoundError, OSError, ValueError):
                            access_token_error = AccessTokenExtractionError(
                                "access_token_store",
                                "access_token_store_failed",
                                "AccessToken 保存到 MongoDB 失败",
                                homepage_restored=(
                                    access_token_extraction.homepage_restored
                                ),
                            )
                        else:
                            if not access_token_extraction.homepage_restored:
                                access_token_error = AccessTokenExtractionError(
                                    "session_home_restore",
                                    "session_home_restore_failed",
                                    "AccessToken 已保存，但未能恢复 ChatGPT 主界面",
                                    homepage_restored=False,
                                )
                            else:
                                try:
                                    plan_check_result = (
                                        await automation.extract_chatgpt_account_plan(
                                            access_token_extraction.access_token
                                        )
                                    )
                                except PlanCheckError as exc:
                                    plan_check_error = exc
                                    try:
                                        await self.resources.store_account_plan_failure(
                                            account.id, exc
                                        )
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        await self.resources.store_account_plan_result(
                                            account.id, plan_check_result
                                        )
                                    except Exception:
                                        plan_check_error = PlanCheckError(
                                            "plan_result_store_failed"
                                        )
                                        plan_check_result = None
                                        try:
                                            await self.resources.store_account_plan_failure(
                                                account.id, plan_check_error
                                            )
                                        except Exception:
                                            pass
                                    else:
                                        try:
                                            checkout_type_result = (
                                                await automation.extract_chatgpt_checkout_type(
                                                    access_token_extraction.access_token,
                                                    country=self.registration_country or "JP",
                                                )
                                            )
                                            await self.resources.store_account_checkout_type(
                                                account.id, checkout_type_result
                                            )
                                        except Exception:
                                            # Classification is advisory and must not fail registration.
                                            pass
                    if (
                        self.settings.enableRegistrationTotp
                        and access_token_extraction is not None
                        and access_token_updated_at is not None
                        and access_token_error is None
                        and callable(
                            getattr(automation, "begin_totp_enrollment", None)
                        )
                        and callable(
                            getattr(automation, "complete_totp_enrollment", None)
                        )
                        and callable(
                            getattr(self.resources, "store_account_totp", None)
                        )
                    ):
                        await self._emit_progress(
                            "two_factor",
                            dirId=dir_id,
                            egressIp=result.egress_ip,
                        )
                        try:
                            totp_baseline: MailboxSnapshot | None = None
                            if callable(baseline_reader):
                                try:
                                    totp_baseline = await baseline_reader(
                                        access_url, email
                                    )
                                except MailboxClientError:
                                    totp_baseline = None
                            challenge = await automation.begin_totp_enrollment(email)
                            wait_options = {}
                            if totp_baseline is not None:
                                wait_options["baseline"] = totp_baseline
                            totp_verification = await self.mailbox.wait_for_new_code(
                                access_url,
                                email,
                                challenge.requested_at_utc,
                                **wait_options,
                            )
                            totp_enrollment = (
                                await automation.complete_totp_enrollment(
                                    totp_verification.verification_code
                                )
                            )
                            access_token_updated_at = (
                                await self.resources.store_account_totp(
                                    account.id,
                                    totp_enrollment.secret,
                                    totp_enrollment.access_token,
                                    totp_enrollment.access_token_expires_at_utc,
                                    totp_enrollment.activated_at_utc,
                                )
                            )
                        except TotpEnrollmentError as exc:
                            totp_error = exc
                        except (MailboxClientError, MongoUnavailableError, ResourceNotFoundError, OSError, ValueError) as exc:
                            totp_error = TotpEnrollmentError(
                                "totp_store",
                                "totp_setup_or_store_failed",
                                "2FA 设置或保存失败",
                            )
                    if SECURITY_NAVIGATION_ENABLED:
                        try:
                            security_navigation = (
                                await automation.navigate_to_security_key_setup()
                            )
                        except SecurityNavigationError as exc:
                            security_error = exc
                access_token_home_restored = (
                    access_token_extraction is not None
                    and access_token_extraction.homepage_restored
                ) or (
                    access_token_error is not None
                    and access_token_error.homepage_restored
                )
                if self.hold_seconds and (
                    result.next_step != "verification"
                    or security_navigation is not None
                    or (
                        profile_completion is not None
                        and not SECURITY_NAVIGATION_ENABLED
                        and access_token_home_restored
                    )
                ):
                    await self.hold_sleep(self.hold_seconds)
                return ProbeAttemptResult(
                    result,
                    password_submission,
                    verification,
                    verification_submission,
                    profile_completion,
                    account_id,
                    access_token_extraction,
                    access_token_error,
                    access_token_updated_at,
                    plan_check_result,
                    plan_check_error,
                    totp_enrollment,
                    totp_error,
                    password_setup,
                    password_setup_error,
                    security_navigation,
                    security_error,
                    opened.recovered,
                    opened.recovery_elapsed_ms,
                )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if dir_id is not None:
                try:
                    await self._cleanup_browser(
                        roxy,
                        workspace_id,
                        dir_id,
                        observe_delayed_open=open_requested and not open_completed,
                    )
                except ProbeFailure:
                    if primary_error is None or (
                        isinstance(primary_error, ProbeFailure)
                        and primary_error.code == "roxy_browser_not_ready"
                    ):
                        raise

    def _write_attempt_failure(
        self,
        workspace_id: int,
        proxy_id: str,
        attempts: int,
    ) -> None:
        latest_error = self.attempt_errors[-1]
        self.artifacts.write_result(
            {
                "status": "running",
                "code": "proxy_attempt_failed",
                "probeId": self.probe_id,
                "proxyId": proxy_id,
                "workspaceId": workspace_id,
                "emailId": self.email_id,
                "message": latest_error["message"],
                "headless": self.settings.headless,
                "attempts": attempts,
                "attemptErrors": list(self.attempt_errors),
            }
        )

    def _failure(
        self,
        code: str,
        message: str,
        exit_code: int,
        *,
        proxy_id: str | None = None,
        attempts: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {
            "status": "failed",
            "code": code,
            "probeId": self.probe_id,
            "message": message,
            "headless": self.settings.headless,
            "attemptErrors": list(self.attempt_errors),
        }
        if proxy_id is not None:
            result["proxyId"] = proxy_id
        if self.email_id is not None:
            result["emailId"] = self.email_id
        if attempts is not None:
            result["attempts"] = attempts
        self.artifacts.write_result(result)
        return result, exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AutoRegister 单线程 RoxyBrowser + Playwright 注册入口探测",
    )
    parser.add_argument("--workspace-id", type=int, default=None)
    parser.add_argument("--hold-seconds", type=int, default=0)
    parser.add_argument(
        "--debug-show-code",
        action="store_true",
        help="调试时在终端和 latest.json 显示验证码",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.hold_seconds < 0 or args.hold_seconds > 300:
        result = {
            "status": "failed",
            "code": "invalid_hold_seconds",
            "message": "--hold-seconds 必须为 0 到 300 的整数",
        }
        print(json.dumps(result, ensure_ascii=False))
        return 2
    runner: BrowserProbeRunner | None = None
    try:
        settings = SettingsStore().load()
        runner = BrowserProbeRunner(
            settings,
            workspace_id=args.workspace_id,
            hold_seconds=args.hold_seconds,
            debug_show_code=args.debug_show_code,
        )
        result, exit_code = await runner.run()
    except CorruptSettingsError:
        result = {
            "status": "failed",
            "code": "settings_corrupted",
            "message": "执行设置文件损坏",
        }
        exit_code = 2
    except ProbeFailure as exc:
        result = {
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
        }
        result.update(exc.result_fields())
        exit_code = exc.exit_code
    except RoxyApiError as exc:
        stage = (
            "roxy_health"
            if exc.operation == "health"
            else (
                "roxy_workspace"
                if exc.operation == "workspace_list"
                else "roxy_browser"
            )
        )
        result = {
            "status": "failed",
            "code": "roxy_auth_failed" if exc.is_auth_failure else "roxy_api_failed",
            "message": (
                "Roxy API 拒绝了当前凭据，请重新生成并保存 API Key"
                if exc.is_auth_failure
                else "Roxy API 调用失败，请检查客户端状态"
            ),
            "stage": stage,
            "operation": exc.operation,
            "retryCount": 0,
            "elapsedMs": exc.elapsed_ms,
            "errorKind": exc.error_kind,
        }
        if exc.http_status is not None:
            result["httpStatus"] = exc.http_status
        if exc.api_code is not None:
            result["apiCode"] = exc.api_code
        exit_code = 4
    except CdpConnectionError:
        result = {
            "status": "failed",
            "code": "cdp_connection_failed",
            "message": "Playwright 无法连接 Roxy CDP 端点",
        }
        exit_code = 4
    except MailboxClientError as exc:
        result = {
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
        }
        exit_code = 7
    except EmailStepError as exc:
        result = {
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
        }
        if exc.click_attempts is not None:
            result["emailContinueAttempts"] = exc.click_attempts
        if exc.click_failures is not None:
            result["emailContinueClickFailures"] = exc.click_failures
        if exc.recovery_state is not None:
            result["emailContinueRecoveryState"] = exc.recovery_state
        if exc.attempt_states is not None:
            result["emailContinueAttemptStates"] = list(exc.attempt_states)
        if exc.dispatch_observed is not None:
            result["emailContinueDispatchObserved"] = exc.dispatch_observed
        if exc.exception_types is not None:
            result["emailContinueClickExceptionTypes"] = list(
                exc.exception_types
            )
        if exc.recovery_elapsed_ms is not None:
            result["emailContinueRecoveryElapsedMs"] = exc.recovery_elapsed_ms
        if exc.screenshot_captured is not None:
            result["screenshotCaptured"] = exc.screenshot_captured
        exit_code = 7
    except PasswordStepError as exc:
        result = {
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
        }
        exit_code = 7
    except VerificationStepError as exc:
        result = {
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
        }
        exit_code = 7
    except ProfileStepError as exc:
        result = {
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
            "profileFormVariant": exc.form_variant,
            "profileLocatorStrategy": exc.locator_strategy,
            "profileSubmitVariant": exc.submit_variant,
        }
        exit_code = 7
    except TargetChallengeError:
        result = {
            "status": "failed",
            "code": "target_challenge_detected",
            "message": "检测到人机验证或挑战页，未执行绕过",
        }
        exit_code = 7
    except TargetNotReachedError:
        result = {
            "status": "failed",
            "code": "login_entry_not_found",
            "message": "未找到 ChatGPT 注册入口",
        }
        exit_code = 7
    except (MongoUnavailableError, OSError):
        result = {
            "status": "failed",
            "code": "local_dependency_failed",
            "message": "本地依赖不可用或探测结果无法保存",
        }
        exit_code = 8
    except Exception:
        result = {
            "status": "failed",
            "code": "internal_error",
            "message": "浏览器探测发生未预期错误",
        }
        exit_code = 8
    result.setdefault(
        "attemptErrors",
        list(runner.attempt_errors) if runner is not None else [],
    )
    if runner is not None and runner.email_id is not None:
        result.setdefault("emailId", runner.email_id)
    try:
        ArtifactWriter().write_result(result)
    except OSError:
        pass
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
