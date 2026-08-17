from __future__ import annotations

import asyncio
import ctypes
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from .browser_automation import (
    CdpConnectionError,
    EmailStepError,
    PasswordStepError,
    ProfileStepError,
    TargetChallengeError,
    TargetNotReachedError,
    VerificationStepError,
    mask_ip,
)
from .browser_probe import ArtifactWriter, BrowserProbeRunner, ProbeFailure
from .errors import MongoUnavailableError, ResourceNotFoundError
from .mailbox_client import MailboxClientError
from .mongo_manager import MongoManager
from .roxy_client import RoxyApiError
from .run_store import MongoRunWorkerStore
from .settings_store import StoredExecutionSettings


PROFILE_DIAGNOSTIC_VALUES = {
    "profileFormVariant": frozenset(
        {"numeric_age", "birthday", "already_configured", "unknown"}
    ),
    "profileLocatorStrategy": frozenset(
        {"strict_attributes", "semantic_labels", "account_home", "unresolved"}
    ),
    "profileSubmitVariant": frozenset(
        {
            "finish_creating_account",
            "continue",
            "structural_submit",
            "not_applicable",
            "unresolved",
        }
    ),
}


def _safe_profile_diagnostics(source: Any) -> dict[str, str]:
    diagnostics: dict[str, str] = {}
    for key, allowed in PROFILE_DIAGNOSTIC_VALUES.items():
        value = source.get(key) if isinstance(source, dict) else getattr(source, {
            "profileFormVariant": "form_variant",
            "profileLocatorStrategy": "locator_strategy",
            "profileSubmitVariant": "submit_variant",
        }[key], None)
        if isinstance(value, str) and value in allowed:
            diagnostics[key] = value
    return diagnostics


def _roxy_error_stage(operation: str, fallback_stage: str) -> str:
    if operation == "health":
        return "roxy_health"
    if operation == "workspace_list":
        return "roxy_workspace"
    if operation.startswith("browser_"):
        return "roxy_browser"
    return fallback_stage


def _safe_error_diagnostics(
    exc: BaseException,
    *,
    fallback_stage: str,
) -> dict[str, str | int]:
    diagnostics: dict[str, str | int] = {}
    if isinstance(exc, ProbeFailure):
        diagnostics["errorStage"] = exc.stage or fallback_stage
        if exc.operation is not None:
            diagnostics["errorOperation"] = exc.operation
        if exc.error_kind is not None:
            diagnostics["errorKind"] = exc.error_kind
        if exc.http_status is not None:
            diagnostics["errorHttpStatus"] = exc.http_status
        if exc.api_code is not None:
            diagnostics["errorApiCode"] = exc.api_code
        if exc.retry_count is not None:
            diagnostics["errorRetryCount"] = exc.retry_count
        if exc.elapsed_ms is not None:
            diagnostics["errorElapsedMs"] = exc.elapsed_ms
        return diagnostics
    if isinstance(exc, RoxyApiError):
        diagnostics.update(
            {
                "errorStage": _roxy_error_stage(exc.operation, fallback_stage),
                "errorOperation": exc.operation,
                "errorKind": exc.error_kind,
                "errorRetryCount": 0,
                "errorElapsedMs": exc.elapsed_ms,
            }
        )
        if exc.http_status is not None:
            diagnostics["errorHttpStatus"] = exc.http_status
        if exc.api_code is not None:
            diagnostics["errorApiCode"] = exc.api_code
        return diagnostics
    if isinstance(exc, CdpConnectionError):
        return {
            "errorStage": fallback_stage,
            "errorOperation": "cdp_connect",
            "errorKind": "transport",
        }
    return {"errorStage": fallback_stage}


def _safe_failure(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, ProbeFailure):
        return exc.code, exc.message
    if isinstance(exc, RoxyApiError):
        return (
            "roxy_auth_failed" if exc.is_auth_failure else "roxy_api_failed",
            "Roxy API 凭据无效" if exc.is_auth_failure else "Roxy API 调用失败",
        )
    if isinstance(exc, CdpConnectionError):
        return "cdp_connection_failed", "Playwright 无法连接 Roxy CDP"
    if isinstance(exc, MailboxClientError):
        return exc.code, exc.message
    if isinstance(
        exc,
        (EmailStepError, PasswordStepError, VerificationStepError, ProfileStepError),
    ):
        return exc.code, exc.message
    if isinstance(exc, TargetChallengeError):
        return "target_challenge_detected", "检测到人机验证或挑战页"
    if isinstance(exc, TargetNotReachedError):
        return "login_entry_not_found", "未找到 ChatGPT 注册入口"
    if isinstance(exc, (MongoUnavailableError, ResourceNotFoundError, OSError)):
        return "local_dependency_failed", "本地依赖不可用或资源状态已变化"
    return "internal_error", "浏览器 worker 发生未预期错误"


