from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .chatgpt_plan import (
    ACCOUNTS_CHECK_PATH,
    ACCOUNTS_CHECK_URL,
    PLAN_RESPONSE_MAX_BYTES,
    AccountPlanResult,
    PlanCheckError,
    parse_accounts_check,
    plan_request_headers,
    normalize_access_token,
)
from .checkout_type import (
    CheckoutTypeCheckError,
    CheckoutTypeResult,
    parse_checkout_type_response,
)
from .totp import TotpSecretError, generate_totp, normalize_totp_secret


IP_CHECK_URL = "https://api64.ipify.org?format=json"
CHATGPT_LOGIN_URL = "https://chatgpt.com/auth/login?openaicom_referred=true"
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session"
CHATGPT_PLAN_URL = f"{ACCOUNTS_CHECK_URL}?timezone_offset_min=-480"
CHATGPT_CHECKOUT_PATH = "/backend-api/payments/checkout"
CHATGPT_CHECKOUT_URL = f"https://chatgpt.com{CHATGPT_CHECKOUT_PATH}"
CHATGPT_MFA_ENROLL_URL = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
CHATGPT_MFA_ACTIVATE_URL = (
    "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
)
CHATGPT_MODELS_URL = "https://chatgpt.com/backend-api/models"
SESSION_RESPONSE_MAX_BYTES = 65_536
ACCESS_TOKEN_MAX_CHARS = 16_384
VERIFICATION_PATTERN = re.compile(
    r"check\s+your\s+email|verification\s+code|verify\s+your\s+email|"
    r"验证码|驗證碼|验证邮箱|驗證郵箱|"
    r"メール(?:アドレス)?を確認|認証コード|確認コード|メール認証|"
    r"gelen\s+kutunu\s+kontrol\s+et|doğrulama\s+kodu|"
    r"doğrulama\s+kodunu\s+gir",
    re.IGNORECASE,
)
EMAIL_VERIFIED_PATTERN = re.compile(
    r"email(?:\s+address)?\s+(?:has\s+been\s+)?verified|"
    r"email(?:\s+address)?\s+is\s+already\s+verified|"
    r"邮箱(?:地址)?(?:已|已经)验证|郵箱(?:地址)?(?:已|已經)驗證|"
    r"メール(?:アドレス)?(?:は)?(?:すでに)?確認済み|メールが確認されました|"
    r"이메일(?:\s+주소)?(?:가)?\s*(?:이미\s*)?확인",
    re.IGNORECASE,
)
AUTHENTICATOR_FACTOR_PATTERN = re.compile(
    r"authenticator(?:\s+app)?|authentication\s+app|verification\s+app|\btotp\b|"
    r"验证器|身份验证器|動態口令|动态口令|認証アプリ|ワンタイム認証コード|인증\s*앱",
    re.IGNORECASE,
)
EMAIL_REJECTION_PATTERN = re.compile(
    r"invalid\s+email|"
    r"email(?:\s+address)?\s+(?:is\s+)?(?:invalid|unsupported|not\s+supported|not\s+allowed|not\s+accepted)|"
    r"(?:cannot|can't|could\s+not|couldn't|unable\s+to)\s+(?:use\s+(?:this\s+)?email|continue|sign\s+up)|"
    r"邮箱(?:地址)?(?:无效|不受支持|不可用|不允许|未被接受)|"
    r"无法(?:使用.*邮箱|继续|注册)|"
    r"(?:無効|使用できない|利用できない).*(?:メール|メールアドレス)|"
    r"(?:メール|メールアドレス).*(?:無効|使用できません|利用できません)|"
    r"続行できません|登録できません",
    re.IGNORECASE,
)
VERIFICATION_REJECTION_PATTERN = re.compile(
    r"invalid\s+(?:verification\s+)?code|"
    r"incorrect\s+(?:verification\s+)?code|"
    r"wrong\s+(?:verification\s+)?code|"
    r"(?:verification\s+)?code\s+(?:is\s+)?(?:invalid|incorrect|expired)|"
    r"验证码(?:无效|错误|不正确|已过期)|驗證碼(?:無效|錯誤|不正確|已過期)|"
    r"(?:認証|確認)?コード.*(?:無効|正しくありません|間違|期限切れ)|"
    r"(?:無効|正しくない|期限切れ).*(?:認証|確認)?コード|"
    r"(?:geçersiz|yanlış|hatalı|süresi\s+dolmuş).{0,40}doğrulama\s+kodu|"
    r"doğrulama\s+kodu.{0,40}(?:geçersiz|yanlış|hatalı|süresi\s+dolmuş)",
    re.IGNORECASE,
)
PASSWORD_REJECTION_PATTERN = re.compile(
    r"password.{0,80}(?:required|invalid|too\s+short|must\s+contain)|"
    r"(?:required|invalid|too\s+short).{0,40}password|"
    r"パスワード.{0,80}(?:必要|無効|短すぎ|含め)|"
    r"(?:şifre|parola).{0,80}(?:gerekli|geçersiz|çok\s+kısa|içermeli)",
    re.IGNORECASE,
)
PASSWORD_ROUTE_SELECTOR = (
    "xpath=//*[self::button or self::a or @role='button' or @role='link']["
    "contains(translate(normalize-space(.), "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'password') "
    "or contains(normalize-space(.), 'パスワード') "
    "or contains(normalize-space(.), '密码') "
    "or contains(normalize-space(.), '密碼') "
    "or contains(normalize-space(.), '비밀번호') "
    "or contains(translate(normalize-space(.), 'Şİ', 'şi'), 'şifre') "
    "or contains(translate(normalize-space(.), "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'parola') "
    "or contains(translate(normalize-space(.), "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'passwort')"
    "]"
)
PROFILE_SUBMIT_PATTERN = re.compile(
    r"^\s*(?:finish\s+creating\s+account|continue|続行|"
    r"アカウント(?:の)?作成(?:を完了)?|登録(?:を完了)?|完了|"
    r"devam\s+et|hesap(?:\s+oluşturmayı)?\s+tamamla|bitir)\s*$",
    re.IGNORECASE,
)
PROFILE_NAME_LABEL_PATTERN = re.compile(
    r"^\s*(?:full\s+name|氏名|名前|フルネーム|ad(?:\s+soyad)?|tam\s+ad)\s*$",
    re.IGNORECASE,
)
PROFILE_AGE_LABEL_PATTERN = re.compile(
    r"^\s*(?:age|年齢|yaş)\s*$", re.IGNORECASE
)
PROFILE_BIRTHDAY_LABEL_PATTERN = re.compile(
    r"^\s*(?:birthday|date\s+of\s+birth|生年月日|誕生日|doğum\s+tarihi)\s*$",
    re.IGNORECASE,
)
PROFILE_REJECTION_PATTERN = re.compile(
    r"invalid\s+(?:full\s+)?name|"
    r"(?:enter|provide)\s+(?:your\s+)?(?:full\s+)?name|"
    r"invalid\s+age|"
    r"age\s+(?:is\s+)?(?:required|invalid|not\s+valid)|"
    r"(?:must|need\s+to)\s+be\s+at\s+least|"
    r"(?:unable|failed)\s+to\s+(?:create|finish)|"
    r"something\s+went\s+wrong|"
    r"(?:氏名|名前|年齢|生年月日).*(?:無効|入力してください|正しくありません)|"
    r"アカウント.*(?:作成できません|作成に失敗)|問題が発生しました|"
    r"(?:ad|soyad|yaş|doğum\s+tarihi).{0,60}(?:geçersiz|gerekli|zorunlu)|"
    r"hesap.{0,40}(?:oluşturulamadı|tamamlanamadı)|bir\s+şeyler\s+yanlış\s+gitti",
    re.IGNORECASE,
)
ENGLISH_FIRST_NAMES = (
    "James",
    "Daniel",
    "Michael",
    "David",
    "Thomas",
    "William",
    "Joseph",
    "Charles",
    "Andrew",
    "Henry",
    "Emily",
    "Olivia",
    "Sophia",
    "Emma",
    "Ava",
    "Mia",
    "Grace",
    "Chloe",
    "Lily",
    "Ella",
)
ENGLISH_LAST_NAMES = (
    "Smith",
    "Johnson",
    "Brown",
    "Taylor",
    "Anderson",
    "Thomas",
    "Jackson",
    "White",
    "Harris",
    "Martin",
    "Thompson",
    "Moore",
    "Clark",
    "Lewis",
    "Walker",
    "Hall",
    "Young",
    "King",
    "Wright",
    "Green",
)
CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "checking your browser",
    "cloudflare",
    "verifying...",
    "verifying…",
    "just a moment...",
    "just a moment…",
    "人机验证",
    "驗證您是人類",
    "人間であることを確認",
    "ブラウザを確認しています",
    "しばらくお待ちください",
)
CHALLENGE_SELECTOR = (
    'iframe[src*="challenges.cloudflare.com" i], '
    'iframe[title*="challenge" i], '
    '.cf-turnstile, [class*="cf-turnstile"], '
    '[data-sitekey][data-callback], [data-cf-challenge]'
)
LOGIN_CHALLENGE_WAIT_SECONDS = 60.0
CHALLENGE_POLL_INTERVAL_SECONDS = 0.5
EMAIL_FORM_STABILITY_SECONDS = 5.0
NET_ERROR_PATTERN = re.compile(r"\bnet::ERR_[A-Z0-9_]+\b", re.IGNORECASE)
SAFE_EXCEPTION_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
EMAIL_POST_SUBMIT_RESET_CONFIRMATION_SECONDS = 2.0
EMAIL_POST_SUBMIT_STABLE_FORM_CONFIRMATION_SECONDS = 1.0
PROFILE_PATHS = frozenset(
    {
        "/about-you",
        "/create-account/profile",
        "/u/signup/profile",
        "/signup/profile",
    }
)
AUTH_RETRY_ACTION_PATTERN = re.compile(
    r"^\s*(?:try\s+again|retry|再试一次|重试|再試一次|重試|もう一度|再試行|"
    r"tekrar\s+dene|yeniden\s+dene|önce\s+dene)\s*$",
    re.IGNORECASE,
)
AUTH_RETRY_PAGE_PATTERN = re.compile(
    r"try\s+again|retry|再试一次|重试|再試一次|重試|もう一度|再試行|"
    r"tekrar\s+dene|yeniden\s+dene|önce\s+dene",
    re.IGNORECASE,
)
PASSKEY_ENROLL_HOST = "auth.openai.com"
PASSKEY_ENROLL_PATH = "/passkey-enroll"
PASSKEY_REDIRECT_HOSTS = frozenset({"auth.openai.com", "chatgpt.com"})
CHATGPT_PASSKEY_SETTINGS_URL = "https://chatgpt.com/#settings/Security/passkeys"


class CdpConnectionError(RuntimeError):
    pass


