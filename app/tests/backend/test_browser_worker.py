from __future__ import annotations

import json
from types import SimpleNamespace

from backend.browser_automation import (
    EmailStepError,
    ProfileStepError,
    TargetChallengeError,
    VerificationStepError,
)
from backend.browser_probe import ArtifactWriter
from backend.browser_probe import ProbeFailure
from backend.browser_worker import (
    _safe_failure,
    _write_safe_failure_artifact,
)


def fake_runner(tmp_path):
    return SimpleNamespace(
        probe_id="probe-id",
        email_id="email-id",
        egress_ip="203.0.113.45",
        settings=SimpleNamespace(headless=False),
        artifacts=ArtifactWriter(tmp_path),
    )


def test_worker_writes_redacted_verification_failure_artifact(tmp_path) -> None:
    error = VerificationStepError(
        "verification_form_unchanged_after_click",
        "验证码 Continue 点击后表单在 45 秒内未发生变化",
        continue_attempts=1,
        click_completed=True,
        click_exception_type=None,
        post_click_state="form_unchanged",
        wait_elapsed_ms=45_000,
        url_changed=False,
        input_visible_at_end=True,
        button_visible_at_end=True,
    )
    code, message = _safe_failure(error)

    assert _write_safe_failure_artifact(
        fake_runner(tmp_path),
        error,
        stage="verification",
        code=code,
        message=message,
    ) is True

    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload == {
        "status": "failed",
        "code": "verification_form_unchanged_after_click",
        "message": "验证码 Continue 点击后表单在 45 秒内未发生变化",
        "stage": "verification",
        "errorStage": "verification",
        "probeId": "probe-id",
        "emailId": "email-id",
        "headless": False,
        "egressIpMasked": "203.0.*.*",
        "verificationContinueAttempts": 1,
        "verificationClickCompleted": True,
        "verificationPostClickState": "form_unchanged",
        "verificationWaitElapsedMs": 45_000,
        "verificationUrlChanged": False,
        "verificationInputVisibleAtEnd": True,
        "verificationButtonVisibleAtEnd": True,
    }
    raw = (tmp_path / "latest.json").read_text(encoding="utf-8")
    for forbidden in (
        "person@example.com",
        "222222",
        "accessToken",
        "sessionToken",
        "ws://",
        "PRIVATE_RAW_EXCEPTION",
    ):
        assert forbidden not in raw


def test_worker_profile_failure_diagnostics_are_allowlisted_and_redacted(tmp_path) -> None:
    error = ProfileStepError(
        "profile_birthday_fill_failed",
        "生日填写失败",
        form_variant="birthday",
        locator_strategy="semantic_labels",
        submit_variant="continue",
    )
    code, message = _safe_failure(error)
    assert _write_safe_failure_artifact(
        fake_runner(tmp_path),
        error,
        stage="profile",
        code=code,
        message=message,
    ) is True
    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["profileFormVariant"] == "birthday"
    assert payload["profileLocatorStrategy"] == "semantic_labels"
    assert payload["profileSubmitVariant"] == "continue"
    raw = json.dumps(payload)
    assert "1995-08-10" not in raw
    assert "Private ProfileName" not in raw