def _safe_failure_artifact(
    runner: Any,
    exc: BaseException,
    *,
    stage: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "failed",
        "code": code,
        "message": message,
        "stage": stage,
        "probeId": str(runner.probe_id),
        "emailId": str(runner.email_id) if runner.email_id is not None else None,
        "headless": bool(runner.settings.headless),
    }
    result.update(_safe_error_diagnostics(exc, fallback_stage=stage))
    if runner.egress_ip:
        with suppress(ValueError):
            result["egressIpMasked"] = mask_ip(str(runner.egress_ip))

    attempt_errors = getattr(runner, "attempt_errors", None)
    if isinstance(attempt_errors, list) and attempt_errors:
        result["attempts"] = len(attempt_errors)
        result["attemptErrors"] = list(attempt_errors)
        last_proxy_id = attempt_errors[-1].get("proxyId")
        if isinstance(last_proxy_id, str) and last_proxy_id:
            result["proxyId"] = last_proxy_id

    if isinstance(exc, EmailStepError):
        email_diagnostics = {
            "emailContinueAttempts": exc.click_attempts,
            "emailContinueClickFailures": exc.click_failures,
            "emailContinueRecoveryState": exc.recovery_state,
            "emailContinueAttemptStates": (
                list(exc.attempt_states) if exc.attempt_states is not None else None
            ),
            "emailContinueDispatchObserved": exc.dispatch_observed,
            "emailContinueClickExceptionTypes": (
                list(exc.exception_types)
                if exc.exception_types is not None
                else None
            ),
            "emailContinueRecoveryElapsedMs": exc.recovery_elapsed_ms,
            "emailFailureScreenshotCaptured": exc.screenshot_captured,
            "loginChallengeObserved": exc.login_challenge_observed,
            "emailFormReadyWaitMs": exc.email_form_ready_wait_ms,
            "emailPreContinueStableWaitsMs": (
                list(exc.email_pre_continue_stable_waits_ms)
                if exc.email_pre_continue_stable_waits_ms is not None
                else None
            ),
            "emailFormStabilityResetCount": (
                exc.email_form_stability_reset_count
            ),
        }
        result.update(
            {key: value for key, value in email_diagnostics.items() if value is not None}
        )

    if isinstance(exc, VerificationStepError):
        verification_diagnostics = {
            "verificationContinueAttempts": exc.continue_attempts,
            "verificationClickCompleted": exc.click_completed,
            "verificationClickExceptionType": exc.click_exception_type,
            "verificationPostClickState": exc.post_click_state,
            "verificationWaitElapsedMs": exc.wait_elapsed_ms,
            "verificationUrlChanged": exc.url_changed,
            "verificationInputVisibleAtEnd": exc.input_visible_at_end,
            "verificationButtonVisibleAtEnd": exc.button_visible_at_end,
        }
        result.update(
            {
                key: value
                for key, value in verification_diagnostics.items()
                if value is not None
            }
        )

    if isinstance(exc, ProfileStepError):
        result.update(_safe_profile_diagnostics(exc))

    if isinstance(exc, TargetChallengeError):
        if exc.stage is not None:
            result["challengeStage"] = exc.stage
        if exc.wait_ms is not None:
            result["challengeWaitMs"] = exc.wait_ms
        challenge_diagnostics = {
            "loginChallengeObserved": exc.login_challenge_observed,
            "emailFormReadyWaitMs": exc.email_form_ready_wait_ms,
            "emailPreContinueStableWaitsMs": (
                list(exc.email_pre_continue_stable_waits_ms)
                if exc.email_pre_continue_stable_waits_ms is not None
                else None
            ),
            "emailFormStabilityResetCount": (
                exc.email_form_stability_reset_count
            ),
        }
        result.update(
            {
                key: value
                for key, value in challenge_diagnostics.items()
                if value is not None
            }
        )
    return result


def _write_safe_failure_artifact(
    runner: Any,
    exc: BaseException,
    *,
    stage: str,
    code: str,
    message: str,
) -> bool:
    try:
        runner.artifacts.write_result(
            _safe_failure_artifact(
                runner,
                exc,
                stage=stage,
                code=code,
                message=message,
            )
        )
    except Exception:
        return False
    return True