class ProxyNavigationError(RuntimeError):
    def __init__(
        self,
        *,
        stage: str,
        code: str,
        message: str,
        exception_type: str,
        elapsed_ms: int,
        timeout_ms: int | None,
        net_error: str | None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.exception_type = exception_type
        self.elapsed_ms = elapsed_ms
        self.timeout_ms = timeout_ms
        self.net_error = net_error

    def as_attempt_error(self, *, attempt: int, proxy_id: str) -> dict[str, Any]:
        return {
            "attempt": attempt,
            "proxyId": proxy_id,
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "exceptionType": self.exception_type,
            "netError": self.net_error,
            "elapsedMs": self.elapsed_ms,
            "timeoutMs": self.timeout_ms,
        }


class EmailStepError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        click_attempts: int | None = None,
        click_failures: int | None = None,
        recovery_state: str | None = None,
        attempt_states: tuple[str, ...] | None = None,
        dispatch_observed: bool | None = None,
        exception_types: tuple[str, ...] | None = None,
        recovery_elapsed_ms: int | None = None,
        screenshot_captured: bool | None = None,
        login_challenge_observed: bool | None = None,
        email_form_ready_wait_ms: int | None = None,
        email_pre_continue_stable_waits_ms: tuple[int, ...] | None = None,
        email_form_stability_reset_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.click_attempts = click_attempts
        self.click_failures = click_failures
        self.recovery_state = recovery_state
        self.attempt_states = attempt_states
        self.dispatch_observed = dispatch_observed
        self.exception_types = exception_types
        self.recovery_elapsed_ms = recovery_elapsed_ms
        self.screenshot_captured = screenshot_captured
        self.login_challenge_observed = login_challenge_observed
        self.email_form_ready_wait_ms = email_form_ready_wait_ms
        self.email_pre_continue_stable_waits_ms = (
            email_pre_continue_stable_waits_ms
        )
        self.email_form_stability_reset_count = email_form_stability_reset_count


class VerificationStepError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        continue_attempts: int | None = None,
        click_completed: bool | None = None,
        click_exception_type: str | None = None,
        post_click_state: str | None = None,
        wait_elapsed_ms: int | None = None,
        url_changed: bool | None = None,
        input_visible_at_end: bool | None = None,
        button_visible_at_end: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.continue_attempts = continue_attempts
        self.click_completed = click_completed
        self.click_exception_type = click_exception_type
        self.post_click_state = post_click_state
        self.wait_elapsed_ms = wait_elapsed_ms
        self.url_changed = url_changed
        self.input_visible_at_end = input_visible_at_end
        self.button_visible_at_end = button_visible_at_end

    def with_click_diagnostics(
        self,
        *,
        click_completed: bool,
        click_exception_type: str | None,
        continue_attempts: int = 1,
    ) -> "VerificationStepError":
        code = self.code
        message = self.message
        if code == "verification_form_unchanged_after_click" and not click_completed:
            code = "verification_continue_click_failed"
            message = "验证码页面 Continue 点击失败且页面未进入下一步"
        return VerificationStepError(
            code,
            message,
            continue_attempts=continue_attempts,
            click_completed=click_completed,
            click_exception_type=click_exception_type,
            post_click_state=self.post_click_state,
            wait_elapsed_ms=self.wait_elapsed_ms,
            url_changed=self.url_changed,
            input_visible_at_end=self.input_visible_at_end,
            button_visible_at_end=self.button_visible_at_end,
        )


class PasswordStepError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProfileStepError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        form_variant: str = "unknown",
        locator_strategy: str = "unresolved",
        submit_variant: str = "unresolved",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.form_variant = form_variant
        self.locator_strategy = locator_strategy
        self.submit_variant = submit_variant


class SecurityNavigationError(RuntimeError):
    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        redirect_state: str | None = None,
        redirect_poll_count: int | None = None,
        redirect_elapsed_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.redirect_state = redirect_state
        self.redirect_poll_count = redirect_poll_count
        self.redirect_elapsed_ms = redirect_elapsed_ms


class TotpEnrollmentError(RuntimeError):
    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.http_status = http_status


class AccessTokenExtractionError(RuntimeError):
    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        homepage_restored: bool = False,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.homepage_restored = homepage_restored


class TargetChallengeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        wait_ms: int | None = None,
        login_challenge_observed: bool | None = None,
        email_form_ready_wait_ms: int | None = None,
        email_pre_continue_stable_waits_ms: tuple[int, ...] | None = None,
        email_form_stability_reset_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.wait_ms = wait_ms
        self.login_challenge_observed = login_challenge_observed
        self.email_form_ready_wait_ms = email_form_ready_wait_ms
        self.email_pre_continue_stable_waits_ms = (
            email_pre_continue_stable_waits_ms
        )
        self.email_form_stability_reset_count = email_form_stability_reset_count


class TargetNotReachedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AutomationResult:
    egress_ip_masked: str
    final_url: str
    latency_ms: int
    next_step: str
    pre_continue_delay_ms: int
    submitted_at_utc: datetime
    email_fill_attempts: int = 1
    email_form_reset_count: int = 0
    email_pre_fill_delays_ms: tuple[int, ...] = ()
    email_continue_attempts: int = 1
    email_post_submit_reset_count: int = 0
    email_pre_continue_delays_ms: tuple[int, ...] = ()
    email_continue_click_failures: int = 0
    email_continue_recovery_state: str | None = None
    email_continue_attempt_states: tuple[str, ...] = ()
    email_continue_dispatch_observed: bool = False
    email_continue_click_exception_types: tuple[str, ...] = ()
    email_continue_recovery_elapsed_ms: int = 0
    login_challenge_observed: bool = False
    email_form_ready_wait_ms: int = 0
    email_pre_continue_stable_waits_ms: tuple[int, ...] = ()
    email_form_stability_reset_count: int = 0
    # Kept out of repr and public probe artifacts. The controller forwards it
    # only to the worker snapshot endpoint requested by the local UI.
    egress_ip: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class VerificationSubmitResult:
    final_url: str
    next_step: str
    pre_continue_delay_ms: int
    submitted_at_utc: datetime
    continue_attempts: int = 1
    click_completed: bool = True
    click_exception_type: str | None = None
    post_click_state: str = "transitioned"
    wait_elapsed_ms: int = 0
    url_changed: bool = True
    input_visible_at_end: bool = False
    button_visible_at_end: bool = False


@dataclass(frozen=True, slots=True)
class PasswordSubmitResult:
    final_url: str
    next_step: str
    pre_continue_delay_ms: int
    submitted_at_utc: datetime
    click_completed: bool = True
    click_exception_type: str | None = None


@dataclass(frozen=True, slots=True)
class PasswordSetupResult:
    final_url: str
    configured_at_utc: datetime
    email_reauth_used: bool
    totp_reauth_used: bool


@dataclass(frozen=True, slots=True)
class ProfileCompletionResult:
    final_url: str
    next_step: str
    skipped: bool
    skip_reason: str | None
    full_name: str | None
    age: int | None
    name_to_age_delay_ms: int | None
    age_to_finish_delay_ms: int | None
    submitted_at_utc: datetime | None
    form_variant: str = "unknown"
    locator_strategy: str = "unresolved"
    submit_variant: str = "unresolved"


@dataclass(frozen=True, slots=True)
class _ProfileFormControls:
    name_input: Any
    second_input: Any
    form: Any
    variant: str
    locator_strategy: str
    birthday_segments: tuple[Any, Any, Any] | None = None
    birthday_value_input: Any | None = None


@dataclass(frozen=True, slots=True)
class SecurityNavigationResult:
    final_url: str
    delays_ms: tuple[int, ...]
    requested_at_utc: datetime
    opened_new_page: bool
    navigation_mode: str = "direct_settings"
    redirect_state: str = "final_direct"
    redirect_poll_count: int = 1
    redirect_elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class AccessTokenExtractionResult:
    access_token: str = field(repr=False)
    expires_at_utc: datetime
    extracted_at_utc: datetime
    final_url: str
    homepage_restored: bool


@dataclass(frozen=True, slots=True)
class TotpEnrollmentChallenge:
    requested_at_utc: datetime
    final_url: str


@dataclass(frozen=True, slots=True)
class TotpEnrollmentResult:
    secret: str = field(repr=False)
    access_token: str = field(repr=False)
    access_token_expires_at_utc: datetime
    activated_at_utc: datetime
    final_url: str


@dataclass(frozen=True, slots=True)
class _SecuritySetupWaitResult:
    page: Any
    opened_new_page: bool
    redirect_state: str
    redirect_poll_count: int
    redirect_elapsed_ms: int


@dataclass(frozen=True, slots=True)
class _VerificationWaitResult:
    next_step: str
    post_click_state: str
    wait_elapsed_ms: int
    url_changed: bool
    input_visible_at_end: bool
    button_visible_at_end: bool


@dataclass(frozen=True, slots=True)
class _EmailFormStabilityResult:
    status: str
    wait_ms: int
    challenge_observed: bool
    stability_reset_count: int
    next_step: str | None = None


def mask_ip(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.version == 4:
        parts = value.split(".")
        return f"{parts[0]}.{parts[1]}.*.*"
    parts = address.exploded.split(":")
    return f"{parts[0]}:{parts[1]}:*"


def sanitize_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def contains_challenge(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def _safe_exception_type(error: Exception) -> str:
    name = type(error).__name__
    return name if SAFE_EXCEPTION_TYPE_PATTERN.fullmatch(name) else "Exception"


def _safe_net_error(error: Exception) -> str | None:
    match = NET_ERROR_PATTERN.search(str(error))
    if match is None:
        return None
    return f"net::{match.group(0).split('::', 1)[1].upper()}"


def _is_timeout(error: Exception) -> bool:
    return isinstance(error, TimeoutError) or type(error).__name__.casefold() == "timeouterror"


def _navigation_failure(
    error: Exception,
    *,
    stage: str,
    failed_code: str,
    timeout_code: str,
    failed_message: str,
    timeout_message: str,
    started: float,
    timeout_ms: int,
) -> ProxyNavigationError:
    timed_out = _is_timeout(error)
    return ProxyNavigationError(
        stage=stage,
        code=timeout_code if timed_out else failed_code,
        message=timeout_message if timed_out else failed_message,
        exception_type=_safe_exception_type(error),
        elapsed_ms=max(0, int((monotonic() - started) * 1000)),
        timeout_ms=timeout_ms,
        net_error=_safe_net_error(error),
    )


def _stage_failure(
    error: Exception,
    *,
    stage: str,
    code: str,
    message: str,
    started: float,
    timeout_ms: int | None,
) -> ProxyNavigationError:
    return ProxyNavigationError(
        stage=stage,
        code=code,
        message=message,
        exception_type=_safe_exception_type(error),
        elapsed_ms=max(0, int((monotonic() - started) * 1000)),
        timeout_ms=timeout_ms,
        net_error=_safe_net_error(error),
    )


class CdpBrowserAutomation:
    def __init__(
        self,
        ws_endpoint: str,
        screenshot_path: Path,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        ip_check_timeout_ms: int = 90_000,
        login_navigation_timeout_ms: int = 90_000,
        email_action_timeout_ms: int = 10_000,
        next_step_timeout_ms: int = 45_000,
        verification_retry_timeout_ms: int = 15_000,
        poll_interval_seconds: float = 0.25,
        pre_fill_delay_min_seconds: float = 3.0,
        pre_fill_delay_max_seconds: float = 5.0,
        email_pre_continue_delay_min_seconds: float = 3.0,
        email_pre_continue_delay_max_seconds: float = 5.0,
        pre_continue_delay_min_seconds: float = 1.0,
        pre_continue_delay_max_seconds: float = 3.0,
        profile_action_delay_min_seconds: float = 1.0,
        profile_action_delay_max_seconds: float = 3.0,
        security_action_delay_min_seconds: float = 4.0,
        security_action_delay_max_seconds: float = 6.0,
        security_action_timeout_ms: int = 10_000,
        security_navigation_timeout_ms: int = 45_000,
        session_navigation_timeout_ms: int = 45_000,
        login_challenge_wait_seconds: float = LOGIN_CHALLENGE_WAIT_SECONDS,
        challenge_poll_interval_seconds: float = CHALLENGE_POLL_INTERVAL_SECONDS,
        email_form_stability_seconds: float = EMAIL_FORM_STABILITY_SECONDS,
        signup_screen_hint: str = "login_or_signup",
        random_uniform: Callable[[float, float], float] | None = None,
        random_choice: Callable[[tuple[str, ...]], str] | None = None,
        random_randint: Callable[[int, int], int] | None = None,
        delay_sleep: Callable[[float], Awaitable[Any]] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            pre_fill_delay_min_seconds < 0
            or pre_fill_delay_max_seconds < pre_fill_delay_min_seconds
        ):
            raise ValueError("邮箱输入前随机等待范围无效")
        if (
            email_pre_continue_delay_min_seconds < 0
            or email_pre_continue_delay_max_seconds
            < email_pre_continue_delay_min_seconds
        ):
            raise ValueError("邮箱提交前随机等待范围无效")
        if (
            pre_continue_delay_min_seconds < 0
            or pre_continue_delay_max_seconds < pre_continue_delay_min_seconds
        ):
            raise ValueError("验证码提交前随机等待范围无效")
        if (
            profile_action_delay_min_seconds < 0
            or profile_action_delay_max_seconds < profile_action_delay_min_seconds
        ):
            raise ValueError("账号资料步骤随机等待范围无效")
        if (
            security_action_delay_min_seconds < 0
            or security_action_delay_max_seconds < security_action_delay_min_seconds
        ):
            raise ValueError("安全设置导航随机等待范围无效")
        if security_action_timeout_ms < 1 or security_navigation_timeout_ms < 1:
            raise ValueError("安全设置导航超时必须大于零")
        if session_navigation_timeout_ms < 1:
            raise ValueError("Session 导航超时必须大于零")
        if verification_retry_timeout_ms < 1:
            raise ValueError("验证码首次提交观察超时必须大于零")
        if login_challenge_wait_seconds < 0 or challenge_poll_interval_seconds <= 0:
            raise ValueError("登录挑战等待范围无效")
        if email_form_stability_seconds <= 0:
            raise ValueError("邮箱表单稳定窗口必须大于零")
        normalized_screen_hint = str(signup_screen_hint).strip().casefold()
        if normalized_screen_hint not in {"login_or_signup", "signup"}:
            raise ValueError("注册 screen_hint 配置无效")
        self.ws_endpoint = ws_endpoint
        self.screenshot_path = Path(screenshot_path)
        self.playwright_factory = playwright_factory
        self.ip_check_timeout_ms = ip_check_timeout_ms
        self.login_navigation_timeout_ms = login_navigation_timeout_ms
        self.email_action_timeout_ms = email_action_timeout_ms
        self.next_step_timeout_ms = next_step_timeout_ms
        self.verification_retry_timeout_ms = min(
            verification_retry_timeout_ms,
            next_step_timeout_ms,
        )
        self.poll_interval_seconds = poll_interval_seconds
        self.pre_fill_delay_min_seconds = pre_fill_delay_min_seconds
        self.pre_fill_delay_max_seconds = pre_fill_delay_max_seconds
        self.email_pre_continue_delay_min_seconds = (
            email_pre_continue_delay_min_seconds
        )
        self.email_pre_continue_delay_max_seconds = (
            email_pre_continue_delay_max_seconds
        )
        self.pre_continue_delay_min_seconds = pre_continue_delay_min_seconds
        self.pre_continue_delay_max_seconds = pre_continue_delay_max_seconds
        self.profile_action_delay_min_seconds = profile_action_delay_min_seconds
        self.profile_action_delay_max_seconds = profile_action_delay_max_seconds
        self.security_action_delay_min_seconds = security_action_delay_min_seconds
        self.security_action_delay_max_seconds = security_action_delay_max_seconds
        self.security_action_timeout_ms = security_action_timeout_ms
        self.security_navigation_timeout_ms = security_navigation_timeout_ms
        self.session_navigation_timeout_ms = session_navigation_timeout_ms
        self.login_challenge_wait_seconds = login_challenge_wait_seconds
        self.challenge_poll_interval_seconds = challenge_poll_interval_seconds
        self.email_form_stability_seconds = email_form_stability_seconds
        self.signup_screen_hint = normalized_screen_hint
        self.random_uniform = random_uniform or random.uniform
        self.random_choice = random_choice or random.choice
        self.random_randint = random_randint or random.randint
        self.delay_sleep = delay_sleep or asyncio.sleep
        self.utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._playwright: Any = None
        self._browser: Any = None
        self._active_page: Any = None

    async def __aenter__(self) -> "CdpBrowserAutomation":
        if self.playwright_factory is None:
            from playwright.async_api import async_playwright

            manager = async_playwright()
        else:
            manager = self.playwright_factory()
        try:
            self._playwright = await manager.start()
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self.ws_endpoint,
                timeout=20_000,
            )
        except Exception:
            if self._playwright is not None:
                await self._playwright.stop()
            raise CdpConnectionError("无法连接 Roxy 提供的 CDP 端点") from None
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None
        self._active_page = None

    async def _page(self) -> Any:
        if self._active_page is not None:
            return self._active_page
        if self._browser is None:
            raise CdpConnectionError("CDP 浏览器尚未连接")
        contexts = self._browser.contexts
        if not contexts:
            raise CdpConnectionError("Roxy CDP 没有可用浏览器上下文")
        context = contexts[0]
        pages = list(context.pages)
        if not pages:
            self._active_page = await context.new_page()
            return self._active_page

        preferred_page = next(
            (
                page
                for page in pages
                if "api64.ipify.org" in str(getattr(page, "url", ""))
            ),
            pages[0],
        )
        for page in pages:
            if page is preferred_page:
                continue
            try:
                await page.close()
            except Exception:
                raise CdpConnectionError("无法关闭 Roxy 临时窗口中的多余标签页") from None
        self._active_page = preferred_page
        return preferred_page

    @staticmethod
    def _profile_button_candidates(page: Any) -> list[Any]:
        return [
            page.locator(
                'xpath=//*[@data-testid="accounts-profile-button" '
                'and @role="button" '
                'and not(ancestor-or-self::*[@inert]) '
                'and not(ancestor-or-self::*[@aria-hidden="true"])]'
            ),
            page.locator(
                '[data-testid="accounts-profile-button"][role="button"]'
                '[aria-label*=", open profile menu" i]:visible'
            ),
            page.locator(
                '[data-testid="accounts-profile-button"][role="button"]'
                '[aria-label$="open profile menu" i]'
                ':not([aria-label="Open profile menu" i]):visible'
            ),
            page.locator(
                'xpath=//*[@data-testid="accounts-profile-button" '
                'and @role="button" '
                'and not(ancestor::*[@inert]) '
                'and not(ancestor-or-self::*[@aria-hidden="true"]) '
                'and contains('
                'translate(@aria-label, '
                '"ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
                '"abcdefghijklmnopqrstuvwxyz"), '
                '"open profile menu")]'
            ),
        ]

    @staticmethod
    def _is_profile_path(path: str) -> bool:
        normalized = str(path or "").rstrip("/") or "/"
        return normalized in PROFILE_PATHS

    async def _auth_retry_control(
        self,
        page: Any,
        *,
        include_semantic: bool,
    ) -> Any | None:
        selectors = (
            'button[data-dd-action-name="Try again"]',
            'button[data-dd-action-name="Retry"]',
            'button[type="submit"][name="retry"]',
        )
        for selector in selectors:
            candidate = await self._first_visible_locator(page.locator(selector))
            if candidate is not None:
                return candidate
        if not include_semantic:
            return None
        try:
            return await self._first_visible_locator(
                page.get_by_role("button", name=AUTH_RETRY_ACTION_PATTERN)
            )
        except Exception:
            return None

    async def _click_auth_retry_if_available(
        self,
        page: Any,
        body_text: str = "",
    ) -> bool:
        control = await self._auth_retry_control(
            page,
            include_semantic=AUTH_RETRY_PAGE_PATTERN.search(body_text) is not None,
        )
        if control is None:
            return False
        try:
            if not await control.is_enabled():
                return False
            await control.click(timeout=self.email_action_timeout_ms)
        except Exception:
            return False
        return True

    async def _first_visible_locator(self, locator: Any) -> Any | None:
        try:
            count = await locator.count()
        except Exception:
            return None
        for index in range(count):
            try:
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    async def _profile_form_controls(self, page: Any) -> _ProfileFormControls | None:
        forms = page.locator(
            'xpath=//form['
            'not(ancestor-or-self::*[@inert]) '
            'and not(ancestor-or-self::*[@aria-hidden="true"])]'
        )
        try:
            form_count = await forms.count()
        except Exception:
            return None
        for index in range(form_count):
            form = forms.nth(index)
            try:
                if not await form.is_visible():
                    continue
            except Exception:
                continue

            strict_name = await self._first_visible_locator(
                form.locator('input[name="name"][autocomplete="name"]')
            )
            semantic_name = None
            if strict_name is None:
                try:
                    semantic_name = await self._first_visible_locator(
                        form.get_by_label(PROFILE_NAME_LABEL_PATTERN)
                    )
                except Exception:
                    semantic_name = None
            name_input = strict_name or semantic_name
            if name_input is None:
                continue

            strict_age = await self._first_visible_locator(
                form.locator('input[name="age"]')
            )
            strict_birthday = await self._first_visible_locator(
                form.locator(
                    'input[autocomplete="bday"], '
                    'input[name="birthday"], input[name="birthdate"], '
                    'input[id*="birthday" i], input[id*="birthdate" i]'
                )
            )
            birthday_value_input = None
            try:
                hidden_birthday = form.locator(
                    'input[name="birthday"], input[name="birthdate"]'
                )
                if await hidden_birthday.count() > 0:
                    birthday_value_input = hidden_birthday.first
            except Exception:
                birthday_value_input = None
            birthday_segments = (
                await self._first_visible_locator(
                    form.locator('[role="spinbutton"][data-type="year"]')
                ),
                await self._first_visible_locator(
                    form.locator('[role="spinbutton"][data-type="month"]')
                ),
                await self._first_visible_locator(
                    form.locator('[role="spinbutton"][data-type="day"]')
                ),
            )
            semantic_age = None
            semantic_birthday = None
            if strict_age is None:
                try:
                    semantic_age = await self._first_visible_locator(
                        form.get_by_label(PROFILE_AGE_LABEL_PATTERN)
                    )
                except Exception:
                    semantic_age = None
            if strict_birthday is None:
                try:
                    semantic_birthday = await self._first_visible_locator(
                        form.get_by_label(PROFILE_BIRTHDAY_LABEL_PATTERN)
                    )
                except Exception:
                    semantic_birthday = None

            age_input = strict_age or semantic_age
            birthday_input = strict_birthday or semantic_birthday
            if age_input is not None:
                return _ProfileFormControls(
                    name_input=name_input,
                    second_input=age_input,
                    form=form,
                    variant="numeric_age",
                    locator_strategy=(
                        "strict_attributes"
                        if strict_name is not None and strict_age is not None
                        else "semantic_labels"
                    ),
                )
            if all(segment is not None for segment in birthday_segments):
                year_segment, month_segment, day_segment = birthday_segments
                return _ProfileFormControls(
                    name_input=name_input,
                    second_input=year_segment,
                    form=form,
                    variant="birthday",
                    locator_strategy="segmented_date",
                    birthday_segments=(
                        year_segment,
                        month_segment,
                        day_segment,
                    ),
                    birthday_value_input=birthday_value_input,
                )
            if birthday_input is not None:
                return _ProfileFormControls(
                    name_input=name_input,
                    second_input=birthday_input,
                    form=form,
                    variant="birthday",
                    locator_strategy=(
                        "strict_attributes"
                        if strict_name is not None and strict_birthday is not None
                        else "semantic_labels"
                    ),
                    birthday_value_input=birthday_value_input,
                )
        return None

    async def _profile_field_presence(self, page: Any) -> tuple[bool, bool]:
        forms = page.locator(
            'xpath=//form['
            'not(ancestor-or-self::*[@inert]) '
            'and not(ancestor-or-self::*[@aria-hidden="true"])]'
        )
        name_visible = False
        second_visible = False
        try:
            form_count = await forms.count()
        except Exception:
            return False, False
        for index in range(form_count):
            form = forms.nth(index)
            try:
                if not await form.is_visible():
                    continue
            except Exception:
                continue
            candidates = (
                form.locator('input[name="name"][autocomplete="name"]'),
                form.get_by_label(PROFILE_NAME_LABEL_PATTERN),
            )
            for candidate in candidates:
                if await self._first_visible_locator(candidate) is not None:
                    name_visible = True
                    break
            second_candidates = (
                form.locator('input[name="age"]'),
                form.locator(
                    'input[autocomplete="bday"], input[name="birthday"], '
                    'input[name="birthdate"], input[id*="birthday" i], '
                    'input[id*="birthdate" i]'
                ),
                form.get_by_label(PROFILE_AGE_LABEL_PATTERN),
                form.get_by_label(PROFILE_BIRTHDAY_LABEL_PATTERN),
            )
            for candidate in second_candidates:
                if await self._first_visible_locator(candidate) is not None:
                    second_visible = True
                    break
        return name_visible, second_visible

    async def _profile_submit_control(
        self,
        controls: _ProfileFormControls,
    ) -> tuple[Any | None, str]:
        buttons = controls.form.locator(
            'button[type="submit"], button:not([type])'
        )
        try:
            count = await buttons.count()
        except Exception:
            return None, "unresolved"
        visible_buttons: list[tuple[Any, str]] = []
        for index in range(count):
            button = buttons.nth(index)
            try:
                if not await button.is_visible():
                    continue
                text = await button.inner_text(timeout=self.email_action_timeout_ms)
            except Exception:
                continue
            normalized = " ".join(text.split()).casefold()
            visible_buttons.append((button, normalized))
            if PROFILE_SUBMIT_PATTERN.fullmatch(normalized) is not None:
                return (
                    button,
                    "finish_creating_account"
                    if normalized == "finish creating account"
                    or "アカウント" in normalized
                    or "登録" in normalized
                    else "continue",
                )
        if len(visible_buttons) == 1:
            return visible_buttons[0][0], "structural_submit"
        return None, "unresolved"

    @staticmethod
    def _date_years_before(value: date, years: int) -> date:
        try:
            return value.replace(year=value.year - years)
        except ValueError:
            return value.replace(year=value.year - years, day=28)

    def _birthday_for_age(self, reference: date, age: int) -> date:
        earliest = self._date_years_before(reference, age + 1) + timedelta(days=1)
        latest = self._date_years_before(reference, age)
        maximum_offset = max(0, (latest - earliest).days)
        generated_offset = int(self.random_randint(0, maximum_offset))
        offset = min(maximum_offset, max(0, generated_offset))
        return earliest + timedelta(days=offset)

    async def _profile_second_visible(self, controls: _ProfileFormControls) -> bool:
        if controls.birthday_segments is None:
            return await controls.second_input.is_visible()
        return all(
            [await segment.is_visible() for segment in controls.birthday_segments]
        )

    async def _profile_second_value(self, controls: _ProfileFormControls) -> str:
        if controls.birthday_segments is None:
            return await controls.second_input.input_value(
                timeout=self.email_action_timeout_ms
            )
        if controls.birthday_value_input is not None:
            try:
                return await controls.birthday_value_input.input_value(
                    timeout=self.email_action_timeout_ms
                )
            except Exception:
                pass
        values: list[str] = []
        for segment in controls.birthday_segments:
            value = await segment.get_attribute("aria-valuenow")
            if value is None:
                value = await segment.inner_text(
                    timeout=self.email_action_timeout_ms
                )
            values.append(str(value).strip())
        year, month, day = values
        if all(value.isdigit() for value in values):
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return ""

    async def _fill_profile_second(
        self,
        controls: _ProfileFormControls,
        value: str,
    ) -> str:
        if controls.birthday_segments is None:
            await controls.second_input.fill(
                value,
                timeout=self.email_action_timeout_ms,
            )
            return await self._profile_second_value(controls)

        parsed = date.fromisoformat(value)
        year_segment, month_segment, day_segment = controls.birthday_segments
        for segment, segment_value in (
            (year_segment, f"{parsed.year:04d}"),
            (month_segment, f"{parsed.month:02d}"),
            (day_segment, f"{parsed.day:02d}"),
        ):
            await segment.click(timeout=self.email_action_timeout_ms)
            await segment.press("Control+A", timeout=self.email_action_timeout_ms)
            await segment.press_sequentially(
                segment_value,
                delay=50,
                timeout=self.email_action_timeout_ms,
            )
            await segment.press("Tab", timeout=self.email_action_timeout_ms)

        deadline = monotonic() + 2
        while True:
            current = await self._profile_second_value(controls)
            if current == value or monotonic() >= deadline:
                return current
            await asyncio.sleep(self.poll_interval_seconds)

    @staticmethod
    def _email_input_locator(page: Any) -> Any:
        return page.locator(
            'xpath=//form['
            'not(ancestor-or-self::*[@inert]) '
            'and not(ancestor-or-self::*[@aria-hidden="true"]) '
            'and .//input[@type="email" and '
            '(@id="email" or @name="email" or @autocomplete="email" '
            'or @autocomplete="username")]'
            ']//input[@type="email" and '
            '(@id="email" or @name="email" or @autocomplete="email" '
            'or @autocomplete="username")]'
        )

    @staticmethod
    def _email_continue_locator(page: Any) -> Any:
        return page.locator(
            'xpath=//form['
            'not(ancestor-or-self::*[@inert]) '
            'and not(ancestor-or-self::*[@aria-hidden="true"]) '
            'and .//input[@type="email" and '
            '(@id="email" or @name="email" or @autocomplete="email" '
            'or @autocomplete="username")]'
            ']//*[self::button or self::input][@type="submit" '
            'and not(ancestor-or-self::*[@inert]) '
            'and not(ancestor-or-self::*[@aria-hidden="true"])]'
        )

    async def _page_contains_challenge(
        self,
        page: Any,
        body_text: str | None = None,
    ) -> bool:
        if body_text is None:
            try:
                body_text = await page.locator("body").inner_text(timeout=1_000)
            except Exception:
                body_text = ""
        if contains_challenge(body_text):
            return True

        try:
            structural = page.locator(CHALLENGE_SELECTOR)
            if await structural.count() > 0 and await self._is_visible(structural):
                return True
        except Exception:
            pass

        try:
            frames = list(getattr(page, "frames", ()))
        except Exception:
            frames = []
        for frame in frames:
            frame_url = str(getattr(frame, "url", ""))
            if "challenges.cloudflare.com" in frame_url.casefold():
                return True
        return False

    async def _wait_for_email_form_stable(
        self,
        page: Any,
        *,
        expected_email: str | None,
        stable_seconds: float,
        allow_next_step: bool,
    ) -> _EmailFormStabilityResult:
        started = monotonic()
        scheduled_wait = 0.0
        stable_started_at: float | None = None
        stable_started_scheduled_at: float | None = None
        empty_email_started_at: float | None = None
        empty_email_started_scheduled_at: float | None = None
        challenge_observed = False
        stability_reset_count = 0
        last_challenge_present = False

        def elapsed_seconds() -> float:
            return max(0.0, monotonic() - started, scheduled_wait)

        def elapsed_ms() -> int:
            return max(
                0,
                int(
                    round(
                        min(self.login_challenge_wait_seconds, elapsed_seconds())
                        * 1000
                    )
                ),
            )

        while True:
            observed_elapsed = elapsed_seconds()
            remaining_ms = max(
                1,
                int(
                    max(0.0, self.login_challenge_wait_seconds - observed_elapsed)
                    * 1000
                ),
            )
            try:
                body_text = await page.locator("body").inner_text(
                    timeout=min(1_000, remaining_ms)
                )
                body_read = True
            except Exception:
                body_text = ""
                body_read = False

            challenge_present = await self._page_contains_challenge(page, body_text)
            last_challenge_present = challenge_present
            challenge_observed = challenge_observed or challenge_present

            current_url = sanitize_url(str(getattr(page, "url", "")))
            current_parsed = urlsplit(current_url)
            current_host = (current_parsed.hostname or "").casefold()
            current_path = current_parsed.path.rstrip("/")
            still_on_login = (
                current_host == "chatgpt.com" and current_path == "/auth/login"
            )

            try:
                ready_state = await page.evaluate("document.readyState")
            except Exception:
                ready_state = None

            email_input = self._email_input_locator(page)
            continue_button = self._email_continue_locator(page)
            email_visible = False
            email_enabled = False
            email_editable = False
            button_visible = False
            button_enabled = False
            email_value: str | None = None
            try:
                email_visible = await self._is_visible(email_input)
                if email_visible:
                    email_enabled = await email_input.first.is_enabled(
                        timeout=min(1_000, remaining_ms)
                    )
                    email_editable = await email_input.first.is_editable(
                        timeout=min(1_000, remaining_ms)
                    )
                    if expected_email is not None:
                        email_value = await email_input.first.input_value(
                            timeout=min(1_000, remaining_ms)
                        )
                button_visible = await self._is_visible(continue_button)
                if button_visible:
                    button_enabled = await continue_button.first.is_enabled(
                        timeout=min(1_000, remaining_ms)
                    )
            except Exception:
                email_visible = False
                email_enabled = False
                email_editable = False
                button_visible = False
                button_enabled = False
                email_value = None

            if allow_next_step and not challenge_present:
                password_input = page.locator('input[type="password"]')
                verification_input = page.locator(
                    'input[autocomplete="one-time-code"], '
                    'input[name="code"], input[name="otp"]'
                )
                try:
                    password_visible = await self._is_visible(password_input)
                except Exception:
                    password_visible = False
                try:
                    verification_visible = await self._is_visible(
                        verification_input
                    )
                except Exception:
                    verification_visible = False
                verification_url = (
                    current_host in {"auth.openai.com", "chatgpt.com"}
                    and current_path.endswith("/email-verification")
                )
                if password_visible:
                    return _EmailFormStabilityResult(
                        "next_step",
                        elapsed_ms(),
                        challenge_observed,
                        stability_reset_count,
                        "password",
                    )
                if AUTHENTICATOR_FACTOR_PATTERN.search(body_text):
                    return _EmailFormStabilityResult(
                        "next_step",
                        elapsed_ms(),
                        challenge_observed,
                        stability_reset_count,
                        "totp",
                    )
                if (
                    verification_visible
                    or verification_url
                    or VERIFICATION_PATTERN.search(body_text) is not None
                ):
                    return _EmailFormStabilityResult(
                        "next_step",
                        elapsed_ms(),
                        challenge_observed,
                        stability_reset_count,
                        "verification",
                    )
                if (
                    current_host in {"auth.openai.com", "chatgpt.com"}
                    and not still_on_login
                    and not email_visible
                ):
                    return _EmailFormStabilityResult(
                        "next_step",
                        elapsed_ms(),
                        challenge_observed,
                        stability_reset_count,
                        "transitioned",
                    )

            if (
                expected_email is not None
                and still_on_login
                and email_visible
                and email_value == ""
                and not challenge_present
            ):
                if empty_email_started_at is None:
                    empty_email_started_at = monotonic()
                    empty_email_started_scheduled_at = scheduled_wait
                elif (
                    max(
                        monotonic() - empty_email_started_at,
                        scheduled_wait
                        - (empty_email_started_scheduled_at or 0.0),
                    )
                    >= EMAIL_POST_SUBMIT_RESET_CONFIRMATION_SECONDS
                ):
                    if stable_started_at is not None:
                        stability_reset_count += 1
                    return _EmailFormStabilityResult(
                        "email_reset",
                        elapsed_ms(),
                        challenge_observed,
                        stability_reset_count,
                    )
            else:
                empty_email_started_at = None
                empty_email_started_scheduled_at = None

            form_stable = (
                body_read
                and not challenge_present
                and still_on_login
                and ready_state == "complete"
                and email_visible
                and email_enabled
                and email_editable
                and button_visible
                and button_enabled
                and (expected_email is None or email_value == expected_email)
            )
            if form_stable:
                if stable_started_at is None:
                    stable_started_at = monotonic()
                    stable_started_scheduled_at = scheduled_wait
                elif (
                    max(
                        monotonic() - stable_started_at,
                        scheduled_wait - (stable_started_scheduled_at or 0.0),
                    )
                    >= stable_seconds
                ):
                    return _EmailFormStabilityResult(
                        "stable",
                        elapsed_ms(),
                        challenge_observed,
                        stability_reset_count,
                    )
            else:
                if stable_started_at is not None:
                    stability_reset_count += 1
                    stable_started_at = None
                    stable_started_scheduled_at = None

            observed_elapsed = elapsed_seconds()
            if observed_elapsed >= self.login_challenge_wait_seconds:
                return _EmailFormStabilityResult(
                    "challenge_timeout"
                    if last_challenge_present
                    else "unstable_timeout",
                    elapsed_ms(),
                    challenge_observed,
                    stability_reset_count,
                )

            delay = min(
                self.challenge_poll_interval_seconds,
                self.login_challenge_wait_seconds - observed_elapsed,
            )
            await self.delay_sleep(delay)
            scheduled_wait += delay

    async def _open_registration_page(self, page: Any, email: str) -> None:
        if self.signup_screen_hint != "signup":
            started = monotonic()
            try:
                await page.goto(
                    CHATGPT_LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=self.login_navigation_timeout_ms,
                )
            except Exception as exc:
                raise _navigation_failure(
                    exc,
                    stage="login_navigation",
                    failed_code="login_navigation_failed",
                    timeout_code="login_navigation_timeout",
                    failed_message="ChatGPT 登录页导航失败",
                    timeout_message="ChatGPT 登录页导航超时",
                    started=started,
                    timeout_ms=self.login_navigation_timeout_ms,
                ) from None
            return

        started = monotonic()
        try:
            await page.goto(
                CHATGPT_HOME_URL,
                wait_until="domcontentloaded",
                timeout=self.login_navigation_timeout_ms,
            )
        except Exception as exc:
            raise _navigation_failure(
                exc,
                stage="signup_bootstrap_navigation",
                failed_code="signup_bootstrap_navigation_failed",
                timeout_code="signup_bootstrap_navigation_timeout",
                failed_message="强制密码注册初始化页面导航失败",
                timeout_message="强制密码注册初始化页面导航超时",
                started=started,
                timeout_ms=self.login_navigation_timeout_ms,
            ) from None

        bootstrap_started = monotonic()
        try:
            bootstrap = await page.evaluate(
                """
                async ({ email }) => {
                  const csrfResponse = await fetch('/api/auth/csrf', {
                    method: 'GET',
                    credentials: 'include',
                    headers: {accept: 'application/json'},
                  });
                  const csrfText = await csrfResponse.text().catch(() => '');
                  let csrf = {};
                  try { csrf = csrfText ? JSON.parse(csrfText) : {}; } catch (_) {}
                  const csrfToken = String(csrf.csrfToken || '');
                  if (!csrfResponse.ok || !csrfToken) {
                    return {ok:false, stage:'csrf', status:csrfResponse.status || 0};
                  }
                  const deviceCookie = document.cookie
                    .split(';')
                    .map(value => value.trim())
                    .find(value => value.startsWith('oai-did='));
                  const deviceId = deviceCookie
                    ? decodeURIComponent(deviceCookie.slice('oai-did='.length))
                    : crypto.randomUUID();
                  const query = new URLSearchParams({
                    prompt: 'login',
                    'ext-oai-did': deviceId,
                    auth_session_logging_id: crypto.randomUUID(),
                    'ext-passkey-client-capabilities': '0111',
                    screen_hint: 'signup',
                    login_hint: email,
                  });
                  const body = new URLSearchParams({
                    callbackUrl: 'https://chatgpt.com/',
                    csrfToken,
                    json: 'true',
                  });
                  const signinResponse = await fetch(
                    `/api/auth/signin/openai?${query.toString()}`,
                    {
                      method: 'POST',
                      credentials: 'include',
                      redirect: 'follow',
                      headers: {
                        accept: 'application/json',
                        'content-type': 'application/x-www-form-urlencoded',
                      },
                      body: body.toString(),
                    },
                  );
                  const signinText = await signinResponse.text().catch(() => '');
                  let payload = {};
                  try { payload = signinText ? JSON.parse(signinText) : {}; } catch (_) {}
                  const url = String(payload.url || '');
                  return {
                    ok: signinResponse.ok && Boolean(url),
                    stage: 'signin',
                    status: signinResponse.status || 0,
                    url,
                  };
                }
                """,
                {"email": email},
            )
        except Exception as exc:
            raise _stage_failure(
                exc,
                stage="signup_bootstrap",
                code="signup_bootstrap_failed",
                message="强制密码注册初始化失败",
                started=bootstrap_started,
                timeout_ms=self.login_navigation_timeout_ms,
            ) from None

        auth_url = str(bootstrap.get("url") or "") if isinstance(bootstrap, dict) else ""
        parsed_auth_url = urlsplit(auth_url)
        if (
            not isinstance(bootstrap, dict)
            or not bootstrap.get("ok")
            or parsed_auth_url.scheme != "https"
            or (parsed_auth_url.hostname or "").casefold()
            not in {"auth.openai.com", "chatgpt.com"}
        ):
            raise _stage_failure(
                RuntimeError("signup bootstrap response invalid"),
                stage="signup_bootstrap",
                code="signup_bootstrap_response_invalid",
                message="强制密码注册初始化响应无效",
                started=bootstrap_started,
                timeout_ms=self.login_navigation_timeout_ms,
            )

        navigation_started = monotonic()
        try:
            await page.goto(
                auth_url,
                wait_until="domcontentloaded",
                timeout=self.login_navigation_timeout_ms,
            )
        except Exception as exc:
            raise _navigation_failure(
                exc,
                stage="signup_navigation",
                failed_code="signup_navigation_failed",
                timeout_code="signup_navigation_timeout",
                failed_message="强制密码注册页导航失败",
                timeout_message="强制密码注册页导航超时",
                started=navigation_started,
                timeout_ms=self.login_navigation_timeout_ms,
            ) from None

    async def _resolve_forced_signup_password_route(self, page: Any) -> str | None:
        if self.signup_screen_hint != "signup":
            return None

        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        route_clicked = False
        while monotonic() < deadline:
            remaining_ms = max(1, int((deadline - monotonic()) * 1000))
            current_url = sanitize_url(str(getattr(page, "url", "")))
            current_path = urlsplit(current_url).path.rstrip("/").casefold()
            new_password_input = page.locator(
                'input[autocomplete="new-password"], input[name="new-password"]'
            )
            any_password_input = page.locator('input[type="password"]')
            verification_input = page.locator(
                'input[autocomplete="one-time-code"], '
                'input[name="code"], input[name="otp"]'
            )

            try:
                new_password_visible = await self._is_visible(new_password_input)
                any_password_visible = await self._is_visible(any_password_input)
            except Exception:
                new_password_visible = False
                any_password_visible = False

            if new_password_visible and (
                current_path.endswith("/create-account/password")
                or "create-account" in current_path
            ):
                await self._safe_screenshot(page)
                return "password"

            if any_password_visible:
                await self._safe_screenshot(page)
                raise PasswordStepError(
                    "existing_account_password_required",
                    "该邮箱进入了已有账号密码登录页，未创建新密码",
                )

            try:
                verification_visible = await self._is_visible(verification_input)
            except Exception:
                verification_visible = False
            verification_url = current_path.endswith("/email-verification")
            if verification_visible or verification_url:
                if route_clicked:
                    await self.delay_sleep(self.poll_interval_seconds)
                    continue
                password_route = page.locator(PASSWORD_ROUTE_SELECTOR)
                try:
                    route_visible = await self._is_visible(password_route)
                except Exception:
                    route_visible = False
                if route_visible:
                    try:
                        await password_route.first.click(
                            timeout=min(self.email_action_timeout_ms, remaining_ms)
                        )
                    except Exception:
                        route_visible = False
                if not route_visible:
                    try:
                        route_visible = bool(
                            await page.evaluate(
                                """
                                () => {
                                  const passwordRouteCandidates = [...document.querySelectorAll(
                                    'button,a,[role="button"],[role="link"]'
                                  )];
                                  const visible = element => !!element &&
                                    !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length) &&
                                    getComputedStyle(element).visibility !== 'hidden' &&
                                    getComputedStyle(element).display !== 'none';
                                  const enabled = element => !element.disabled &&
                                    String(element.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
                                  const target = passwordRouteCandidates.find(element => {
                                    if (!visible(element) || !enabled(element)) return false;
                                    const text = [
                                      element.innerText,
                                      element.textContent,
                                      element.getAttribute('aria-label'),
                                      element.getAttribute('title'),
                                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                                    return /continue with password|パスワードで続行|使用密码继续|使用密碼繼續|비밀번호로 계속|şifre ile devam|parola ile devam|mit passwort fortfahren/i.test(text);
                                  });
                                  if (!target) return false;
                                  target.scrollIntoView({block: 'center'});
                                  target.click();
                                  return true;
                                }
                                """
                            )
                        )
                    except Exception:
                        route_visible = False
                if not route_visible:
                    await self._safe_screenshot(page)
                    return "verification"
                route_clicked = True
                await self.delay_sleep(self.poll_interval_seconds)
                continue

            email_input = self._email_input_locator(page)
            try:
                if await self._is_visible(email_input):
                    return None
            except Exception:
                pass
            await self.delay_sleep(self.poll_interval_seconds)

        if route_clicked:
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "registration_password_route_timeout",
                "点击密码注册入口后未进入创建密码页",
            )
        return None

    async def submit_email_and_continue(self, email: str) -> AutomationResult:
        page = await self._page()
        started = monotonic()
        stage_started = monotonic()
        try:
            await page.goto(
                IP_CHECK_URL,
                wait_until="domcontentloaded",
                timeout=self.ip_check_timeout_ms,
            )
        except Exception as exc:
            raise _navigation_failure(
                exc,
                stage="ip_navigation",
                failed_code="ip_navigation_failed",
                timeout_code="ip_navigation_timeout",
                failed_message="代理出口 IP 页面导航失败",
                timeout_message="代理出口 IP 页面导航超时",
                started=stage_started,
                timeout_ms=self.ip_check_timeout_ms,
            ) from None

        stage_started = monotonic()
        try:
            body = await page.locator("body").inner_text(timeout=5_000)
        except Exception as exc:
            raise _stage_failure(
                exc,
                stage="ip_response_read",
                code="ip_response_read_failed",
                message="代理出口 IP 响应读取失败",
                started=stage_started,
                timeout_ms=5_000,
            ) from None

        stage_started = monotonic()
        try:
            ip_payload = json.loads(body)
            egress_ip = str(ip_payload["ip"])
            masked_ip = mask_ip(egress_ip)
        except Exception as exc:
            raise _stage_failure(
                exc,
                stage="ip_response_parse",
                code="ip_response_invalid",
                message="代理出口 IP 响应格式无效",
                started=stage_started,
                timeout_ms=None,
            ) from None

        signup_bootstrap_started_at_utc: datetime | None = None
        if self.signup_screen_hint == "signup":
            signup_bootstrap_started_at_utc = self.utc_now()
            if signup_bootstrap_started_at_utc.tzinfo is None:
                raise ValueError("强制密码注册时间必须包含时区")
            signup_bootstrap_started_at_utc = (
                signup_bootstrap_started_at_utc.astimezone(timezone.utc)
            )
        await self._open_registration_page(page, email)

        stage_started = monotonic()
        try:
            body_text = await page.locator("body").inner_text(timeout=10_000)
        except Exception as exc:
            raise _stage_failure(
                exc,
                stage="login_content_read",
                code="login_content_read_failed",
                message="ChatGPT 登录页内容读取失败",
                started=stage_started,
                timeout_ms=10_000,
            ) from None

        final_url = sanitize_url(page.url)

        email_fill_attempts = 0
        email_form_reset_count = 0
        email_continue_attempts = 0
        email_post_submit_reset_count = 0
        email_pre_fill_delays_ms: list[int] = []
        email_pre_continue_delays_ms: list[int] = []
        pre_continue_delay_ms = 0
        submitted_at_utc: datetime | None = None
        email_continue_click_failures = 0
        email_continue_recovery_state: str | None = None
        email_continue_attempt_states: list[str] = []
        email_continue_click_exception_types: list[str] = []
        email_continue_dispatch_observed = False
        email_continue_recovery_started_at: float | None = None
        login_challenge_observed = contains_challenge(body_text)
        email_form_ready_wait_ms = 0
        email_pre_continue_stable_waits_ms: list[int] = []
        email_form_stability_reset_count = 0

        def stability_diagnostics() -> dict[str, Any]:
            return {
                "login_challenge_observed": login_challenge_observed,
                "email_form_ready_wait_ms": email_form_ready_wait_ms,
                "email_pre_continue_stable_waits_ms": tuple(
                    email_pre_continue_stable_waits_ms
                ),
                "email_form_stability_reset_count": (
                    email_form_stability_reset_count
                ),
            }

        def build_result(next_step: str) -> AutomationResult:
            if submitted_at_utc is None:
                raise RuntimeError("邮箱提交时间缺失")
            return AutomationResult(
                egress_ip_masked=masked_ip,
                final_url=sanitize_url(page.url),
                latency_ms=int((monotonic() - started) * 1000),
                next_step=next_step,
                pre_continue_delay_ms=pre_continue_delay_ms,
                submitted_at_utc=submitted_at_utc,
                email_fill_attempts=email_fill_attempts,
                email_form_reset_count=email_form_reset_count,
                email_pre_fill_delays_ms=tuple(email_pre_fill_delays_ms),
                email_continue_attempts=email_continue_attempts,
                email_post_submit_reset_count=email_post_submit_reset_count,
                email_pre_continue_delays_ms=tuple(email_pre_continue_delays_ms),
                email_continue_click_failures=email_continue_click_failures,
                email_continue_recovery_state=email_continue_recovery_state,
                email_continue_attempt_states=tuple(email_continue_attempt_states),
                email_continue_dispatch_observed=email_continue_dispatch_observed,
                email_continue_click_exception_types=tuple(
                    email_continue_click_exception_types
                ),
                email_continue_recovery_elapsed_ms=(
                    max(
                        0,
                        int(
                            round(
                                (monotonic() - email_continue_recovery_started_at)
                                * 1000
                            )
                        ),
                    )
                    if email_continue_recovery_started_at is not None
                    else 0
                ),
                login_challenge_observed=login_challenge_observed,
                email_form_ready_wait_ms=email_form_ready_wait_ms,
                email_pre_continue_stable_waits_ms=tuple(
                    email_pre_continue_stable_waits_ms
                ),
                email_form_stability_reset_count=(
                    email_form_stability_reset_count
                ),
                egress_ip=egress_ip,
            )

        forced_signup_next_step = await self._resolve_forced_signup_password_route(
            page
        )
        if forced_signup_next_step is not None:
            if signup_bootstrap_started_at_utc is None:
                raise RuntimeError("强制密码注册时间缺失")
            submitted_at_utc = signup_bootstrap_started_at_utc
            email_continue_recovery_state = "signup_bootstrap"
            email_continue_dispatch_observed = True
            email_continue_attempt_states.append("signup_bootstrap_next_step")
            return build_result(forced_signup_next_step)

        for fill_attempt in range(1, 3):
            generated_pre_fill_delay = float(
                self.random_uniform(
                    self.pre_fill_delay_min_seconds,
                    self.pre_fill_delay_max_seconds,
                )
            )
            pre_fill_delay_seconds = min(
                self.pre_fill_delay_max_seconds,
                max(self.pre_fill_delay_min_seconds, generated_pre_fill_delay),
            )
            pre_fill_stable_seconds = max(
                self.email_form_stability_seconds,
                pre_fill_delay_seconds,
            )
            email_pre_fill_delays_ms.append(
                int(round(pre_fill_stable_seconds * 1000))
            )

            ready_result = await self._wait_for_email_form_stable(
                page,
                expected_email=None,
                stable_seconds=pre_fill_stable_seconds,
                allow_next_step=submitted_at_utc is not None,
            )
            login_challenge_observed = (
                login_challenge_observed or ready_result.challenge_observed
            )
            email_form_ready_wait_ms += ready_result.wait_ms
            email_form_stability_reset_count += (
                ready_result.stability_reset_count
            )
            if ready_result.status == "next_step":
                if ready_result.next_step is None:
                    raise RuntimeError("邮箱页面恢复状态缺失")
                email_continue_recovery_state = "next_step"
                return build_result(ready_result.next_step)
            if ready_result.status == "challenge_timeout":
                await self._safe_screenshot(page)
                raise TargetChallengeError(
                    "Cloudflare 挑战未在 60 秒内自动完成",
                    stage="login",
                    wait_ms=int(round(self.login_challenge_wait_seconds * 1000)),
                    **stability_diagnostics(),
                )
            if ready_result.status != "stable":
                await self._safe_screenshot(page)
                raise EmailStepError(
                    "email_form_not_stable",
                    "邮箱表单未在 60 秒内达到连续稳定状态",
                    **stability_diagnostics(),
                )

            email_input = self._email_input_locator(page)
            try:
                await email_input.first.fill(email, timeout=self.email_action_timeout_ms)
                filled_value = await email_input.first.input_value(
                    timeout=self.email_action_timeout_ms
                )
            except Exception:
                await self._safe_screenshot(page)
                raise EmailStepError(
                    "email_fill_failed",
                    "邮箱填写失败",
                    **stability_diagnostics(),
                ) from None
            if filled_value != email:
                await self._safe_screenshot(page)
                raise EmailStepError(
                    "email_value_mismatch",
                    "邮箱输入值校验失败",
                    **stability_diagnostics(),
                )

            email_fill_attempts = fill_attempt
            generated_delay = float(
                self.random_uniform(
                    self.email_pre_continue_delay_min_seconds,
                    self.email_pre_continue_delay_max_seconds,
                )
            )
            pre_continue_delay_seconds = min(
                self.email_pre_continue_delay_max_seconds,
                max(self.email_pre_continue_delay_min_seconds, generated_delay),
            )
            pre_continue_stable_seconds = max(
                self.email_form_stability_seconds,
                pre_continue_delay_seconds,
            )
            pre_continue_delay_ms = int(
                round(pre_continue_stable_seconds * 1000)
            )

            pre_continue_result = await self._wait_for_email_form_stable(
                page,
                expected_email=email,
                stable_seconds=pre_continue_stable_seconds,
                allow_next_step=submitted_at_utc is not None,
            )
            login_challenge_observed = (
                login_challenge_observed
                or pre_continue_result.challenge_observed
            )
            email_pre_continue_stable_waits_ms.append(
                pre_continue_result.wait_ms
            )
            email_form_stability_reset_count += (
                pre_continue_result.stability_reset_count
            )
            if pre_continue_result.status == "next_step":
                if pre_continue_result.next_step is None:
                    raise RuntimeError("邮箱页面恢复状态缺失")
                email_continue_recovery_state = "next_step"
                return build_result(pre_continue_result.next_step)
            if pre_continue_result.status == "challenge_timeout":
                await self._safe_screenshot(page)
                raise TargetChallengeError(
                    "Cloudflare 挑战未在 60 秒内自动完成",
                    stage="login",
                    wait_ms=int(round(self.login_challenge_wait_seconds * 1000)),
                    **stability_diagnostics(),
                )
            if pre_continue_result.status == "unstable_timeout":
                await self._safe_screenshot(page)
                raise EmailStepError(
                    "email_form_unstable_before_continue",
                    "邮箱表单在点击 Continue 前未能连续稳定 5 秒",
                    **stability_diagnostics(),
                )
            if pre_continue_result.status == "email_reset":
                email_form_reset_count += 1
                if fill_attempt == 2:
                    await self._safe_screenshot(page)
                    raise EmailStepError(
                        "email_form_reset",
                        "邮箱输入框在提交前被网页重复重置",
                        **stability_diagnostics(),
                    )
                continue

            refreshed_email_input = self._email_input_locator(page)
            refreshed_continue_button = self._email_continue_locator(page)
            try:
                value_before_click = await refreshed_email_input.first.input_value(
                    timeout=self.email_action_timeout_ms
                )
                input_still_visible = await self._is_visible(refreshed_email_input)
                button_still_visible = await self._is_visible(
                    refreshed_continue_button
                )
                input_still_enabled = await refreshed_email_input.first.is_enabled(
                    timeout=self.email_action_timeout_ms
                )
                input_still_editable = await refreshed_email_input.first.is_editable(
                    timeout=self.email_action_timeout_ms
                )
                button_still_enabled = (
                    await refreshed_continue_button.first.is_enabled(
                        timeout=self.email_action_timeout_ms
                    )
                )
            except Exception:
                value_before_click = ""
                input_still_visible = False
                button_still_visible = False
                input_still_enabled = False
                input_still_editable = False
                button_still_enabled = False

            form_stable = (
                value_before_click == email
                and input_still_visible
                and input_still_enabled
                and input_still_editable
                and button_still_visible
                and button_still_enabled
            )
            if not form_stable:
                email_form_stability_reset_count += 1
                if value_before_click != "":
                    await self._safe_screenshot(page)
                    raise EmailStepError(
                        "email_form_unstable_before_continue",
                        "邮箱表单在点击 Continue 前失去稳定状态",
                        **stability_diagnostics(),
                    )
                email_form_reset_count += 1
                if fill_attempt == 2:
                    await self._safe_screenshot(page)
                    raise EmailStepError(
                        "email_form_reset",
                        "邮箱输入框在提交前被网页重复重置",
                        **stability_diagnostics(),
                    )
                continue

            email_pre_continue_delays_ms.append(pre_continue_delay_ms)
            submitted_at_utc = self.utc_now()
            if submitted_at_utc.tzinfo is None:
                raise ValueError("邮箱提交时间必须包含时区")
            submitted_at_utc = submitted_at_utc.astimezone(timezone.utc)
            email_continue_attempts += 1
            email_continue_attempt_states.append("click_started")
            click_outcome, observed_step, click_error = (
                await self._click_email_and_observe(
                    page,
                    refreshed_continue_button.first,
                    final_url,
                    email,
                )
            )
            if click_error is not None:
                email_continue_click_failures += 1
                email_continue_click_exception_types.append(
                    _safe_exception_type(click_error)
                )
                if email_continue_recovery_started_at is None:
                    email_continue_recovery_started_at = monotonic()
            if click_outcome == "next_step":
                if observed_step is None:
                    raise RuntimeError("邮箱提交后的页面状态缺失")
                email_continue_dispatch_observed = True
                email_continue_attempt_states[-1] = "next_step_observed_during_click"
                if email_continue_recovery_state is None:
                    email_continue_recovery_state = "next_step"
                return build_result(observed_step)
            if click_outcome == "click_succeeded":
                email_continue_dispatch_observed = True
                email_continue_attempt_states[-1] = "click_succeeded"
            else:
                email_continue_attempt_states[-1] = "click_exception"
                try:
                    reconciled_step = await self._wait_for_next_step(
                        page,
                        final_url,
                        expected_email=email,
                    )
                except EmailStepError:
                    reconciled_step = None
                if reconciled_step is not None and reconciled_step not in {
                    "email_form_stable",
                    "email_form_reset",
                }:
                    email_continue_dispatch_observed = True
                    if email_continue_recovery_state is None:
                        email_continue_recovery_state = "next_step"
                    return build_result(reconciled_step)
                if reconciled_step == "email_form_stable":
                    email_continue_recovery_state = "stable_form_retry"
                elif reconciled_step == "email_form_reset":
                    email_post_submit_reset_count += 1
                    email_continue_recovery_state = "form_reset"
                if fill_attempt >= 2 or email_continue_attempts >= 2:
                    screenshot_captured = await self._safe_screenshot_after_settle(page)
                    raise EmailStepError(
                        "email_post_submit_reset"
                        if reconciled_step == "email_form_reset"
                        else "continue_click_failed",
                        "邮箱提交后被网页重复重置"
                        if reconciled_step == "email_form_reset"
                        else "Continue 按钮点击失败",
                        click_attempts=email_continue_attempts,
                        click_failures=email_continue_click_failures,
                        recovery_state=email_continue_recovery_state,
                        attempt_states=tuple(email_continue_attempt_states),
                        dispatch_observed=email_continue_dispatch_observed,
                        exception_types=tuple(email_continue_click_exception_types),
                        recovery_elapsed_ms=(
                            int(
                                round(
                                    (
                                        monotonic()
                                        - email_continue_recovery_started_at
                                    )
                                    * 1000
                                )
                            )
                            if email_continue_recovery_started_at is not None
                            else 0
                        ),
                        screenshot_captured=screenshot_captured,
                        **stability_diagnostics(),
                    ) from None
                continue

            try:
                next_step = await self._wait_for_next_step(page, final_url)
            except EmailStepError as exc:
                if (
                    exc.code == "email_continue_timeout"
                    and fill_attempt == 1
                    and email_continue_attempts == 1
                ):
                    email_continue_recovery_state = "stalled_form_reload_retry"
                    if email_continue_recovery_started_at is None:
                        email_continue_recovery_started_at = monotonic()
                    email_continue_attempt_states[-1] = "stalled_form_reload_retry"
                    try:
                        await page.goto(
                            CHATGPT_LOGIN_URL,
                            wait_until="domcontentloaded",
                            timeout=self.login_navigation_timeout_ms,
                        )
                    except Exception:
                        email_continue_recovery_state = "stalled_form_retry"
                        email_continue_attempt_states[-1] = "stalled_form_retry"
                    continue
                raise
            if next_step == "email_form_reset":
                email_post_submit_reset_count += 1
                email_continue_recovery_state = "form_reset"
                if email_continue_recovery_started_at is None:
                    email_continue_recovery_started_at = monotonic()
                if fill_attempt == 2 or email_continue_attempts >= 2:
                    screenshot_captured = await self._safe_screenshot_after_settle(page)
                    raise EmailStepError(
                        "email_post_submit_reset",
                        "邮箱提交后被网页重复重置",
                        click_attempts=email_continue_attempts,
                        click_failures=email_continue_click_failures,
                        recovery_state="form_reset",
                        attempt_states=tuple(email_continue_attempt_states),
                        dispatch_observed=email_continue_dispatch_observed,
                        exception_types=tuple(email_continue_click_exception_types),
                        recovery_elapsed_ms=int(
                            round(
                                (monotonic() - email_continue_recovery_started_at)
                                * 1000
                            )
                        ),
                        screenshot_captured=screenshot_captured,
                        **stability_diagnostics(),
                    )
                continue

            if email_continue_recovery_state is None:
                email_continue_recovery_state = "next_step"
            return build_result(next_step)

        raise RuntimeError("邮箱提交状态机意外结束")

    async def submit_password_and_continue(
        self,
        password: str,
    ) -> PasswordSubmitResult:
        if (
            len(password) < 12
            or not re.search(r"[A-Z]", password)
            or not re.search(r"[a-z]", password)
            or not re.search(r"[0-9]", password)
            or not re.search(r"[^A-Za-z0-9]", password)
        ):
            raise PasswordStepError(
                "generated_password_invalid",
                "生成的注册密码不符合强度要求",
            )

        page = await self._page()
        initial_url = sanitize_url(page.url)
        password_input = page.locator('input[type="password"]')
        submit_button = page.locator(
            'xpath=//form['
            'not(ancestor-or-self::*[@inert]) '
            'and not(ancestor-or-self::*[@aria-hidden="true"]) '
            'and .//input[@type="password"]'
            ']//*[self::button or self::input]['
            '@type="submit" or (self::button and not(@type))]'
        )

        try:
            input_visible = await self._is_visible(password_input)
            button_visible = await self._is_visible(submit_button)
        except Exception:
            input_visible = False
            button_visible = False
        if not input_visible:
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_input_missing",
                "未找到注册密码输入框",
            )
        if not button_visible:
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_continue_button_missing",
                "未找到密码页面的提交按钮",
            )

        try:
            await password_input.first.fill(
                password,
                timeout=self.email_action_timeout_ms,
            )
            filled_value = await password_input.first.input_value(
                timeout=self.email_action_timeout_ms,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_fill_failed",
                "注册密码填写失败",
            ) from None
        if filled_value != password:
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_value_mismatch",
                "注册密码输入值校验失败",
            )

        generated_delay = float(
            self.random_uniform(
                self.email_pre_continue_delay_min_seconds,
                self.email_pre_continue_delay_max_seconds,
            )
        )
        delay_seconds = min(
            self.email_pre_continue_delay_max_seconds,
            max(self.email_pre_continue_delay_min_seconds, generated_delay),
        )
        await self.delay_sleep(delay_seconds)
        pre_continue_delay_ms = int(round(delay_seconds * 1000))

        submitted_at_utc = self.utc_now()
        if submitted_at_utc.tzinfo is None:
            raise ValueError("密码提交时间必须包含时区")
        submitted_at_utc = submitted_at_utc.astimezone(timezone.utc)
        observation_task = asyncio.create_task(
            self._wait_for_password_next_step(page, initial_url),
            name="password-next-step-observer",
        )
        click_task = asyncio.create_task(
            submit_button.first.click(timeout=self.email_action_timeout_ms),
            name="password-continue-click",
        )
        click_outcome, observation_outcome = await asyncio.gather(
            click_task,
            observation_task,
            return_exceptions=True,
        )
        if isinstance(observation_outcome, asyncio.CancelledError):
            raise observation_outcome
        if isinstance(observation_outcome, TargetChallengeError):
            raise observation_outcome
        if isinstance(observation_outcome, PasswordStepError):
            raise observation_outcome
        if isinstance(observation_outcome, BaseException):
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_next_step_unknown",
                "密码提交后页面状态检查失败",
            ) from None

        click_completed = not isinstance(click_outcome, BaseException)
        click_exception_type = (
            _safe_exception_type(click_outcome)
            if isinstance(click_outcome, Exception)
            else ("Exception" if isinstance(click_outcome, BaseException) else None)
        )
        return PasswordSubmitResult(
            final_url=sanitize_url(page.url),
            next_step=observation_outcome,
            pre_continue_delay_ms=pre_continue_delay_ms,
            submitted_at_utc=submitted_at_utc,
            click_completed=click_completed,
            click_exception_type=click_exception_type,
        )

    async def _wait_for_password_next_step(
        self,
        page: Any,
        initial_url: str,
    ) -> str:
        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        password_input = page.locator('input[type="password"]')
        verification_input = page.locator(
            'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"]'
        )
        alert = page.locator('[role="alert"]:not(.sr-only)')
        while True:
            remaining_ms = max(1, int((deadline - monotonic()) * 1000))
            try:
                body_text = await page.locator("body").inner_text(
                    timeout=min(1_000, remaining_ms)
                )
            except Exception:
                body_text = ""
            if contains_challenge(body_text):
                await self._safe_screenshot(page)
                raise TargetChallengeError(
                    "检测到人机验证或挑战页，探测已停止",
                    stage="password",
                )
            if AUTHENTICATOR_FACTOR_PATTERN.search(body_text):
                await self._safe_screenshot(page)
                return "totp"
            current_url = sanitize_url(page.url)
            parsed = urlsplit(current_url)
            verification_url = (
                (parsed.hostname or "").casefold() in {"auth.openai.com", "chatgpt.com"}
                and parsed.path.rstrip("/").endswith("/email-verification")
            )
            try:
                if await self._is_visible(verification_input) or verification_url:
                    await self._safe_screenshot(page)
                    return "verification"
                if await self._is_visible(alert):
                    alert_text = await alert.first.inner_text(
                        timeout=min(1_000, remaining_ms)
                    )
                    if PASSWORD_REJECTION_PATTERN.search(alert_text.strip()):
                        await self._safe_screenshot(page)
                        raise PasswordStepError(
                            "password_rejected",
                            "网站未接受生成的注册密码",
                        )
            except PasswordStepError:
                raise
            except Exception:
                pass
            if monotonic() >= deadline:
                await self._safe_screenshot(page)
                raise PasswordStepError(
                    "password_next_step_unknown",
                    "密码提交后未进入验证码页面",
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def submit_existing_password_and_continue(
        self,
        password: str,
    ) -> PasswordSubmitResult:
        if not password:
            raise PasswordStepError(
                "existing_password_missing",
                "已有账号缺少登录密码",
            )
        page = await self._page()
        initial_url = sanitize_url(page.url)
        password_input = page.locator('input[type="password"]')
        submit_button = page.locator(
            'xpath=//form[.//input[@type="password"]]'
            '//*[self::button or self::input][@type="submit" or self::button]'
        )
        if not await self._is_visible(password_input):
            raise PasswordStepError(
                "existing_password_input_missing",
                "未找到已有账号密码输入框",
            )
        if not await self._is_visible(submit_button):
            raise PasswordStepError(
                "existing_password_submit_missing",
                "未找到已有账号密码提交按钮",
            )
        try:
            await password_input.first.fill(
                password,
                timeout=self.email_action_timeout_ms,
            )
        except Exception:
            raise PasswordStepError(
                "existing_password_fill_failed",
                "已有账号密码填写失败",
            ) from None
        submitted_at = self.utc_now()
        if submitted_at.tzinfo is None:
            raise ValueError("密码提交时间必须包含时区")
        submitted_at = submitted_at.astimezone(timezone.utc)
        try:
            await submit_button.first.click(timeout=self.email_action_timeout_ms)
        except Exception:
            raise PasswordStepError(
                "existing_password_click_failed",
                "已有账号密码提交失败",
            ) from None

        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        verification_input = page.locator(
            'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"]'
        )
        alert = page.locator('[role="alert"]:not(.sr-only)')
        while monotonic() < deadline:
            if await self._is_confirmed_chatgpt_home(page):
                return PasswordSubmitResult(
                    final_url=sanitize_url(page.url),
                    next_step="account_home",
                    pre_continue_delay_ms=0,
                    submitted_at_utc=submitted_at,
                )
            try:
                body = await page.locator("body").inner_text(timeout=1_000)
            except Exception:
                body = ""
            if AUTHENTICATOR_FACTOR_PATTERN.search(body):
                return PasswordSubmitResult(
                    final_url=sanitize_url(page.url),
                    next_step="totp",
                    pre_continue_delay_ms=0,
                    submitted_at_utc=submitted_at,
                )
            parsed = urlsplit(sanitize_url(page.url))
            verification_url = parsed.path.rstrip("/").endswith(
                "/email-verification"
            )
            try:
                if await self._is_visible(verification_input) or verification_url:
                    return PasswordSubmitResult(
                        final_url=sanitize_url(page.url),
                        next_step="verification",
                        pre_continue_delay_ms=0,
                        submitted_at_utc=submitted_at,
                    )
                if await self._is_visible(alert):
                    alert_text = await alert.first.inner_text(timeout=1_000)
                    if PASSWORD_REJECTION_PATTERN.search(alert_text):
                        raise PasswordStepError(
                            "existing_password_rejected",
                            "网站未接受已有账号密码",
                        )
            except PasswordStepError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval_seconds)
        await self._safe_screenshot(page)
        raise PasswordStepError(
            "existing_password_next_step_unknown",
            "已有账号密码提交后未进入可识别页面",
        )

    async def switch_password_page_to_email_code(self) -> PasswordSubmitResult:
        page = await self._page()
        initial_url = sanitize_url(page.url)
        submitted_at = self.utc_now()
        if submitted_at.tzinfo is None:
            raise ValueError("邮箱验证码切换时间必须包含时区")
        submitted_at = submitted_at.astimezone(timezone.utc)
        try:
            clicked = await page.evaluate(
                """
                () => {
                  const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                    && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                  const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
                  const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
                  const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')]
                    .filter(el => visible(el) && enabled(el));
                  const hit = candidates.find(el => {
                    const name = String(el.getAttribute('name') || '').toLowerCase();
                    const value = String(el.getAttribute('value') || '').toLowerCase();
                    const attrs = [el.id, name, value, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid'), el.textContent]
                      .join(' ').toLowerCase();
                    const text = norm(el.textContent || el.getAttribute('value') || '');
                    return (name === 'intent' && value.includes('passwordless') && value.includes('otp'))
                      || /passwordless.*otp|otp.*passwordless|one[-_\s]?time.*code|code.*one[-_\s]?time/.test(attrs)
                      || /一次性验证码|一次性驗證碼|ワンタイムコード|メールでコード|useaone-timecode|continuewithaone-timecode|loginwithaone-timecode/.test(text);
                  });
                  if (!hit) return false;
                  hit.scrollIntoView({block:'center'});
                  hit.click();
                  return true;
                }
                """
            )
        except Exception:
            clicked = False
        if not clicked:
            raise PasswordStepError(
                "passwordless_email_code_action_missing",
                "密码页面没有使用邮箱验证码登录入口",
            )
        next_step = await self._wait_for_password_next_step(page, initial_url)
        return PasswordSubmitResult(
            final_url=sanitize_url(page.url),
            next_step=next_step,
            pre_continue_delay_ms=0,
            submitted_at_utc=submitted_at,
        )

    async def submit_verification_code_and_continue(
        self,
        verification_code: str,
    ) -> VerificationSubmitResult:
        if re.fullmatch(r"[0-9]{6}", verification_code) is None:
            raise VerificationStepError(
                "verification_code_invalid",
                "接码结果不是有效的 6 位验证码",
            )

        page = await self._page()
        initial_url = sanitize_url(page.url)
        try:
            initial_body = await page.locator("body").inner_text(timeout=10_000)
        except Exception:
            initial_body = ""
        if contains_challenge(initial_body):
            await self._safe_screenshot(page)
            raise TargetChallengeError("检测到人机验证或挑战页，探测已停止")

        verification_input = page.locator(
            'input[autocomplete="one-time-code"][name="code"][maxlength="6"]'
        )
        continue_button = page.locator(
            'button[type="submit"][name="intent"][value="validate"]'
        )
        try:
            input_visible = await self._is_visible(verification_input)
        except Exception:
            input_visible = False
        if not input_visible:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_input_missing",
                "未找到可用的验证码输入框",
            )

        try:
            button_visible = await self._is_visible(continue_button)
        except Exception:
            button_visible = False
        if not button_visible:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_continue_button_missing",
                "未找到验证码页面的 Continue 按钮",
            )

        try:
            await verification_input.first.fill(
                verification_code,
                timeout=self.email_action_timeout_ms,
            )
            filled_value = await verification_input.first.input_value(
                timeout=self.email_action_timeout_ms
            )
        except Exception:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_fill_failed",
                "验证码填写失败",
            ) from None
        if filled_value != verification_code:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_value_mismatch",
                "验证码输入值校验失败",
            )

        generated_delay = float(
            self.random_uniform(
                self.pre_continue_delay_min_seconds,
                self.pre_continue_delay_max_seconds,
            )
        )
        delay_seconds = min(
            self.pre_continue_delay_max_seconds,
            max(self.pre_continue_delay_min_seconds, generated_delay),
        )
        await self.delay_sleep(delay_seconds)
        pre_continue_delay_ms = int(round(delay_seconds * 1000))

        refreshed_input = page.locator(
            'input[autocomplete="one-time-code"][name="code"][maxlength="6"]'
        )
        refreshed_button = page.locator(
            'button[type="submit"][name="intent"][value="validate"]'
        )
        try:
            value_before_click = await refreshed_input.first.input_value(
                timeout=self.email_action_timeout_ms
            )
            input_still_visible = await self._is_visible(refreshed_input)
        except Exception:
            value_before_click = ""
            input_still_visible = False
        if value_before_click != verification_code or not input_still_visible:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_form_reset",
                "验证码输入框在提交前被网页重置",
            )

        try:
            button_still_visible = await self._is_visible(refreshed_button)
        except Exception:
            button_still_visible = False
        if not button_still_visible:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_continue_button_missing",
                "未找到验证码页面的 Continue 按钮",
            )

        continue_attempts = 0
        submitted_at_utc: datetime | None = None
        active_button = refreshed_button
        while continue_attempts < 2:
            continue_attempts += 1
            submitted_at_utc = self.utc_now()
            if submitted_at_utc.tzinfo is None:
                raise ValueError("验证码提交时间必须包含时区")
            submitted_at_utc = submitted_at_utc.astimezone(timezone.utc)
            observation_timeout_ms = (
                self.verification_retry_timeout_ms
                if continue_attempts == 1
                else self.next_step_timeout_ms
            )
            observation_task = asyncio.create_task(
                self._wait_for_verification_next_step(
                    page,
                    initial_url,
                    timeout_ms=observation_timeout_ms,
                ),
                name="verification-next-step-observer",
            )
            click_task = asyncio.create_task(
                active_button.first.click(timeout=self.email_action_timeout_ms),
                name="verification-continue-click",
            )
            click_outcome, observation_outcome = await asyncio.gather(
                click_task,
                observation_task,
                return_exceptions=True,
            )

            if isinstance(click_outcome, asyncio.CancelledError):
                raise click_outcome
            click_completed = not isinstance(click_outcome, BaseException)
            click_exception_type = (
                _safe_exception_type(click_outcome)
                if isinstance(click_outcome, Exception)
                else (
                    "Exception"
                    if isinstance(click_outcome, BaseException)
                    else None
                )
            )

            if isinstance(observation_outcome, asyncio.CancelledError):
                raise observation_outcome
            if isinstance(observation_outcome, TargetChallengeError):
                raise observation_outcome
            if isinstance(observation_outcome, VerificationStepError):
                can_retry = (
                    continue_attempts == 1
                    and observation_outcome.code
                    == "verification_form_unchanged_after_click"
                )
                if can_retry:
                    active_button = await self._reload_verification_form_for_retry(
                        page,
                        verification_code,
                    )
                    continue
                diagnosed = observation_outcome.with_click_diagnostics(
                    click_completed=click_completed,
                    click_exception_type=click_exception_type,
                    continue_attempts=continue_attempts,
                )
                raise diagnosed from None
            if isinstance(observation_outcome, BaseException):
                await self._safe_screenshot(page)
                raise VerificationStepError(
                    "verification_next_step_unknown",
                    "验证码提交后页面状态检查失败",
                    continue_attempts=continue_attempts,
                    click_completed=click_completed,
                    click_exception_type=click_exception_type,
                    post_click_state="observation_failed",
                ) from None

            next_step = observation_outcome
            return VerificationSubmitResult(
                final_url=sanitize_url(page.url),
                next_step=next_step.next_step,
                pre_continue_delay_ms=pre_continue_delay_ms,
                submitted_at_utc=submitted_at_utc,
                continue_attempts=continue_attempts,
                click_completed=click_completed,
                click_exception_type=click_exception_type,
                post_click_state=next_step.post_click_state,
                wait_elapsed_ms=next_step.wait_elapsed_ms,
                url_changed=next_step.url_changed,
                input_visible_at_end=next_step.input_visible_at_end,
                button_visible_at_end=next_step.button_visible_at_end,
            )

        raise RuntimeError("验证码提交状态机意外结束")

    async def _reload_verification_form_for_retry(
        self,
        page: Any,
        verification_code: str,
    ) -> Any:
        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=self.login_navigation_timeout_ms,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_retry_reload_failed",
                "验证码提交无响应，刷新验证页失败",
                continue_attempts=1,
                post_click_state="reload_failed",
            ) from None

        try:
            body_text = await page.locator("body").inner_text(timeout=10_000)
        except Exception:
            body_text = ""
        if contains_challenge(body_text):
            await self._safe_screenshot(page)
            raise TargetChallengeError("检测到人机验证或挑战页，探测已停止")

        verification_input = page.locator(
            'input[autocomplete="one-time-code"][name="code"][maxlength="6"]'
        )
        continue_button = page.locator(
            'button[type="submit"][name="intent"][value="validate"]'
        )
        try:
            input_visible = await self._is_visible(verification_input)
            button_visible = await self._is_visible(continue_button)
        except Exception:
            input_visible = False
            button_visible = False
        if not input_visible or not button_visible:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_retry_form_missing",
                "刷新后未找到可用的验证码提交表单",
                continue_attempts=1,
                post_click_state="reload_form_missing",
            )

        try:
            await verification_input.first.fill(
                verification_code,
                timeout=self.email_action_timeout_ms,
            )
            filled_value = await verification_input.first.input_value(
                timeout=self.email_action_timeout_ms,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_retry_fill_failed",
                "刷新后验证码重新填写失败",
                continue_attempts=1,
                post_click_state="reload_fill_failed",
            ) from None
        if filled_value != verification_code:
            await self._safe_screenshot(page)
            raise VerificationStepError(
                "verification_retry_value_mismatch",
                "刷新后验证码输入值校验失败",
                continue_attempts=1,
                post_click_state="reload_value_mismatch",
            )
        return continue_button

    async def _wait_for_profile_route(
        self,
        page: Any,
    ) -> str | _ProfileFormControls:
        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        saw_profile_page = False
        name_visible = False
        second_visible = False
        auth_retry_count = 0

        while True:
            remaining_ms = max(1, int((deadline - monotonic()) * 1000))
            try:
                body = await page.locator("body").inner_text(
                    timeout=min(1_000, remaining_ms)
                )
            except Exception:
                body = ""
            if contains_challenge(body):
                await self._safe_screenshot(page)
                raise TargetChallengeError("检测到人机验证或挑战页，探测已停止")

            current_url = sanitize_url(page.url)
            parsed = urlsplit(current_url)
            host = (parsed.hostname or "").casefold()
            path = parsed.path.rstrip("/")
            trusted_auth_host = host in {
                "auth.openai.com",
                "auth0.openai.com",
                "accounts.openai.com",
                "chatgpt.com",
            }
            controls = (
                await self._profile_form_controls(page) if trusted_auth_host else None
            )
            profile_route = self._is_profile_path(path) or path.endswith(
                "/email-verification"
            ) or "/email-verification/" in path
            if trusted_auth_host and profile_route:
                saw_profile_page = True
                if controls is not None:
                    return controls
                name_visible, second_visible = await self._profile_field_presence(page)

            if auth_retry_count < 2 and await self._click_auth_retry_if_available(
                page, body
            ):
                auth_retry_count += 1
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            if host == "chatgpt.com" and not self._is_profile_path(path):
                for candidate in self._profile_button_candidates(page):
                    try:
                        if await self._is_visible(candidate):
                            return "already_configured"
                    except Exception:
                        continue

            if monotonic() >= deadline:
                await self._safe_screenshot(page)
                if saw_profile_page and not name_visible:
                    raise ProfileStepError(
                        "profile_name_input_missing",
                        "未找到姓名输入框",
                    )
                if saw_profile_page and not second_visible:
                    raise ProfileStepError(
                        "profile_age_input_missing",
                        "未找到年龄或生日输入框",
                    )
                raise ProfileStepError(
                    "profile_page_missing",
                    "未进入账号资料页面且未确认 ChatGPT 主界面",
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def complete_profile_if_needed(self) -> ProfileCompletionResult:
        page = await self._page()
        profile_route = await self._wait_for_profile_route(page)
        if profile_route == "already_configured":
            return ProfileCompletionResult(
                final_url=sanitize_url(page.url),
                next_step="account_created",
                skipped=True,
                skip_reason="already_configured",
                full_name=None,
                age=None,
                name_to_age_delay_ms=None,
                age_to_finish_delay_ms=None,
                submitted_at_utc=None,
                form_variant="already_configured",
                locator_strategy="account_home",
                submit_variant="not_applicable",
            )
        if not isinstance(profile_route, _ProfileFormControls):
            raise ProfileStepError(
                "profile_page_missing",
                "未进入账号资料页面且未确认 ChatGPT 主界面",
            )

        initial_url = sanitize_url(page.url)
        controls = profile_route
        form_variant = controls.variant
        locator_strategy = controls.locator_strategy

        def profile_error(
            code: str,
            message: str,
            *,
            submit_variant: str = "unresolved",
        ) -> ProfileStepError:
            return ProfileStepError(
                code,
                message,
                form_variant=form_variant,
                locator_strategy=locator_strategy,
                submit_variant=submit_variant,
            )

        async def refreshed_controls() -> _ProfileFormControls:
            refreshed = await self._profile_form_controls(page)
            if refreshed is None or refreshed.variant != form_variant:
                await self._safe_screenshot(page)
                raise profile_error(
                    "profile_form_reset",
                    "账号资料表单被网页重置或切换为未知变体",
                )
            return refreshed

        first_name = self.random_choice(ENGLISH_FIRST_NAMES)
        last_name = self.random_choice(ENGLISH_LAST_NAMES)
        full_name = f"{first_name} {last_name}"
        if re.fullmatch(r"[A-Za-z]+ [A-Za-z]+", full_name) is None:
            raise ValueError("随机英文姓名生成结果无效")
        age = min(35, max(25, int(self.random_randint(25, 35))))
        birthday: date | None = None
        second_value = str(age)
        if form_variant == "birthday":
            reference = self.utc_now()
            if reference.tzinfo is None:
                raise ValueError("生日生成基准时间必须包含时区")
            birthday = self._birthday_for_age(reference.astimezone(timezone.utc).date(), age)
            second_value = birthday.isoformat()

        try:
            await controls.name_input.fill(
                full_name,
                timeout=self.email_action_timeout_ms,
            )
            filled_name = await controls.name_input.input_value(
                timeout=self.email_action_timeout_ms
            )
        except Exception:
            await self._safe_screenshot(page)
            raise profile_error(
                "profile_name_fill_failed",
                "英文姓名填写失败",
            ) from None
        if filled_name != full_name:
            await self._safe_screenshot(page)
            raise profile_error(
                "profile_name_value_mismatch",
                "英文姓名输入值校验失败",
            )

        generated_name_delay = float(
            self.random_uniform(
                self.profile_action_delay_min_seconds,
                self.profile_action_delay_max_seconds,
            )
        )
        name_delay_seconds = min(
            self.profile_action_delay_max_seconds,
            max(self.profile_action_delay_min_seconds, generated_name_delay),
        )
        await self.delay_sleep(name_delay_seconds)
        name_to_age_delay_ms = int(round(name_delay_seconds * 1000))

        controls = await refreshed_controls()
        locator_strategy = controls.locator_strategy
        try:
            name_before_age = await controls.name_input.input_value(
                timeout=self.email_action_timeout_ms
            )
            name_still_visible = await controls.name_input.is_visible()
            second_still_visible = await self._profile_second_visible(controls)
        except Exception:
            name_before_age = ""
            name_still_visible = False
            second_still_visible = False
        if (
            name_before_age != full_name
            or not name_still_visible
            or not second_still_visible
        ):
            await self._safe_screenshot(page)
            raise profile_error(
                "profile_form_reset",
                "账号资料表单在填写第二字段前被网页重置",
            )

        try:
            filled_second = await self._fill_profile_second(
                controls,
                second_value,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise profile_error(
                "profile_age_fill_failed" if form_variant == "numeric_age" else "profile_birthday_fill_failed",
                "年龄填写失败" if form_variant == "numeric_age" else "生日填写失败",
            ) from None
        if filled_second != second_value:
            await self._safe_screenshot(page)
            raise profile_error(
                "profile_age_value_mismatch" if form_variant == "numeric_age" else "profile_birthday_value_mismatch",
                "年龄输入值校验失败" if form_variant == "numeric_age" else "生日输入值校验失败",
            )

        generated_age_delay = float(
            self.random_uniform(
                self.profile_action_delay_min_seconds,
                self.profile_action_delay_max_seconds,
            )
        )
        age_delay_seconds = min(
            self.profile_action_delay_max_seconds,
            max(self.profile_action_delay_min_seconds, generated_age_delay),
        )
        await self.delay_sleep(age_delay_seconds)
        age_to_finish_delay_ms = int(round(age_delay_seconds * 1000))

        controls = await refreshed_controls()
        locator_strategy = controls.locator_strategy
        try:
            final_name = await controls.name_input.input_value(
                timeout=self.email_action_timeout_ms
            )
            final_second = await self._profile_second_value(controls)
            name_final_visible = await controls.name_input.is_visible()
            second_final_visible = await self._profile_second_visible(controls)
        except Exception:
            final_name = ""
            final_second = ""
            name_final_visible = False
            second_final_visible = False
        if (
            final_name != full_name
            or final_second != second_value
            or not name_final_visible
            or not second_final_visible
        ):
            await self._safe_screenshot(page)
            raise profile_error(
                "profile_form_reset",
                "账号资料表单在提交前被网页重置",
            )

        finish_button, submit_variant = await self._profile_submit_control(controls)
        try:
            finish_visible = finish_button is not None and await finish_button.is_visible()
            finish_enabled = finish_button is not None and await finish_button.is_enabled()
        except Exception:
            finish_visible = False
            finish_enabled = False
        if not finish_visible or not finish_enabled:
            await self._safe_screenshot(page)
            raise profile_error(
                "profile_finish_button_missing",
                "未在账号资料表单内找到可用的提交按钮",
                submit_variant=submit_variant,
            )

        submitted_at_utc = self.utc_now()
        if submitted_at_utc.tzinfo is None:
            raise ValueError("账号资料提交时间必须包含时区")
        submitted_at_utc = submitted_at_utc.astimezone(timezone.utc)
        try:
            await finish_button.click(timeout=self.email_action_timeout_ms)
        except Exception:
            if await self._wait_for_confirmed_chatgpt_home(page):
                next_step = "account_created"
            else:
                await self._safe_screenshot(page)
                raise profile_error(
                    "profile_finish_click_failed",
                    "账号资料提交按钮点击失败且未确认 ChatGPT 主界面",
                    submit_variant=submit_variant,
                ) from None
        else:
            next_step = await self._wait_for_profile_next_step(
                page,
                initial_url,
                form_variant=form_variant,
                locator_strategy=locator_strategy,
                submit_variant=submit_variant,
            )
        return ProfileCompletionResult(
            final_url=sanitize_url(page.url),
            next_step=next_step,
            skipped=False,
            skip_reason=None,
            full_name=full_name,
            age=age,
            name_to_age_delay_ms=name_to_age_delay_ms,
            age_to_finish_delay_ms=age_to_finish_delay_ms,
            submitted_at_utc=submitted_at_utc,
            form_variant=form_variant,
            locator_strategy=locator_strategy,
            submit_variant=submit_variant,
        )

    async def extract_chatgpt_access_token(self) -> AccessTokenExtractionResult:
        page = await self._page()
        main_url = sanitize_url(page.url)
        if not await self._wait_for_confirmed_chatgpt_home(page):
            raise AccessTokenExtractionError(
                "session_precondition",
                "session_home_not_confirmed",
                "提取 AccessToken 前未确认 ChatGPT 主界面",
            )

        try:
            response = await page.goto(
                CHATGPT_SESSION_URL,
                wait_until="domcontentloaded",
                timeout=self.session_navigation_timeout_ms,
            )
        except Exception:
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "session_navigation",
                "session_navigation_failed",
                "ChatGPT Session 页面导航失败",
                homepage_restored=restored,
            ) from None

        if not self._is_exact_chatgpt_session_url(str(getattr(page, "url", ""))):
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "session_response",
                "session_response_untrusted",
                "ChatGPT Session 导航进入了非可信页面",
                homepage_restored=restored,
            )
        if response is None or int(getattr(response, "status", 0)) != 200:
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "session_response",
                "session_http_failed",
                "ChatGPT Session 接口未返回成功响应",
                homepage_restored=restored,
            )

        headers = getattr(response, "headers", {})
        content_type = ""
        if isinstance(headers, dict):
            content_type = str(headers.get("content-type", ""))
        if "application/json" not in content_type.casefold():
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "session_response",
                "session_json_invalid",
                "ChatGPT Session 响应不是 JSON",
                homepage_restored=restored,
            )

        try:
            raw_body = await response.body()
        except Exception:
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "session_response",
                "session_response_read_failed",
                "ChatGPT Session 响应读取失败",
                homepage_restored=restored,
            ) from None
        if not isinstance(raw_body, bytes) or len(raw_body) > SESSION_RESPONSE_MAX_BYTES:
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "session_response",
                "session_response_too_large",
                "ChatGPT Session 响应大小超出限制",
                homepage_restored=restored,
            )

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "session_response",
                "session_json_invalid",
                "ChatGPT Session JSON 格式无效",
                homepage_restored=restored,
            ) from None
        finally:
            raw_body = b""

        token = payload.get("accessToken") if isinstance(payload, dict) else None
        payload = None
        if not isinstance(token, str) or not token:
            restored = await self._restore_chatgpt_home(page)
            raise AccessTokenExtractionError(
                "access_token",
                "access_token_missing",
                "ChatGPT Session 响应缺少 AccessToken",
                homepage_restored=restored,
            )

        extracted_at = self.utc_now()
        if extracted_at.tzinfo is None:
            raise ValueError("AccessToken 提取时间必须包含时区")
        extracted_at = extracted_at.astimezone(timezone.utc)
        try:
            expires_at = self._access_token_expiry(token, extracted_at)
        except ValueError as exc:
            restored = await self._restore_chatgpt_home(page)
            code = str(exc)
            raise AccessTokenExtractionError(
                "access_token",
                code,
                (
                    "ChatGPT AccessToken 已过期"
                    if code == "access_token_expired"
                    else "ChatGPT AccessToken 格式无效"
                ),
                homepage_restored=restored,
            ) from None

        restored = await self._restore_chatgpt_home(page)
        return AccessTokenExtractionResult(
            access_token=token,
            expires_at_utc=expires_at,
            extracted_at_utc=extracted_at,
            final_url=(sanitize_url(page.url) if restored else main_url),
            homepage_restored=restored,
        )

    async def extract_chatgpt_account_plan(
        self,
        access_token: str,
    ) -> AccountPlanResult:
        page = await self._page()
        if not await self._wait_for_confirmed_chatgpt_home(page):
            raise PlanCheckError("plan_home_not_confirmed")

        try:
            browser_context = await page.evaluate(
                """
                () => {
                  const deviceCookie = document.cookie
                    .split(';')
                    .map(value => value.trim())
                    .find(value => value.startsWith('oai-did='));
                  return {
                    language: String(navigator.language || 'en-US'),
                    timezoneOffset: String(new Date().getTimezoneOffset()),
                    deviceId: deviceCookie
                      ? decodeURIComponent(deviceCookie.slice('oai-did='.length))
                      : '',
                  };
                }
                """
            )
        except Exception:
            browser_context = {}
        if not isinstance(browser_context, dict):
            browser_context = {}
        language = str(browser_context.get("language") or "en-US").strip()[:32]
        timezone_offset = str(
            browser_context.get("timezoneOffset") or "0"
        ).strip()
        try:
            timezone_offset_value = int(timezone_offset)
        except ValueError:
            timezone_offset_value = 0
        timezone_offset = str(min(840, max(-840, timezone_offset_value)))
        device_id = str(browser_context.get("deviceId") or "").strip()[:128]
        plan_url = (
            f"{ACCOUNTS_CHECK_URL}?timezone_offset_min={timezone_offset}"
        )

        route_pattern = f"**{ACCOUNTS_CHECK_PATH}*"

        async def authorize_route(route: Any) -> None:
            headers = dict(getattr(route.request, "headers", {}) or {})
            headers.update(
                plan_request_headers(
                    access_token,
                    language=language,
                    device_id=device_id or None,
                )
            )
            await route.continue_(headers=headers)

        response = None
        result: AccountPlanResult | None = None
        raw_body = b""
        error: PlanCheckError | None = None
        route_installed = False
        try:
            try:
                await page.route(route_pattern, authorize_route)
                route_installed = True
            except Exception:
                error = PlanCheckError("plan_route_install_failed")
            if error is None:
                try:
                    response = await page.goto(
                        plan_url,
                        wait_until="domcontentloaded",
                        timeout=self.session_navigation_timeout_ms,
                    )
                except Exception:
                    error = PlanCheckError("plan_navigation_failed", retryable=True)
            if error is None and not self._is_exact_chatgpt_plan_url(
                str(getattr(page, "url", "")),
                timezone_offset=timezone_offset,
            ):
                error = PlanCheckError("plan_response_untrusted")
            if error is None:
                status = int(getattr(response, "status", 0)) if response else 0
                if status == 401:
                    error = PlanCheckError(
                        "access_token_unauthorized", http_status=status
                    )
                elif not 200 <= status < 300:
                    error = PlanCheckError(
                        "plan_http_failed",
                        http_status=status or None,
                        retryable=(
                            status in {408, 409, 425, 429} or status >= 500
                        ),
                    )
            if error is None:
                try:
                    raw_body = await response.body()
                except Exception:
                    error = PlanCheckError("plan_response_read_failed")
            if error is None and (
                not isinstance(raw_body, bytes)
                or len(raw_body) > PLAN_RESPONSE_MAX_BYTES
            ):
                error = PlanCheckError("plan_response_too_large")
            if error is None:
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    error = PlanCheckError("plan_response_invalid")
                else:
                    if not isinstance(payload, dict):
                        error = PlanCheckError("plan_response_invalid")
                    else:
                        try:
                            result = parse_accounts_check(
                                payload,
                                access_token=access_token,
                                checked_at=self.utc_now(),
                            )
                        except PlanCheckError as exc:
                            error = exc
        finally:
            raw_body = b""
            if route_installed:
                try:
                    await page.unroute(route_pattern, authorize_route)
                except Exception:
                    pass
            restored = await self._restore_chatgpt_home(page)
        if not restored:
            raise PlanCheckError("plan_home_restore_failed")
        if error is not None:
            raise error
        if result is None:
            raise PlanCheckError("plan_response_invalid")
        return result

    async def extract_chatgpt_checkout_type(
        self,
        access_token: str,
        *,
        country: str = "JP",
    ) -> CheckoutTypeResult:
        page = await self._page()
        if not await self._wait_for_confirmed_chatgpt_home(page):
            raise CheckoutTypeCheckError("checkout_type_home_not_confirmed")
        token = normalize_access_token(access_token)
        if not token:
            raise CheckoutTypeCheckError("access_token_missing")
        try:
            response = await page.evaluate(
                """
                async ({ url, path, token, country, deviceId }) => {
                  const result = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                      'accept': 'application/json',
                      'authorization': `Bearer ${token}`,
                      'content-type': 'application/json',
                      'oai-device-id': deviceId,
                      'oai-language': 'ja-JP',
                      'x-openai-target-path': path,
                      'x-openai-target-route': path,
                    },
                    body: JSON.stringify({
                      entry_point: 'all_plans_pricing_modal',
                      plan_name: 'chatgptplusplan',
                      billing_details: { country, currency: 'JPY' },
                      checkout_ui_mode: 'custom',
                    }),
                  });
                  const body = (await result.text()).slice(0, 524288);
                  return { status: result.status, body };
                }
                """,
                {
                    "url": CHATGPT_CHECKOUT_URL,
                    "path": CHATGPT_CHECKOUT_PATH,
                    "token": token,
                    "country": country.upper(),
                    "deviceId": str(uuid4()),
                },
            )
        except Exception as exc:
            raise CheckoutTypeCheckError("checkout_type_request_failed") from exc
        if not isinstance(response, dict):
            raise CheckoutTypeCheckError("checkout_type_response_invalid")
        status = int(response.get("status") or 0)
        if not 200 <= status < 300:
            raise CheckoutTypeCheckError(
                "checkout_type_http_failed", http_status=status or None
            )
        try:
            payload = json.loads(str(response.get("body") or ""))
        except json.JSONDecodeError as exc:
            raise CheckoutTypeCheckError("checkout_type_response_invalid") from exc
        return parse_checkout_type_response(payload)

    async def verification_factor(self) -> str:
        page = await self._page()
        try:
            body = await page.locator("body").inner_text(timeout=2_000)
        except Exception:
            body = ""
        return "totp" if AUTHENTICATOR_FACTOR_PATTERN.search(body) else "email"

    async def submit_totp_challenge(
        self,
        secret: str,
    ) -> VerificationSubmitResult:
        if await self.verification_factor() != "totp":
            raise TotpEnrollmentError(
                "existing_login",
                "totp_challenge_missing",
                "当前页面不是认证器验证码页面",
            )
        remaining = 30 - (time() % 30)
        if remaining < 4:
            await self.delay_sleep(remaining + 0.25)
        try:
            return await self.submit_verification_code_and_continue(
                generate_totp(secret)
            )
        except VerificationStepError as exc:
            raise TotpEnrollmentError(
                "existing_login",
                "totp_challenge_failed",
                "认证器验证码登录失败",
            ) from exc

    async def begin_totp_enrollment(
        self,
        email: str,
    ) -> TotpEnrollmentChallenge:
        page = await self._page()
        if not await self._is_confirmed_chatgpt_home(page):
            if not await self._restore_chatgpt_home(page):
                raise TotpEnrollmentError(
                    "totp_reauth",
                    "totp_home_not_confirmed",
                    "启用 2FA 前未确认 ChatGPT 登录主页",
                )
        requested_at = self.utc_now()
        if requested_at.tzinfo is None:
            raise ValueError("2FA 重认证时间必须包含时区")
        requested_at = requested_at.astimezone(timezone.utc)
        try:
            response = await page.evaluate(
                """
                async ({ email }) => {
                  const csrfResponse = await fetch('https://chatgpt.com/api/auth/csrf', {
                    credentials: 'include',
                    headers: {'accept': 'application/json'},
                  });
                  const csrfData = await csrfResponse.json().catch(() => ({}));
                  const csrfToken = String(csrfData.csrfToken || '');
                  if (!csrfResponse.ok || !csrfToken) {
                    return {ok:false, stage:'csrf', status:csrfResponse.status};
                  }
                  const query = new URLSearchParams({
                    connection: 'password',
                    login_hint: email,
                    reauth: 'password',
                    max_age: '0',
                  });
                  const body = new URLSearchParams({
                    callbackUrl: 'https://chatgpt.com/?action=enable&factor=totp',
                    csrfToken,
                    json: 'true',
                  });
                  const signinResponse = await fetch(
                    `https://chatgpt.com/api/auth/signin/openai?${query.toString()}`,
                    {
                      method: 'POST',
                      credentials: 'include',
                      headers: {
                        'accept': 'application/json',
                        'content-type': 'application/x-www-form-urlencoded',
                      },
                      body: body.toString(),
                    },
                  );
                  const signinData = await signinResponse.json().catch(() => ({}));
                  return {
                    ok: signinResponse.ok && !!signinData.url,
                    stage: 'signin',
                    status: signinResponse.status,
                    url: String(signinData.url || ''),
                  };
                }
                """,
                {"email": email},
            )
        except Exception:
            raise TotpEnrollmentError(
                "totp_reauth",
                "totp_reauth_request_failed",
                "2FA 重认证请求失败",
            ) from None
        if not isinstance(response, dict) or not response.get("ok"):
            raise TotpEnrollmentError(
                "totp_reauth",
                "totp_reauth_start_failed",
                "2FA 重认证启动失败",
                http_status=(
                    int(response.get("status") or 0)
                    if isinstance(response, dict)
                    else None
                ),
            )
        auth_url = str(response.get("url") or "")
        if not self._is_trusted_openai_page(auth_url):
            raise TotpEnrollmentError(
                "totp_reauth",
                "totp_reauth_url_untrusted",
                "2FA 重认证返回了不受信任的地址",
            )
        try:
            await page.goto(
                auth_url,
                wait_until="domcontentloaded",
                timeout=self.login_navigation_timeout_ms,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise TotpEnrollmentError(
                "totp_reauth",
                "totp_reauth_navigation_failed",
                "2FA 重认证页面加载失败",
            ) from None

        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        verification_input = page.locator(
            'input[autocomplete="one-time-code"][name="code"][maxlength="6"]'
        )
        while monotonic() < deadline:
            try:
                body = await page.locator("body").inner_text(timeout=1_000)
            except Exception:
                body = ""
            if AUTHENTICATOR_FACTOR_PATTERN.search(body):
                await self._safe_screenshot(page)
                raise TotpEnrollmentError(
                    "totp_reauth",
                    "totp_already_enabled",
                    "账号已要求认证器验证码，不能再次创建 2FA Secret",
                )
            try:
                if await self._is_visible(verification_input):
                    return TotpEnrollmentChallenge(
                        requested_at_utc=requested_at,
                        final_url=sanitize_url(page.url),
                    )
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval_seconds)
        await self._safe_screenshot(page)
        raise TotpEnrollmentError(
            "totp_reauth",
            "totp_reauth_email_code_missing",
            "2FA 重认证未进入邮箱验证码页面",
        )

    async def complete_totp_enrollment(
        self,
        email_code: str,
    ) -> TotpEnrollmentResult:
        try:
            await self.submit_verification_code_and_continue(email_code)
        except (VerificationStepError, TargetChallengeError) as exc:
            raise TotpEnrollmentError(
                "totp_reauth",
                "totp_reauth_email_code_failed",
                "2FA 重认证邮箱验证码提交失败",
            ) from exc
        try:
            token_result = await self.extract_chatgpt_access_token()
        except AccessTokenExtractionError as exc:
            raise TotpEnrollmentError(
                "totp_session",
                "totp_session_refresh_failed",
                "2FA 重认证后未取得新的 Session Token",
            ) from exc

        page = await self._page()
        device_id = ""
        try:
            cookies = await page.context.cookies("https://chatgpt.com")
            device_id = next(
                (
                    str(cookie.get("value") or "")
                    for cookie in cookies
                    if cookie.get("name") == "oai-did"
                ),
                "",
            )
        except Exception:
            device_id = ""

        async def request(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            try:
                result = await page.evaluate(
                    """
                    async ({ url, token, deviceId, body }) => {
                      const headers = {
                        'accept': 'application/json',
                        'authorization': `Bearer ${token}`,
                        'content-type': 'application/json',
                        'oai-language': navigator.language || 'en-US',
                      };
                      if (deviceId) headers['oai-device-id'] = deviceId;
                      const response = await fetch(url, {
                        method: body === null ? 'GET' : 'POST',
                        credentials: 'include',
                        headers,
                        body: body === null ? undefined : JSON.stringify(body),
                      });
                      const data = await response.json().catch(() => ({}));
                      return {ok:response.ok, status:response.status, data};
                    }
                    """,
                    {
                        "url": url,
                        "token": token_result.access_token,
                        "deviceId": device_id,
                        "body": body,
                    },
                )
            except Exception:
                raise TotpEnrollmentError(
                    "totp_api",
                    "totp_api_request_failed",
                    "2FA 接口请求失败",
                ) from None
            return result if isinstance(result, dict) else {}

        enroll = await request(CHATGPT_MFA_ENROLL_URL, {"factor_type": "totp"})
        enroll_data = enroll.get("data") if isinstance(enroll.get("data"), dict) else {}
        try:
            secret = normalize_totp_secret(str(enroll_data.get("secret") or ""))
        except TotpSecretError as exc:
            raise TotpEnrollmentError(
                "totp_enroll",
                "totp_enroll_response_invalid",
                "2FA enroll 响应缺少有效 Secret",
                http_status=int(enroll.get("status") or 0) or None,
            ) from exc
        session_id = str(enroll_data.get("session_id") or "")
        if not enroll.get("ok") or not session_id:
            raise TotpEnrollmentError(
                "totp_enroll",
                "totp_enroll_failed",
                "2FA TOTP enroll 失败",
                http_status=int(enroll.get("status") or 0) or None,
            )

        remaining = 30 - (time() % 30)
        if remaining < 4:
            await self.delay_sleep(remaining + 0.25)
        totp_code = generate_totp(secret)
        activated = await request(
            CHATGPT_MFA_ACTIVATE_URL,
            {
                "code": totp_code,
                "factor_type": "totp",
                "session_id": session_id,
            },
        )
        activated_data = (
            activated.get("data")
            if isinstance(activated.get("data"), dict)
            else {}
        )
        if not activated.get("ok") or activated_data.get("success") is not True:
            raise TotpEnrollmentError(
                "totp_activate",
                "totp_activate_failed",
                "2FA TOTP 激活失败",
                http_status=int(activated.get("status") or 0) or None,
            )
        validation = await request(CHATGPT_MODELS_URL, None)
        if not validation.get("ok"):
            raise TotpEnrollmentError(
                "totp_validate",
                "totp_token_validation_failed",
                "2FA 已激活，但新的 Session Token 验证失败",
                http_status=int(validation.get("status") or 0) or None,
            )
        activated_at = self.utc_now()
        if activated_at.tzinfo is None:
            raise ValueError("2FA 激活时间必须包含时区")
        return TotpEnrollmentResult(
            secret=secret,
            access_token=token_result.access_token,
            access_token_expires_at_utc=token_result.expires_at_utc,
            activated_at_utc=activated_at.astimezone(timezone.utc),
            final_url=sanitize_url(page.url),
        )

    async def add_password_in_settings(
        self,
        password: str,
        totp_secret: str,
        email_code_provider: Callable[[datetime], Awaitable[str]],
        *,
        timeout_seconds: float = 120,
    ) -> PasswordSetupResult:
        if (
            len(password) < 12
            or not re.search(r"[A-Z]", password)
            or not re.search(r"[a-z]", password)
            or not re.search(r"[0-9]", password)
            or not re.search(r"[^A-Za-z0-9]", password)
        ):
            raise PasswordStepError(
                "generated_password_invalid",
                "生成的账号密码不符合强度要求",
            )
        normalized_secret = normalize_totp_secret(totp_secret)
        page = await self._page()
        requested_at = self.utc_now()
        if requested_at.tzinfo is None:
            raise ValueError("添加密码重认证时间必须包含时区")
        requested_at = requested_at.astimezone(timezone.utc)
        try:
            reauth = await page.evaluate(
                """
                async () => {
                  const sessionResponse = await fetch('/api/auth/session', {
                    credentials: 'include',
                    headers: {'accept': 'application/json'},
                  });
                  const session = await sessionResponse.json().catch(() => ({}));
                  const email = String(session?.user?.email || '');
                  if (!sessionResponse.ok || !session?.accessToken || !email) {
                    return {ok:false, stage:'session', status:sessionResponse.status};
                  }
                  const deviceCookie = document.cookie
                    .split(';')
                    .map(value => value.trim())
                    .find(value => value.startsWith('oai-did='));
                  const deviceId = deviceCookie
                    ? decodeURIComponent(deviceCookie.slice('oai-did='.length))
                    : crypto.randomUUID();
                  const csrfResponse = await fetch('/api/auth/csrf', {
                    credentials: 'include',
                    headers: {'accept': 'application/json'},
                  });
                  const csrf = await csrfResponse.json().catch(() => ({}));
                  const csrfToken = String(csrf.csrfToken || '');
                  if (!csrfResponse.ok || !csrfToken) {
                    return {ok:false, stage:'csrf', status:csrfResponse.status};
                  }
                  const query = new URLSearchParams({
                    post_login_add_password: 'true',
                    prompt: 'login',
                    max_age: '0',
                    login_hint: email,
                    'ext-oai-did': deviceId,
                  });
                  const body = new URLSearchParams({
                    csrfToken,
                    callbackUrl: 'https://chatgpt.com/?tm_action=password&tm_stage=password_done',
                    json: 'true',
                  });
                  const signinResponse = await fetch(
                    `/api/auth/signin/openai?${query.toString()}`,
                    {
                      method: 'POST',
                      credentials: 'include',
                      headers: {
                        'accept': 'application/json',
                        'content-type': 'application/x-www-form-urlencoded',
                      },
                      body: body.toString(),
                    },
                  );
                  const signin = await signinResponse.json().catch(() => ({}));
                  return {
                    ok: signinResponse.ok && !!signin.url,
                    stage: 'signin',
                    status: signinResponse.status,
                    url: String(signin.url || ''),
                  };
                }
                """
            )
        except Exception:
            raise PasswordStepError(
                "password_reauth_request_failed",
                "添加密码重认证请求失败",
            ) from None
        if not isinstance(reauth, dict) or not reauth.get("ok"):
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_reauth_start_failed",
                "添加密码重认证启动失败",
            )
        auth_url = str(reauth.get("url") or "")
        if not self._is_trusted_openai_page(auth_url):
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_reauth_url_untrusted",
                "添加密码重认证返回了非可信地址",
            )
        try:
            await page.goto(
                auth_url,
                wait_until="domcontentloaded",
                timeout=self.security_navigation_timeout_ms,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise PasswordStepError(
                "password_reauth_navigation_failed",
                "添加密码重认证页面加载失败",
            ) from None

        async def submit_visible_code(code: str) -> bool:
            code_input = page.locator(
                'input[autocomplete="one-time-code"], input[name="code"], '
                'input[name="otp"], input[inputmode="numeric"]'
            )
            if not await self._is_visible(code_input):
                # The re-auth page can replace the code form while the mailbox
                # provider is waiting. Re-observe the current page instead of
                # treating that normal transition as a terminal failure.
                return False
            try:
                await code_input.first.fill(
                    code,
                    timeout=self.email_action_timeout_ms,
                )
                scope = code_input.first.locator(
                    "xpath=ancestor::form[1]"
                )
                submit = scope.locator(
                    'button[type="submit"], input[type="submit"], button[name="intent"]'
                )
                if not await self._is_visible(submit):
                    submit = page.locator(
                        'button[type="submit"], input[type="submit"], button[name="intent"]'
                    )
                await submit.first.click(timeout=self.email_action_timeout_ms)
            except Exception:
                try:
                    if not await self._is_visible(code_input):
                        return False
                except Exception:
                    return False
                raise PasswordStepError(
                    "password_reauth_code_submit_failed",
                    "添加密码重认证验证码提交失败",
                ) from None
            return True

        deadline = monotonic() + max(30.0, float(timeout_seconds))
        email_used = False
        totp_used = False
        email_resend_attempted = False
        password_submitted = False
        password_inputs_missing_since: float | None = None
        while monotonic() < deadline:
            try:
                body = await page.locator("body").inner_text(timeout=1_000)
            except Exception:
                body = ""
            current_url = sanitize_url(page.url)
            path = urlsplit(current_url).path.rstrip("/")
            password_inputs = page.locator(
                'input[type="password"], input[autocomplete="new-password"]'
            )
            visible_password_inputs: list[Any] = []
            try:
                count = await password_inputs.count()
                for index in range(count):
                    candidate = password_inputs.nth(index)
                    if await candidate.is_visible():
                        visible_password_inputs.append(candidate)
            except Exception:
                visible_password_inputs = []

            code_input = page.locator(
                'input[autocomplete="one-time-code"], input[name="code"], '
                'input[name="otp"], input[inputmode="numeric"]'
            )
            try:
                code_visible = await self._is_visible(code_input)
            except Exception:
                code_visible = False

            if code_visible and not visible_password_inputs:
                if AUTHENTICATOR_FACTOR_PATTERN.search(body):
                    if totp_used:
                        if VERIFICATION_REJECTION_PATTERN.search(body):
                            raise PasswordStepError(
                                "password_totp_reauth_rejected",
                                "添加密码的 TOTP 重认证未通过",
                            )
                        await asyncio.sleep(self.poll_interval_seconds)
                        continue
                    if await submit_visible_code(generate_totp(normalized_secret)):
                        totp_used = True
                    await self.delay_sleep(2)
                    continue
                if path.endswith("/email-verification") or VERIFICATION_PATTERN.search(body):
                    if email_used:
                        if VERIFICATION_REJECTION_PATTERN.search(body):
                            raise PasswordStepError(
                                "password_email_reauth_rejected",
                                "添加密码的邮箱验证码重认证未通过",
                            )
                        await asyncio.sleep(self.poll_interval_seconds)
                        continue
                    code_requested_at = requested_at
                    if not email_resend_attempted:
                        try:
                            resend_clicked = await page.evaluate(
                                """
                                () => {
                                  const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                                    && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                                  const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
                                  const elements = [...document.querySelectorAll('button,a,[role="button"],[role="link"]')]
                                    .filter(el => visible(el) && enabled(el));
                                  const target = elements.find(el => {
                                    const text = [el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
                                      .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
                                    return /resend|send again|send a new|new code|重新发送|再次发送|重发|重發|再送信|もう一度送信|コードを再送|다시\s*보내|재전송/.test(text);
                                  });
                                  if (!target) return false;
                                  target.scrollIntoView({block:'center'});
                                  target.click();
                                  return true;
                                }
                                """
                            )
                        except Exception:
                            resend_clicked = False
                        email_resend_attempted = True
                        if resend_clicked:
                            code_requested_at = self.utc_now()
                            if code_requested_at.tzinfo is None:
                                raise ValueError("添加密码验证码重发时间必须包含时区")
                            code_requested_at = code_requested_at.astimezone(timezone.utc)
                            await self.delay_sleep(1)
                    email_code = await email_code_provider(code_requested_at)
                    if await submit_visible_code(email_code):
                        email_used = True
                    await self.delay_sleep(2)
                    continue

            if visible_password_inputs and not password_submitted:
                try:
                    for candidate in visible_password_inputs:
                        await candidate.fill(
                            password,
                            timeout=self.email_action_timeout_ms,
                        )
                    scope = visible_password_inputs[0].locator(
                        "xpath=ancestor::form[1]"
                    )
                    submit = scope.locator(
                        'button[type="submit"], input[type="submit"]'
                    )
                    if not await self._is_visible(submit):
                        submit = page.get_by_role(
                            "button",
                            name=re.compile(
                                r"save|continue|submit|update|change|set|保存|继续|提交|更新|设置|続行|確認",
                                re.IGNORECASE,
                            ),
                        )
                    await submit.first.click(timeout=self.email_action_timeout_ms)
                except Exception:
                    raise PasswordStepError(
                        "password_settings_submit_failed",
                        "新密码填写或提交失败",
                    ) from None
                password_submitted = True
                await self.delay_sleep(2)
                continue

            success_text = re.search(
                r"password\s+(?:updated|changed|added|created)|"
                r"密码(?:已更新|已更改|已添加|设置成功)|"
                r"パスワード.*更新|비밀번호.*업데이트",
                body,
                re.IGNORECASE,
            )
            if password_submitted and success_text:
                configured_at = self.utc_now().astimezone(timezone.utc)
                return PasswordSetupResult(
                    final_url=current_url,
                    configured_at_utc=configured_at,
                    email_reauth_used=email_used,
                    totp_reauth_used=totp_used,
                )
            if password_submitted and not visible_password_inputs:
                if password_inputs_missing_since is None:
                    password_inputs_missing_since = monotonic()
                elif monotonic() - password_inputs_missing_since >= 3:
                    configured_at = self.utc_now().astimezone(timezone.utc)
                    return PasswordSetupResult(
                        final_url=current_url,
                        configured_at_utc=configured_at,
                        email_reauth_used=email_used,
                        totp_reauth_used=totp_used,
                    )
            else:
                password_inputs_missing_since = None
            await asyncio.sleep(self.poll_interval_seconds)
        await self._safe_screenshot(page)
        raise PasswordStepError(
            "password_settings_timeout",
            "添加密码流程超时",
        )

    async def _wait_for_confirmed_chatgpt_home(self, page: Any) -> bool:
        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        while True:
            if await self._is_confirmed_chatgpt_home(page):
                return True
            if monotonic() >= deadline:
                return False
            await asyncio.sleep(self.poll_interval_seconds)

    async def _is_confirmed_chatgpt_home(self, page: Any) -> bool:
        try:
            parsed = urlsplit(str(getattr(page, "url", "")))
            if (
                parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() != "chatgpt.com"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in {None, 443}
                or self._is_profile_path(parsed.path.rstrip("/"))
                or parsed.path.rstrip("/") == "/api/auth/session"
            ):
                return False
        except ValueError:
            return False
        for candidate in self._profile_button_candidates(page):
            try:
                if await self._is_visible(candidate):
                    return True
            except Exception:
                continue
        return False

    async def _restore_chatgpt_home(self, page: Any) -> bool:
        try:
            await page.goto(
                CHATGPT_HOME_URL,
                wait_until="domcontentloaded",
                timeout=self.session_navigation_timeout_ms,
            )
        except Exception:
            return False
        deadline = monotonic() + (self.email_action_timeout_ms / 1000)
        while monotonic() < deadline:
            if await self._is_confirmed_chatgpt_home(page):
                await self._safe_screenshot(page)
                return True
            await asyncio.sleep(self.poll_interval_seconds)
        return False

    @staticmethod
    def _is_exact_chatgpt_session_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            return (
                parsed.scheme.casefold() == "https"
                and (parsed.hostname or "").casefold() == "chatgpt.com"
                and parsed.username is None
                and parsed.password is None
                and parsed.port in {None, 443}
                and parsed.path.rstrip("/") == "/api/auth/session"
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    @staticmethod
    def _is_exact_chatgpt_plan_url(
        value: str,
        *,
        timezone_offset: str = "-480",
    ) -> bool:
        try:
            parsed = urlsplit(value)
            return (
                parsed.scheme.casefold() == "https"
                and (parsed.hostname or "").casefold() == "chatgpt.com"
                and parsed.username is None
                and parsed.password is None
                and parsed.port in {None, 443}
                and parsed.path.rstrip("/") == ACCOUNTS_CHECK_PATH
                and parsed.query == f"timezone_offset_min={timezone_offset}"
                and not parsed.fragment
            )
        except ValueError:
            return False

    @staticmethod
    def _access_token_expiry(token: str, now_utc: datetime) -> datetime:
        if (
            len(token) > ACCESS_TOKEN_MAX_CHARS
            or token.strip() != token
            or re.search(r"[\x00-\x20\x7f]", token)
        ):
            raise ValueError("access_token_invalid")
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError("access_token_invalid")
        try:
            header_segment = parts[0] + ("=" * (-len(parts[0]) % 4))
            payload_segment = parts[1] + ("=" * (-len(parts[1]) % 4))
            header_raw = base64.urlsafe_b64decode(header_segment.encode("ascii"))
            claims_raw = base64.urlsafe_b64decode(payload_segment.encode("ascii"))
            header = json.loads(header_raw.decode("utf-8"))
            claims = json.loads(claims_raw.decode("utf-8"))
        except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
            raise ValueError("access_token_invalid") from None
        algorithm = header.get("alg") if isinstance(header, dict) else None
        if not isinstance(algorithm, str) or not algorithm or algorithm.casefold() == "none":
            raise ValueError("access_token_invalid")
        exp = claims.get("exp") if isinstance(claims, dict) else None
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            raise ValueError("access_token_invalid")
        try:
            expires_at = datetime.fromtimestamp(float(exp), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError("access_token_invalid") from None
        if expires_at <= now_utc:
            raise ValueError("access_token_expired")
        return expires_at

    @staticmethod
    def _is_trusted_openai_page(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold()
        trusted_host = host in {"openai.com", "chatgpt.com"} or host.endswith(
            (".openai.com", ".chatgpt.com")
        )
        return (
            parsed.scheme.casefold() == "https"
            and trusted_host
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        )

    async def _close_other_security_pages(
        self,
        chosen: Any,
        pages: list[Any],
    ) -> None:
        for other in pages:
            if other is chosen:
                continue
            try:
                await other.close()
            except Exception:
                await self._safe_screenshot(chosen)
                raise SecurityNavigationError(
                    "security_key_page",
                    "security_extra_page_cleanup_failed",
                    "Passkey 页面已打开，但旧标签页清理失败",
                ) from None

    async def navigate_to_security_key_setup(self) -> SecurityNavigationResult:
        page = await self._page()
        try:
            await page.goto(
                CHATGPT_PASSKEY_SETTINGS_URL,
                wait_until="domcontentloaded",
                timeout=self.security_navigation_timeout_ms,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise SecurityNavigationError(
                "security_settings",
                "security_settings_navigation_failed",
                "ChatGPT Passkeys 设置页加载失败",
            ) from None

        await self._raise_security_challenge(page, "security_settings")
        delay_ms = await self._security_action_delay()

        final_label = await self._require_security_locator(
            page,
            [
                page.get_by_role(
                    "button",
                    name=re.compile(
                        r"^Add a Security key or Passkey$",
                        re.IGNORECASE,
                    ),
                ),
                page.locator(
                    'xpath=.//*['
                    'translate(normalize-space(.), '
                    '"ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
                    '"abcdefghijklmnopqrstuvwxyz")='
                    '"add a security key or passkey"]'
                ),
            ],
            stage="security_key_request",
            code="security_key_button_missing",
            message="未找到 Add a Security key or Passkey 按钮",
        )
        final_clickable = final_label.locator(
            "xpath=ancestor-or-self::*[self::button or @role=\"button\" "
            "or @tabindex][1]"
        )
        final_target = await self._require_security_locator(
            page,
            [final_clickable, final_label],
            stage="security_key_request",
            code="security_key_button_missing",
            message="未找到可点击的 Add a Security key or Passkey",
        )

        context = getattr(page, "context", None)
        if context is None:
            await self._safe_screenshot(page)
            raise SecurityNavigationError(
                "security_key_request",
                "security_page_context_missing",
                "无法读取当前浏览器页面上下文",
            )
        initial_url = sanitize_url(page.url)
        pages_before = list(context.pages)
        requested_at_utc = self.utc_now()
        if requested_at_utc.tzinfo is None:
            raise ValueError("安全设置导航时间必须包含时区")
        requested_at_utc = requested_at_utc.astimezone(timezone.utc)
        await self._click_security_locator(
            page,
            final_target,
            stage="security_key_request",
            code="security_key_request_click_failed",
            message="Add a Security key or Passkey 点击失败",
            no_wait_after=True,
        )

        setup_wait = await self._wait_for_security_setup_page(
            page,
            context,
            pages_before,
            initial_url,
        )
        final_page = setup_wait.page
        self._active_page = final_page
        await self._safe_screenshot(final_page)
        return SecurityNavigationResult(
            final_url=sanitize_url(final_page.url),
            delays_ms=(delay_ms,),
            requested_at_utc=requested_at_utc,
            opened_new_page=setup_wait.opened_new_page,
            navigation_mode="direct_settings",
            redirect_state=setup_wait.redirect_state,
            redirect_poll_count=setup_wait.redirect_poll_count,
            redirect_elapsed_ms=setup_wait.redirect_elapsed_ms,
        )

    async def _security_action_delay(
        self,
        minimum_seconds: float | None = None,
        maximum_seconds: float | None = None,
    ) -> int:
        minimum = (
            self.security_action_delay_min_seconds
            if minimum_seconds is None
            else minimum_seconds
        )
        maximum = (
            self.security_action_delay_max_seconds
            if maximum_seconds is None
            else maximum_seconds
        )
        generated = float(
            self.random_uniform(minimum, maximum)
        )
        seconds = min(
            maximum,
            max(minimum, generated),
        )
        await self.delay_sleep(seconds)
        return int(round(seconds * 1000))

    async def _require_security_locator(
        self,
        page: Any,
        candidates: list[Any],
        *,
        stage: str,
        code: str,
        message: str,
    ) -> Any:
        deadline = monotonic() + (self.security_action_timeout_ms / 1000)
        while True:
            await self._raise_security_challenge(page, stage)
            for candidate in candidates:
                try:
                    if await self._is_visible(candidate):
                        return candidate.first
                except Exception:
                    continue
            if monotonic() >= deadline:
                await self._safe_screenshot(page)
                raise SecurityNavigationError(stage, code, message)
            await asyncio.sleep(self.poll_interval_seconds)

    async def _click_security_locator(
        self,
        page: Any,
        locator: Any,
        *,
        stage: str,
        code: str,
        message: str,
        no_wait_after: bool = False,
    ) -> None:
        await self._raise_security_challenge(page, stage)
        try:
            await locator.click(
                timeout=self.security_action_timeout_ms,
                no_wait_after=no_wait_after,
            )
        except Exception:
            await self._safe_screenshot(page)
            raise SecurityNavigationError(stage, code, message) from None

    async def _raise_security_challenge(self, page: Any, stage: str) -> None:
        try:
            body = await page.locator("body").inner_text(timeout=1_000)
        except Exception:
            return
        if contains_challenge(body):
            await self._safe_screenshot(page)
            raise SecurityNavigationError(
                stage,
                "security_challenge_detected",
                "安全设置导航期间检测到人机验证或挑战页",
            )

    async def _wait_for_security_setup_page(
        self,
        page: Any,
        context: Any,
        pages_before: list[Any],
        initial_url: str,
    ) -> _SecuritySetupWaitResult:
        started = monotonic()
        deadline = monotonic() + (self.security_navigation_timeout_ms / 1000)
        poll_count = 0
        saw_trusted_intermediate = False
        last_observed_page = page

        def elapsed_ms() -> int:
            return max(0, int(round((monotonic() - started) * 1000)))

        async def reject_untrusted(candidate: Any, count: int) -> None:
            await self._safe_screenshot(candidate)
            raise SecurityNavigationError(
                "security_key_page",
                "security_key_page_untrusted",
                "Security Key 后续网页不是可信的 OpenAI Passkey 页面",
                redirect_state="untrusted_destination",
                redirect_poll_count=count,
                redirect_elapsed_ms=elapsed_ms(),
            )

        async def finish_candidate(
            candidate: Any,
            count: int,
            *,
            opened_new_page: bool,
        ) -> _SecuritySetupWaitResult | None:
            nonlocal saw_trusted_intermediate, last_observed_page
            try:
                await candidate.wait_for_load_state(
                    "domcontentloaded",
                    timeout=min(10_000, self.security_navigation_timeout_ms),
                )
            except Exception:
                await self._safe_screenshot(candidate)
                raise SecurityNavigationError(
                    "security_key_page",
                    "security_key_page_load_failed",
                    "Security Key 后续网页加载失败",
                    redirect_state=(
                        "final_after_trusted_intermediate"
                        if saw_trusted_intermediate
                        else "final_direct"
                    ),
                    redirect_poll_count=count,
                    redirect_elapsed_ms=elapsed_ms(),
                ) from None
            post_load_state = self._security_destination_state(
                str(getattr(candidate, "url", ""))
            )
            if post_load_state == "untrusted":
                await reject_untrusted(candidate, count)
            if post_load_state == "trusted_intermediate":
                saw_trusted_intermediate = True
                last_observed_page = candidate
                return None
            if post_load_state != "final":
                return None
            current_pages = list(context.pages)
            for other in current_pages:
                if other is candidate:
                    continue
                try:
                    await other.close()
                except Exception:
                    await self._safe_screenshot(candidate)
                    raise SecurityNavigationError(
                        "security_key_page",
                        "security_extra_page_cleanup_failed",
                        "Security Key 后续网页已打开，但旧标签页清理失败",
                        redirect_state=(
                            "final_after_trusted_intermediate"
                            if saw_trusted_intermediate
                            else "final_direct"
                        ),
                        redirect_poll_count=count,
                        redirect_elapsed_ms=elapsed_ms(),
                    ) from None
            await self._raise_security_challenge(candidate, "security_key_page")
            return _SecuritySetupWaitResult(
                page=candidate,
                opened_new_page=opened_new_page,
                redirect_state=(
                    "final_after_trusted_intermediate"
                    if saw_trusted_intermediate
                    else "final_direct"
                ),
                redirect_poll_count=count,
                redirect_elapsed_ms=elapsed_ms(),
            )

        while True:
            poll_count += 1
            current_pages = list(context.pages)
            new_pages = [
                candidate
                for candidate in current_pages
                if all(candidate is not existing for existing in pages_before)
            ]
            for candidate in reversed(new_pages):
                last_observed_page = candidate
                candidate_raw_url = str(getattr(candidate, "url", ""))
                candidate_state = self._security_destination_state(candidate_raw_url)
                if candidate_state == "blank":
                    continue
                if candidate_state == "untrusted":
                    await reject_untrusted(candidate, poll_count)
                if candidate_state == "trusted_intermediate":
                    saw_trusted_intermediate = True
                    await self._raise_security_challenge(candidate, "security_key_page")
                    continue
                loaded = await finish_candidate(
                    candidate,
                    poll_count,
                    opened_new_page=True,
                )
                if loaded is not None:
                    return loaded

            current_raw_url = str(getattr(page, "url", ""))
            current_url = sanitize_url(current_raw_url)
            if current_url != initial_url:
                current_state = self._security_destination_state(current_raw_url)
                if current_state == "untrusted":
                    await reject_untrusted(page, poll_count)
                if current_state == "trusted_intermediate":
                    saw_trusted_intermediate = True
                    last_observed_page = page
                    await self._raise_security_challenge(page, "security_key_page")
                elif current_state == "final":
                    loaded = await finish_candidate(
                        page,
                        poll_count,
                        opened_new_page=False,
                    )
                    if loaded is not None:
                        return loaded

            if monotonic() >= deadline:
                await self._safe_screenshot(last_observed_page)
                raise SecurityNavigationError(
                    "security_key_page",
                    "security_key_page_timeout",
                    "等待 Security Key 后续网页超时",
                    redirect_state=(
                        "trusted_intermediate_timeout"
                        if saw_trusted_intermediate
                        else "no_destination_timeout"
                    ),
                    redirect_poll_count=poll_count,
                    redirect_elapsed_ms=elapsed_ms(),
                )
            await asyncio.sleep(self.poll_interval_seconds)

    @staticmethod
    def _security_destination_state(value: str) -> str:
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").casefold()
            port = parsed.port
        except ValueError:
            return "untrusted"
        if value in {"", "about:blank"}:
            return "blank"
        if (
            parsed.scheme.casefold() != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or host not in PASSKEY_REDIRECT_HOSTS
        ):
            return "untrusted"
        if host == PASSKEY_ENROLL_HOST and parsed.path.rstrip("/") == PASSKEY_ENROLL_PATH:
            return "final"
        return "trusted_intermediate"

    async def _wait_for_profile_next_step(
        self,
        page: Any,
        initial_url: str,
        *,
        form_variant: str,
        locator_strategy: str,
        submit_variant: str,
    ) -> str:
        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        alert = page.locator('[role="alert"]:not(.sr-only)')
        auth_retry_count = 0
        submit_click_count = 1
        last_submit_click_at = monotonic()

        def profile_error(code: str, message: str) -> ProfileStepError:
            return ProfileStepError(
                code,
                message,
                form_variant=form_variant,
                locator_strategy=locator_strategy,
                submit_variant=submit_variant,
            )

        while True:
            remaining_ms = max(1, int((deadline - monotonic()) * 1000))
            try:
                body_text = await page.locator("body").inner_text(
                    timeout=min(1_000, remaining_ms)
                )
            except Exception:
                body_text = ""

            if await self._page_contains_challenge(page, body_text):
                await self._safe_screenshot(page)
                raise TargetChallengeError("检测到人机验证或挑战页，探测已停止")

            if auth_retry_count < 2 and await self._click_auth_retry_if_available(
                page, body_text
            ):
                auth_retry_count += 1
                last_submit_click_at = monotonic()
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            current_url = sanitize_url(page.url)
            parsed_current = urlsplit(current_url)
            current_host = (parsed_current.hostname or "").casefold()
            current_path = parsed_current.path.rstrip("/")
            on_profile_page = self._is_profile_path(current_path)
            rejection_text = body_text
            try:
                if await self._is_visible(alert):
                    rejection_text = (
                        f"{rejection_text}\n"
                        f"{await alert.first.inner_text(timeout=min(1_000, remaining_ms))}"
                    )
            except Exception:
                pass
            if on_profile_page and PROFILE_REJECTION_PATTERN.search(rejection_text):
                await self._safe_screenshot(page)
                raise profile_error(
                    "profile_rejected",
                    "网站未接受账号资料或账号创建请求",
                )

            try:
                current_controls = await self._profile_form_controls(page)
                name_visible = current_controls is not None
                second_visible = current_controls is not None
                on_profile_page = on_profile_page or current_controls is not None
            except Exception:
                current_controls = None
                name_visible = True
                second_visible = True
            if (
                current_host == "chatgpt.com"
                and current_url != initial_url
                and not on_profile_page
                and not name_visible
                and not second_visible
                and await self._is_confirmed_chatgpt_home(page)
            ):
                await self._safe_screenshot(page)
                return "account_created"

            if (
                on_profile_page
                and current_controls is not None
                and submit_click_count < 3
                and monotonic() - last_submit_click_at >= 3.5
            ):
                retry_button, _retry_variant = await self._profile_submit_control(
                    current_controls
                )
                try:
                    retry_ready = (
                        retry_button is not None
                        and await retry_button.is_visible()
                        and await retry_button.is_enabled()
                    )
                except Exception:
                    retry_ready = False
                if retry_ready:
                    submit_click_count += 1
                    await retry_button.click(timeout=self.email_action_timeout_ms)
                    last_submit_click_at = monotonic()
                    await asyncio.sleep(self.poll_interval_seconds)
                    continue

            if monotonic() >= deadline:
                await self._safe_screenshot(page)
                if current_url != initial_url or not name_visible or not second_visible:
                    raise profile_error(
                        "profile_next_step_unknown",
                        "账号资料提交后进入了无法识别的页面",
                    )
                raise profile_error(
                    "profile_submit_timeout",
                    "等待账号资料提交后的下一页超时",
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def _wait_for_verification_next_step(
        self,
        page: Any,
        initial_url: str,
        *,
        timeout_ms: int | None = None,
    ) -> _VerificationWaitResult:
        started = monotonic()
        effective_timeout_ms = timeout_ms or self.next_step_timeout_ms
        deadline = monotonic() + (effective_timeout_ms / 1000)
        verification_input = page.locator(
            'input[autocomplete="one-time-code"][name="code"][maxlength="6"]'
        )
        continue_button = page.locator(
            'button[type="submit"][name="intent"][value="validate"]'
        )
        alert = page.locator('[role="alert"]:not(.sr-only)')
        input_visible = True
        button_visible = True
        current_url = initial_url
        initial_path = urlsplit(initial_url).path.rstrip("/")
        auth_retry_count = 0

        def elapsed_ms() -> int:
            return max(0, int(round((monotonic() - started) * 1000)))

        while True:
            remaining_ms = max(1, int((deadline - monotonic()) * 1000))
            try:
                body_text = await page.locator("body").inner_text(
                    timeout=min(1_000, remaining_ms)
                )
            except Exception:
                body_text = ""

            if await self._page_contains_challenge(page, body_text):
                await self._safe_screenshot(page)
                raise TargetChallengeError(
                    "检测到人机验证或挑战页，探测已停止",
                    stage="verification",
                    wait_ms=elapsed_ms(),
                )

            if (
                initial_path.endswith("/email-verification")
                and AUTHENTICATOR_FACTOR_PATTERN.search(body_text)
            ):
                current_url = sanitize_url(page.url)
                try:
                    input_visible = await self._is_visible(verification_input)
                except Exception:
                    input_visible = True
                try:
                    button_visible = await self._is_visible(continue_button)
                except Exception:
                    button_visible = True
                await self._safe_screenshot(page)
                return _VerificationWaitResult(
                    next_step="totp",
                    post_click_state="totp",
                    wait_elapsed_ms=elapsed_ms(),
                    url_changed=current_url != initial_url,
                    input_visible_at_end=input_visible,
                    button_visible_at_end=button_visible,
                )

            if auth_retry_count < 2 and await self._click_auth_retry_if_available(
                page, body_text
            ):
                auth_retry_count += 1
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            rejection_text = body_text
            try:
                if await self._is_visible(alert):
                    rejection_text = (
                        f"{rejection_text}\n"
                        f"{await alert.first.inner_text(timeout=min(1_000, remaining_ms))}"
                    )
            except Exception:
                pass
            if VERIFICATION_REJECTION_PATTERN.search(rejection_text):
                await self._safe_screenshot(page)
                raise VerificationStepError(
                    "verification_code_rejected",
                    "网站未接受该验证码或验证码已过期",
                    post_click_state="rejected",
                    wait_elapsed_ms=elapsed_ms(),
                    url_changed=sanitize_url(page.url) != initial_url,
                    input_visible_at_end=True,
                    button_visible_at_end=True,
                )

            try:
                input_visible = await self._is_visible(verification_input)
            except Exception:
                input_visible = True
            try:
                button_visible = await self._is_visible(continue_button)
            except Exception:
                button_visible = True
            current_url = sanitize_url(page.url)
            current_path = urlsplit(current_url).path.rstrip("/")
            left_verification_url = not current_path.endswith("/email-verification")
            email_verified = EMAIL_VERIFIED_PATTERN.search(body_text) is not None
            profile_controls = None
            if not input_visible:
                try:
                    profile_controls = await self._profile_form_controls(page)
                except Exception:
                    profile_controls = None
            if (
                not input_visible
                and (
                    email_verified
                    or
                    profile_controls is not None
                    or (left_verification_url and current_url != initial_url)
                )
            ):
                await self._safe_screenshot(page)
                return _VerificationWaitResult(
                    next_step="transitioned",
                    post_click_state="transitioned",
                    wait_elapsed_ms=elapsed_ms(),
                    url_changed=current_url != initial_url,
                    input_visible_at_end=input_visible,
                    button_visible_at_end=button_visible,
                )

            if monotonic() >= deadline:
                await self._safe_screenshot(page)
                if current_url != initial_url or not input_visible:
                    raise VerificationStepError(
                        "verification_next_step_unknown",
                        "验证码提交后进入了无法识别的页面",
                        post_click_state="unknown",
                        wait_elapsed_ms=elapsed_ms(),
                        url_changed=current_url != initial_url,
                        input_visible_at_end=input_visible,
                        button_visible_at_end=button_visible,
                    )
                raise VerificationStepError(
                    "verification_form_unchanged_after_click",
                    f"验证码 Continue 点击后表单在 {effective_timeout_ms / 1000:g} 秒内未发生变化",
                    post_click_state="form_unchanged",
                    wait_elapsed_ms=elapsed_ms(),
                    url_changed=False,
                    input_visible_at_end=input_visible,
                    button_visible_at_end=button_visible,
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def _click_email_and_observe(
        self,
        page: Any,
        button: Any,
        initial_url: str,
        expected_email: str,
    ) -> tuple[str, str | None, Exception | None]:
        click_task = asyncio.create_task(
            button.click(timeout=self.email_action_timeout_ms)
        )
        observation_task = asyncio.create_task(
            self._wait_for_next_step(
                page,
                initial_url,
                expected_email=expected_email,
                allow_recovery=False,
            )
        )
        done, _pending = await asyncio.wait(
            {click_task, observation_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if observation_task in done:
            try:
                observed_step = observation_task.result()
            except EmailStepError as exc:
                if exc.code != "email_continue_timeout":
                    if not click_task.done():
                        click_task.cancel()
                    await asyncio.gather(click_task, return_exceptions=True)
                    raise
                observed_step = None
            except BaseException:
                if not click_task.done():
                    click_task.cancel()
                await asyncio.gather(click_task, return_exceptions=True)
                raise
            if observed_step is not None:
                click_error: Exception | None = None
                if click_task.done():
                    try:
                        click_error = click_task.exception()
                    except asyncio.CancelledError:
                        click_error = None
                if not click_task.done():
                    click_task.cancel()
                await asyncio.gather(click_task, return_exceptions=True)
                return "next_step", observed_step, click_error

        if not click_task.done():
            try:
                await click_task
            except Exception as exc:
                click_error: Exception | None = exc
            else:
                click_error = None
        else:
            try:
                click_error = click_task.exception()
            except asyncio.CancelledError:
                click_error = RuntimeError("click_cancelled")

        if not observation_task.done():
            observation_task.cancel()
        await asyncio.gather(observation_task, return_exceptions=True)
        if click_error is None:
            return "click_succeeded", None, None
        return "click_failed", None, click_error

    async def _wait_for_next_step(
        self,
        page: Any,
        initial_url: str,
        *,
        expected_email: str | None = None,
        allow_recovery: bool = True,
    ) -> str:
        deadline = monotonic() + (self.next_step_timeout_ms / 1000)
        email_input = self._email_input_locator(page)
        email_continue_button = self._email_continue_locator(page)
        password_input = page.locator('input[type="password"]')
        verification_input = page.locator(
            'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"]'
        )
        alert = page.locator('[role="alert"]:not(.sr-only)')
        empty_email_first_seen_at: float | None = None
        consecutive_empty_email_checks = 0
        stable_email_form_first_seen_at: float | None = None
        auth_retry_count = 0

        while True:
            remaining_ms = max(1, int((deadline - monotonic()) * 1000))
            try:
                body_text = await page.locator("body").inner_text(
                    timeout=min(1_000, remaining_ms)
                )
            except Exception:
                body_text = ""

            if contains_challenge(body_text):
                await self._safe_screenshot(page)
                raise TargetChallengeError("检测到人机验证或挑战页，探测已停止")

            if AUTHENTICATOR_FACTOR_PATTERN.search(body_text):
                await self._safe_screenshot(page)
                return "totp"

            if auth_retry_count < 2 and await self._click_auth_retry_if_available(
                page, body_text
            ):
                auth_retry_count += 1
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            email_value: str | None = None
            try:
                if await self._is_visible(password_input):
                    await self._safe_screenshot(page)
                    return "password"
                if await self._is_visible(verification_input) or VERIFICATION_PATTERN.search(
                    body_text
                ):
                    await self._safe_screenshot(page)
                    return "verification"

                email_visible = await self._is_visible(email_input)
                current_url = sanitize_url(page.url)
                current_parsed = urlsplit(current_url)
                verification_url = (
                    (current_parsed.hostname or "").casefold()
                    in {"auth.openai.com", "chatgpt.com"}
                    and current_parsed.path.rstrip("/").endswith(
                        "/email-verification"
                    )
                )
                if verification_url:
                    await self._safe_screenshot(page)
                    return "verification"
                if email_visible:
                    email_value = await email_input.first.input_value(
                        timeout=min(1_000, remaining_ms)
                    )
                if await self._is_visible(alert):
                    try:
                        alert_text = await alert.first.inner_text(
                            timeout=min(1_000, remaining_ms)
                        )
                    except Exception:
                        alert_text = ""
                    if EMAIL_REJECTION_PATTERN.search(alert_text.strip()):
                        await self._safe_screenshot(page)
                        raise EmailStepError("email_rejected", "网站未接受该邮箱")
            except EmailStepError:
                raise
            except Exception:
                email_visible = True
                current_url = sanitize_url(page.url)
                email_value = None

            current_parsed = urlsplit(current_url)
            still_on_login = (
                (current_parsed.hostname or "").casefold() == "chatgpt.com"
                and current_parsed.path.rstrip("/") == "/auth/login"
            )
            if (
                allow_recovery
                and still_on_login
                and email_visible
                and email_value == ""
            ):
                observed_at = monotonic()
                consecutive_empty_email_checks += 1
                if empty_email_first_seen_at is None:
                    empty_email_first_seen_at = observed_at
                elif (
                    consecutive_empty_email_checks >= 2
                    and observed_at - empty_email_first_seen_at
                    >= EMAIL_POST_SUBMIT_RESET_CONFIRMATION_SECONDS
                ):
                    return "email_form_reset"
            else:
                empty_email_first_seen_at = None
                consecutive_empty_email_checks = 0

            if (
                allow_recovery
                and expected_email is not None
                and still_on_login
                and email_visible
                and email_value == expected_email
            ):
                try:
                    button_visible = await self._is_visible(email_continue_button)
                    button_enabled = await email_continue_button.first.is_enabled()
                except Exception:
                    button_visible = False
                    button_enabled = False
                if (
                    button_visible
                    and button_enabled
                ):
                    observed_at = monotonic()
                    if stable_email_form_first_seen_at is None:
                        stable_email_form_first_seen_at = observed_at
                    elif (
                        observed_at - stable_email_form_first_seen_at
                        >= EMAIL_POST_SUBMIT_STABLE_FORM_CONFIRMATION_SECONDS
                    ):
                        return "email_form_stable"
                else:
                    stable_email_form_first_seen_at = None
            else:
                stable_email_form_first_seen_at = None

            if monotonic() >= deadline:
                await self._safe_screenshot(page)
                if current_url != initial_url or not email_visible:
                    raise EmailStepError(
                        "email_next_step_unknown",
                        "邮箱提交后进入了无法识别的页面",
                    )
                raise EmailStepError(
                    "email_continue_timeout",
                    "等待邮箱提交后的下一步页面超时",
                )
            await asyncio.sleep(self.poll_interval_seconds)

    @staticmethod
    async def _is_visible(locator: Any) -> bool:
        return await locator.count() > 0 and await locator.first.is_visible()

    async def _safe_screenshot_after_settle(self, page: Any) -> bool:
        for attempt in range(3):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=1_000)
            except Exception:
                pass
            if await self._safe_screenshot(page):
                return True
            if attempt < 2:
                await asyncio.sleep(0.5)
        return False

    async def _safe_screenshot(self, page: Any) -> bool:
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(self.screenshot_path), full_page=True)
        except Exception:
            return False
        return True