def test_worker_failure_artifact_covers_email_and_challenge(tmp_path) -> None:
    runner = fake_runner(tmp_path)
    email_error = EmailStepError(
        "email_input_missing",
        "未找到可用的邮箱输入框",
        click_attempts=0,
        click_failures=0,
        recovery_state="challenge_wait",
        login_challenge_observed=True,
        email_form_ready_wait_ms=5_500,
        email_pre_continue_stable_waits_ms=(6_000,),
        email_form_stability_reset_count=1,
    )
    code, message = _safe_failure(email_error)
    assert _write_safe_failure_artifact(
        runner,
        email_error,
        stage="login",
        code=code,
        message=message,
    ) is True
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))[
        "emailContinueRecoveryState"
    ] == "challenge_wait"
    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["loginChallengeObserved"] is True
    assert payload["emailFormReadyWaitMs"] == 5_500
    assert payload["emailPreContinueStableWaitsMs"] == [6_000]
    assert payload["emailFormStabilityResetCount"] == 1

    challenge = TargetChallengeError(
        "Cloudflare 挑战未在 60 秒内自动完成",
        stage="login",
        wait_ms=60_000,
        login_challenge_observed=True,
        email_form_ready_wait_ms=60_000,
        email_pre_continue_stable_waits_ms=(),
        email_form_stability_reset_count=0,
    )
    code, message = _safe_failure(challenge)
    assert _write_safe_failure_artifact(
        runner,
        challenge,
        stage="login",
        code=code,
        message=message,
    ) is True
    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["code"] == "target_challenge_detected"
    assert payload["challengeStage"] == "login"
    assert payload["challengeWaitMs"] == 60_000
    assert payload["loginChallengeObserved"] is True
    assert payload["emailFormReadyWaitMs"] == 60_000


def test_worker_failure_artifact_preserves_proxy_rotation_history(tmp_path) -> None:
    runner = fake_runner(tmp_path)
    runner.attempt_errors = [
        {
            "attempt": 1,
            "proxyId": "proxy-1",
            "stage": "email_submission",
            "code": "email_post_submit_reset",
            "message": "邮箱提交后被网页重复重置",
            "exceptionType": "EmailStepError",
        },
        {
            "attempt": 2,
            "proxyId": "proxy-2",
            "stage": "email_submission",
            "code": "email_post_submit_reset",
            "message": "邮箱提交后被网页重复重置",
            "exceptionType": "EmailStepError",
        },
    ]
    error = EmailStepError(
        "email_post_submit_reset",
        "邮箱提交后被网页重复重置",
    )
    code, message = _safe_failure(error)

    assert _write_safe_failure_artifact(
        runner,
        error,
        stage="login",
        code=code,
        message=message,
    ) is True

    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["attempts"] == 2
    assert [item["proxyId"] for item in payload["attemptErrors"]] == [
        "proxy-1",
        "proxy-2",
    ]
    assert payload["proxyId"] == "proxy-2"


def test_worker_artifact_write_failure_does_not_raise() -> None:
    class FailingArtifacts:
        def write_result(self, _result) -> None:
            raise PermissionError("PRIVATE_RAW_EXCEPTION")

    runner = SimpleNamespace(
        probe_id="probe-id",
        email_id="email-id",
        egress_ip=None,
        settings=SimpleNamespace(headless=True),
        artifacts=FailingArtifacts(),
    )
    error = VerificationStepError(
        "verification_continue_click_failed",
        "验证码页面 Continue 点击失败",
    )
    code, message = _safe_failure(error)

    assert _write_safe_failure_artifact(
        runner,
        error,
        stage="verification",
        code=code,
        message=message,
    ) is False


def test_worker_writes_only_redacted_roxy_diagnostics(tmp_path) -> None:
    error = ProbeFailure(
        "roxy_workspace_not_ready",
        "safe message",
        4,
        stage="roxy_workspace",
        operation="workspace_list",
        retry_count=12,
        elapsed_ms=30_000,
        http_status=503,
        api_code=901,
        error_kind="api",
    )
    code, message = _safe_failure(error)

    assert _write_safe_failure_artifact(
        fake_runner(tmp_path),
        error,
        stage="cleanup",
        code=code,
        message=message,
    ) is True

    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["stage"] == "cleanup"
    assert payload["errorStage"] == "roxy_workspace"
    assert payload["errorOperation"] == "workspace_list"
    assert payload["errorKind"] == "api"
    assert payload["errorHttpStatus"] == 503
    assert payload["errorApiCode"] == 901
    assert payload["errorRetryCount"] == 12
    assert payload["errorElapsedMs"] == 30_000
    raw = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("password", "token", "ws://", "PRIVATE_RESPONSE_BODY"):
        assert forbidden not in raw