async def _run_worker(
    config: dict[str, Any],
    event_queue: Any,
    cancel_event: Any,
) -> None:
    current_stage = "queued"

    async def progress(stage: str, details: dict[str, Any]) -> None:
        nonlocal current_stage
        if stage != "cleanup":
            current_stage = stage
        event: dict[str, Any] = {
            "type": "stage",
            "runId": config["runId"],
            "workerId": config["workerId"],
            "stage": stage,
        }
        # dirId is internal cleanup state; egressIp is returned only by the
        # authenticated local worker endpoint. Neither is written by the child.
        if details.get("dirId"):
            event["dirId"] = str(details["dirId"])
        if details.get("egressIp"):
            event["egressIp"] = str(details["egressIp"])
        event_queue.put(event)
        with suppress(Exception):
            await worker_store.stage(
                str(config["runId"]),
                str(config["workerId"]),
                stage,
                egress_ip=(
                    str(details["egressIp"])
                    if details.get("egressIp")
                    else None
                ),
                dir_id=(
                    str(details["dirId"])
                    if details.get("dirId")
                    else None
                ),
            )

    settings = StoredExecutionSettings.model_validate(config["settings"])
    mongo = MongoManager(
        uri=str(config["mongoUri"]),
        database_name=str(config["mongoDatabase"]),
    )
    worker_store = MongoRunWorkerStore(mongo)
    runner = BrowserProbeRunner(
        settings,
        workspace_id=int(config["workspaceId"]),
        hold_seconds=0,
        artifact_writer=ArtifactWriter(Path(config["artifactDir"])),
        mongo_manager=mongo,
        reserved_email_id=str(config["emailId"]),
        reservation_owner=str(config["runId"]),
        controller_lock_enabled=False,
        worker_owner=str(config["leaseOwner"]),
        workspace_lease_preacquired=False,
        progress_callback=progress,
        registration_country=str(config.get("registrationCountry") or "") or None,
        registration_proxy_group=str(config.get("registrationProxyGroup") or "") or None,
    )
    task = asyncio.create_task(runner.run(), name=f"probe-{config['workerId']}")
    cancelled = False
    while not task.done():
        if cancel_event.is_set():
            cancelled = True
            task.cancel()
            break
        await asyncio.sleep(0.25)

    if cancelled:
        with suppress(BaseException):
            await task
        event_queue.put(
            {
                "type": "final",
                "runId": config["runId"],
                "workerId": config["workerId"],
                "status": "cancelled",
                "code": "worker_cancelled",
                "egressIp": runner.egress_ip,
            }
        )
        return

    diagnostics: dict[str, str | int] = {}
    try:
        result, _exit_code = await task
    except asyncio.CancelledError:
        status = "cancelled"
        code = "worker_cancelled"
    except BaseException as exc:
        code, message = _safe_failure(exc)
        status = "failed"
        diagnostics = _safe_error_diagnostics(exc, fallback_stage=current_stage)
        diagnostics.update(_safe_profile_diagnostics(exc))
        _write_safe_failure_artifact(
            runner,
            exc,
            stage=current_stage,
            code=code,
            message=message,
        )
    else:
        raw_status = str(result.get("status") or "failed")
        status = raw_status if raw_status in {"success", "partial_success"} else "failed"
        code = str(result.get("code") or "probe_failed")
        diagnostics.update(_safe_profile_diagnostics(result))

    final_event: dict[str, Any] = {
            "type": "final",
            "runId": config["runId"],
            "workerId": config["workerId"],
            "status": status,
            "code": code,
            "egressIp": runner.egress_ip,
        }
    final_event.update(diagnostics)
    event_queue.put(final_event)


def worker_process_main(
    config: dict[str, Any],
    event_queue: Any,
    cancel_event: Any,
) -> None:
    """Windows-spawn entry point. Secrets are passed via process memory, not argv."""
    try:
        asyncio.run(_run_worker(config, event_queue, cancel_event))
    except BaseException:
        # Never let a child traceback serialize settings, tokens, URLs, or DOM
        # state to the parent service console.
        with suppress(Exception):
            event_queue.put(
                {
                    "type": "final",
                    "runId": config.get("runId"),
                    "workerId": config.get("workerId"),
                    "status": "failed",
                    "code": "worker_bootstrap_failed",
                }
            )


class WindowsChildJob:
    """Best-effort kill-on-parent-exit Job Object for spawned browser workers."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return
        self._handle = int(handle)

    def assign(self, pid: int) -> bool:
        if self._handle is None or os.name != "nt":
            return False
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        process = kernel32.OpenProcess(
            self.PROCESS_TERMINATE | self.PROCESS_SET_QUOTA,
            False,
            pid,
        )
        if not process:
            return False
        try:
            return bool(
                kernel32.AssignProcessToJobObject(
                    ctypes.c_void_p(self._handle), ctypes.c_void_p(process)
                )
            )
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(process))

    def close(self) -> None:
        if self._handle is None or os.name != "nt":
            return
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        self._handle = None
