from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.browser_automation import (
    CHATGPT_HOME_URL,
    CHATGPT_LOGIN_URL,
    CHATGPT_PLAN_URL,
    CHATGPT_PASSKEY_SETTINGS_URL,
    CHATGPT_SESSION_URL,
    IP_CHECK_URL,
    PROFILE_AGE_LABEL_PATTERN,
    PROFILE_BIRTHDAY_LABEL_PATTERN,
    PROFILE_NAME_LABEL_PATTERN,
    PROFILE_SUBMIT_PATTERN,
    AUTH_RETRY_ACTION_PATTERN,
    AUTH_RETRY_PAGE_PATTERN,
    VERIFICATION_PATTERN,
    VERIFICATION_REJECTION_PATTERN,
    AccessTokenExtractionError,
    CdpBrowserAutomation,
    EmailStepError,
    PasswordStepError,
    ProxyNavigationError,
    ProfileStepError,
    TargetChallengeError,
    SecurityNavigationError,
    VerificationStepError,
    contains_challenge,
    mask_ip,
    sanitize_url,
)
from backend.chatgpt_plan import PlanCheckError


def test_turkish_auth_and_profile_text_patterns() -> None:
    assert VERIFICATION_PATTERN.search("Gelen kutunu kontrol et")
    assert VERIFICATION_PATTERN.search("Doğrulama kodunu gir")
    assert VERIFICATION_REJECTION_PATTERN.search("Geçersiz doğrulama kodu")
    assert PROFILE_SUBMIT_PATTERN.fullmatch("Devam et")
    assert PROFILE_SUBMIT_PATTERN.fullmatch("Hesap oluşturmayı tamamla")
    assert PROFILE_NAME_LABEL_PATTERN.fullmatch("Ad soyad")
    assert PROFILE_AGE_LABEL_PATTERN.fullmatch("Yaş")
    assert PROFILE_BIRTHDAY_LABEL_PATTERN.fullmatch("Doğum tarihi")
    assert AUTH_RETRY_ACTION_PATTERN.fullmatch("Tekrar dene")
    assert AUTH_RETRY_ACTION_PATTERN.fullmatch("Önce dene")
    assert AUTH_RETRY_PAGE_PATTERN.search("Önce dene")


class FakeBodyLocator:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def inner_text(self, timeout: int) -> str:
        _ = timeout
        stage = "ip" if "ipify" in self.page.url else "login"
        error = self.page.body_errors.get(stage)
        if error is not None:
            raise error
        return self.page.body


class FakeElementLocator:
    def __init__(self, page: "FakePage", kind: str) -> None:
        self.page = page
        self.kind = kind

    @property
    def first(self) -> "FakeElementLocator":
        return self

    def nth(self, _index: int) -> "FakeElementLocator":
        return self

    def locator(self, selector: str) -> "FakeElementLocator":
        if self.kind != "profile_form":
            return FakeElementLocator(self.page, "missing")
        return self.page.profile_form_locator(selector)

    def get_by_label(self, name: object) -> "FakeElementLocator":
        if self.kind != "profile_form":
            return FakeElementLocator(self.page, "missing")
        return self.page.profile_label_locator(name)

    async def count(self) -> int:
        return int(self._available())

    async def is_visible(self) -> bool:
        return self._available()

    async def fill(self, value: str, *, timeout: int) -> None:
        self.page.fill_timeout = timeout
        if self.kind == "verification":
            error = self.page.verification_fill_error
        elif self.kind == "profile_name":
            error = self.page.profile_name_fill_error
        elif self.kind in {"profile_age", "profile_birthday"}:
            error = self.page.profile_age_fill_error
        elif self.kind == "password":
            error = self.page.password_fill_error
        else:
            error = self.page.fill_error
        if error is not None:
            raise error
        if self.kind == "verification":
            self.page.filled_verification_code = value
            self.page.events.append(("verification_fill", value))
        elif self.kind == "profile_name":
            self.page.filled_profile_name = value
            self.page.events.append(("profile_name_fill", value))
        elif self.kind in {"profile_age", "profile_birthday"}:
            self.page.filled_profile_second = value
            self.page.events.append((f"{self.kind}_fill", value))
        elif self.kind == "password":
            self.page.filled_password = value
            self.page.events.append(("password_fill", "<redacted>"))
        else:
            self.page.filled_email = value
            self.page.events.append(("fill", value))

    async def input_value(self, *, timeout: int) -> str:
        self.page.input_value_timeout = timeout
        if self.kind == "verification":
            self.page.events.append(("verification_input_value", None))
            if self.page.verification_input_value_override is not None:
                return self.page.verification_input_value_override
            return self.page.filled_verification_code
        if self.kind == "profile_name":
            self.page.events.append(("profile_name_input_value", None))
            if self.page.profile_name_value_override is not None:
                return self.page.profile_name_value_override
            return self.page.filled_profile_name
        if self.kind in {"profile_age", "profile_birthday"}:
            self.page.events.append((f"{self.kind}_input_value", None))
            if self.page.profile_age_value_override is not None:
                return self.page.profile_age_value_override
            return self.page.filled_profile_second
        if self.kind == "profile_birthday_value":
            return self.page.profile_hidden_birthday
        if self.kind == "password":
            return self.page.filled_password
        self.page.events.append(("input_value", None))
        if self.page.input_value_override is not None:
            return self.page.input_value_override
        if self.page.post_submit_transient_empty_pending:
            self.page.post_submit_transient_empty_pending = False
            self.page.email_visible = False
            self.page.password_visible = True
            self.page.body = "Create your password"
            return ""
        return self.page.filled_email

    async def click(self, *, timeout: int, trial: bool = False) -> None:
        self.page.click_timeout = timeout
        if trial:
            self.page.continue_trial_count += 1
            if self.page.trial_click_error is not None:
                raise self.page.trial_click_error
            if not self.page.continue_enabled:
                if self.page.enable_during_trial:
                    self.page.continue_enabled = True
                else:
                    raise RuntimeError("button not actionable")
            return
        if self.kind.startswith("profile_segment_"):
            self.page.events.append((f"{self.kind}_click", None))
            return
        if self.kind == "verification_button":
            if self.page.verification_click_errors:
                error = self.page.verification_click_errors.pop(0)
            else:
                error = self.page.verification_click_error
        elif self.kind == "password_button":
            error = self.page.password_click_error
        elif self.kind == "profile_finish":
            error = self.page.profile_finish_click_error
        else:
            self.page.continue_click_invocation_count += 1
            if not self.page.continue_enabled:
                if self.page.enable_during_click:
                    self.page.continue_enabled = True
                else:
                    raise RuntimeError("button not actionable")
            if self.page.click_errors:
                error = self.page.click_errors.pop(0)
            else:
                error = self.page.click_error
        if error is not None:
            if (
                self.kind == "verification_button"
                and self.page.verification_click_error_outcome
            ):
                self.page.apply_verification_next_step()
            if self.kind == "button":
                self.page.apply_click_error_outcome()
            if self.kind == "profile_finish" and self.page.profile_finish_click_error_outcome:
                self.page.apply_profile_next_step()
            raise error
        self.page.clicked = True
        if self.kind == "verification_button":
            self.page.verification_clicked = True
            self.page.events.append(("verification_click", None))
            self.page.apply_verification_next_step()
        elif self.kind == "password_button":
            self.page.events.append(("password_click", None))
            self.page.apply_password_next_step()
        elif self.kind == "profile_finish":
            self.page.profile_clicked = True
            self.page.events.append(("profile_finish_click", None))
            self.page.apply_profile_next_step()
        else:
            self.page.continue_click_count += 1
            self.page.events.append(("click", None))
            self.page.apply_next_step()

    async def press(self, key: str, *, timeout: int) -> None:
        _ = timeout
        if not self.kind.startswith("profile_segment_"):
            return
        segment = self.kind.removeprefix("profile_segment_")
        if key == "Control+A":
            self.page.profile_segment_values[segment] = ""
        elif key == "Tab":
            values = self.page.profile_segment_values
            if all(values.get(part, "").isdigit() for part in ("year", "month", "day")):
                self.page.profile_hidden_birthday = (
                    f"{int(values['year']):04d}-{int(values['month']):02d}-"
                    f"{int(values['day']):02d}"
                )

    async def press_sequentially(
        self,
        value: str,
        *,
        delay: int,
        timeout: int,
    ) -> None:
        _ = (delay, timeout)
        if self.kind.startswith("profile_segment_"):
            segment = self.kind.removeprefix("profile_segment_")
            self.page.profile_segment_values[segment] = value
            self.page.events.append((f"profile_segment_{segment}_fill", value))

    async def get_attribute(self, name: str) -> str | None:
        if name == "aria-valuenow" and self.kind.startswith("profile_segment_"):
            segment = self.kind.removeprefix("profile_segment_")
            return self.page.profile_segment_values.get(segment, "")
        return None

    async def inner_text(self, *, timeout: int) -> str:
        _ = timeout
        if self.kind == "alert":
            return self.page.alert_text
        if self.kind == "profile_finish":
            return self.page.profile_submit_text
        if self.kind in {"button", "password_button", "verification_button"}:
            return self.page.continue_text
        return ""

    async def is_enabled(self, **_kwargs: object) -> bool:
        if self.kind == "profile_finish":
            return self.page.profile_finish_enabled
        if self.kind == "button":
            return self.page.continue_enabled
        if self.kind == "email":
            return self.page.email_enabled
        return True

    async def is_editable(self, **_kwargs: object) -> bool:
        return self.kind == "email" and self.page.email_editable

    def _available(self) -> bool:
        if self.kind == "challenge":
            remaining = self.page.login_challenge_checks_before_release
            if remaining is None:
                return False
            self.page.login_challenge_checks += 1
            if remaining < 0:
                return True
            if remaining == 0:
                self.page.login_challenge_checks_before_release = None
                self.page.email_visible = True
                self.page.continue_visible = True
                self.page.body = "Log in or sign up Email address Continue"
                return False
            self.page.login_challenge_checks_before_release = remaining - 1
            return True
        if self.kind == "profile_home_button":
            self.page.profile_home_checks += 1
            visible_after = self.page.profile_home_visible_after
            return visible_after is not None and self.page.profile_home_checks >= visible_after
        return {
            "email": self.page.email_visible,
            "button": self.page.continue_visible,
            "password": self.page.password_visible,
            "password_button": self.page.password_visible,
            "verification": self.page.verification_visible,
            "verification_button": self.page.verification_continue_visible,
            "profile_form": self.page.profile_name_visible
            or self.page.profile_age_visible
            or self.page.profile_birthday_visible
            or self.page.profile_segmented_visible,
            "profile_name": self.page.profile_name_visible,
            "profile_age": self.page.profile_age_visible,
            "profile_birthday": self.page.profile_birthday_visible,
            "profile_birthday_value": self.page.profile_segmented_visible,
            "profile_segment_year": self.page.profile_segmented_visible,
            "profile_segment_month": self.page.profile_segmented_visible,
            "profile_segment_day": self.page.profile_segmented_visible,
            "profile_finish": self.page.profile_finish_visible,
            "alert": self.page.alert_visible,
        }.get(self.kind, False)

class FakePage:
    def __init__(
        self,
        *,
        email_visible: bool = True,
        continue_visible: bool = True,
        continue_enabled: bool = True,
        email_enabled: bool = True,
        email_editable: bool = True,
        ready_state: str = "complete",
        enable_during_trial: bool = False,
        enable_during_click: bool = False,
        goto_errors: dict[str, Exception] | None = None,
        body_errors: dict[str, Exception] | None = None,
        ip_body: str = '{"ip":"203.0.113.42"}',
        next_step: str = "password",
        fill_error: Exception | None = None,
        click_error: Exception | None = None,
        click_errors: list[Exception | None] | None = None,
        click_error_outcome: str = "stable",
        trial_click_error: Exception | None = None,
        input_value_override: str | None = None,
        alert_text: str = "",
        alert_sr_only: bool = False,
        verification_submit_outcome: str = "transitioned",
        password_submit_outcome: str = "verification",
        password_fill_error: Exception | None = None,
        password_click_error: Exception | None = None,
        verification_fill_error: Exception | None = None,
        verification_click_error: Exception | None = None,
        verification_click_errors: list[Exception | None] | None = None,
        verification_click_error_outcome: bool = False,
        verification_input_value_override: str | None = None,
        profile_submit_outcome: str = "transitioned",
        profile_name_fill_error: Exception | None = None,
        profile_age_fill_error: Exception | None = None,
        profile_finish_click_error: Exception | None = None,
        profile_finish_click_error_outcome: bool = False,
        profile_name_value_override: str | None = None,
        profile_age_value_override: str | None = None,
        profile_form_variant: str = "numeric_age",
        profile_locator_strategy: str = "strict_attributes",
        profile_submit_text: str = "Finish creating account",
        profile_home_visible_after: int | None = None,
        profile_home_div_only: bool = False,
        profile_home_localized_only: bool = False,
        screenshot_failures: int = 0,
        login_challenge_checks_before_release: int | None = None,
        continue_text: str = "Continue",
    ) -> None:
        self.url = "about:blank"
        self.body = ""
        self.email_visible = email_visible
        self.continue_visible = continue_visible
        self.continue_enabled = continue_enabled
        self.email_enabled = email_enabled
        self.email_editable = email_editable
        self.ready_state = ready_state
        self.enable_during_trial = enable_during_trial
        self.enable_during_click = enable_during_click
        self.goto_errors = goto_errors or {}
        self.body_errors = body_errors or {}
        self.ip_body = ip_body
        self.next_step = next_step
        self.fill_error = fill_error
        self.click_error = click_error
        self.click_errors = list(click_errors or [])
        self.click_error_outcome = click_error_outcome
        self.trial_click_error = trial_click_error
        self.input_value_override = input_value_override
        self.alert_text = alert_text
        self.alert_sr_only = alert_sr_only
        self.verification_submit_outcome = verification_submit_outcome
        self.password_submit_outcome = password_submit_outcome
        self.password_fill_error = password_fill_error
        self.password_click_error = password_click_error
        self.verification_fill_error = verification_fill_error
        self.verification_click_error = verification_click_error
        self.verification_click_errors = list(verification_click_errors or [])
        self.verification_click_error_outcome = verification_click_error_outcome
        self.verification_input_value_override = verification_input_value_override
        self.profile_submit_outcome = profile_submit_outcome
        self.profile_name_fill_error = profile_name_fill_error
        self.profile_age_fill_error = profile_age_fill_error
        self.profile_finish_click_error = profile_finish_click_error
        self.profile_finish_click_error_outcome = profile_finish_click_error_outcome
        self.profile_name_value_override = profile_name_value_override
        self.profile_age_value_override = profile_age_value_override
        self.profile_form_variant = profile_form_variant
        self.profile_locator_strategy = profile_locator_strategy
        self.profile_submit_text = profile_submit_text
        self.profile_home_visible_after = profile_home_visible_after
        self.profile_home_div_only = profile_home_div_only
        self.profile_home_localized_only = profile_home_localized_only
        self.screenshot_failures = screenshot_failures
        self.login_challenge_checks_before_release = (
            login_challenge_checks_before_release
        )
        self.continue_text = continue_text
        self.login_challenge_checks = 0
        self.screenshot_calls = 0
        self.profile_home_checks = 0
        self.password_visible = False
        self.verification_visible = False
        self.verification_continue_visible = True
        self.alert_visible = False
        self.profile_name_visible = False
        self.profile_age_visible = False
        self.profile_birthday_visible = False
        self.profile_segmented_visible = False
        self.profile_segment_values = {
            "year": "2026",
            "month": "08",
            "day": "13",
        }
        self.profile_hidden_birthday = "2026-08-13"
        self.profile_finish_visible = True
        self.profile_finish_enabled = True
        self.filled_email = ""
        self.filled_verification_code = ""
        self.filled_password = ""
        self.filled_profile_name = ""
        self.filled_profile_second = ""
        self.fill_timeout: int | None = None
        self.input_value_timeout: int | None = None
        self.click_timeout: int | None = None
        self.events: list[tuple[str, object]] = []
        self.clicked = False
        self.continue_click_count = 0
        self.continue_click_invocation_count = 0
        self.continue_trial_count = 0
        self.post_submit_transient_empty_pending = False
        self.verification_clicked = False
        self.profile_clicked = False
        self.closed = False
        self.goto_calls: list[tuple[str, object, object]] = []
        self.locator_calls: list[str] = []
        self.get_by_test_id_calls: list[str] = []
        self.get_by_role_calls: list[tuple[str, dict[str, object]]] = []
        self.reload_calls = 0

    @property
    def filled_profile_age(self) -> str:
        return self.filled_profile_second

    @filled_profile_age.setter
    def filled_profile_age(self, value: str) -> None:
        self.filled_profile_second = value

    async def goto(self, url: str, **kwargs: object) -> None:
        self.goto_calls.append((url, kwargs.get("wait_until"), kwargs.get("timeout")))
        self.url = url
        stage = "ip" if "ipify" in url else "login"
        error = self.goto_errors.get(stage)
        if error is not None:
            raise error
        if stage == "ip":
            self.body = self.ip_body
        else:
            if self.login_challenge_checks_before_release is not None:
                self.body = ""
                self.email_visible = False
                self.continue_visible = False
            else:
                self.body = "Log in or sign up Email address Continue"
            self.url = "https://chatgpt.com/auth/login?openaicom_referred=true"

    async def reload(self, **_kwargs: object) -> None:
        self.reload_calls += 1
        self.events.append(("verification_reload", None))
        self.filled_verification_code = ""
        self.alert_visible = False
        self.alert_text = ""
        self.verification_visible = True
        self.verification_continue_visible = True
        self.body = "Enter the verification code"
        if self.verification_submit_outcome == "timeout_then_transitioned":
            self.verification_submit_outcome = "transitioned"

    def locator(self, selector: str) -> FakeBodyLocator | FakeElementLocator:
        self.locator_calls.append(selector)
        if selector == "body":
            return FakeBodyLocator(self)
        if "challenges.cloudflare.com" in selector or "cf-turnstile" in selector:
            return FakeElementLocator(self, "challenge")
        if (
            'data-testid="accounts-profile-button"' in selector
            or "open profile menu" in selector
        ):
            if self.profile_home_localized_only and "open profile menu" in selector:
                return FakeElementLocator(self, "missing")
            if self.profile_home_div_only and selector.startswith("button"):
                return FakeElementLocator(self, "missing")
            return FakeElementLocator(self, "profile_home_button")
        if selector == 'input[type="password"]':
            return FakeElementLocator(self, "password")
        if 'and .//input[@type="password"]' in selector:
            return FakeElementLocator(self, "password_button")
        if "one-time-code" in selector:
            return FakeElementLocator(self, "verification")
        if selector.startswith('xpath=//form[') and './/input' not in selector:
            return FakeElementLocator(self, "profile_form")
        if 'name="name"' in selector and 'autocomplete="name"' in selector:
            return FakeElementLocator(self, "profile_name")
        if 'name="age"' in selector:
            return FakeElementLocator(self, "profile_age")
        if 'name="intent"' in selector and 'value="validate"' in selector:
            return FakeElementLocator(self, "verification_button")
        if (
            selector == 'button[type="submit"]'
            or (
                selector.startswith('xpath=//form[')
                and '@type="submit"' in selector
                and 'self::button or self::input' in selector
            )
        ):
            if self.profile_name_visible or self.profile_age_visible:
                return FakeElementLocator(self, "profile_finish")
            return FakeElementLocator(self, "button")
        if selector.startswith('[role="alert"]'):
            if ":not(.sr-only)" in selector and self.alert_sr_only:
                return FakeElementLocator(self, "missing")
            return FakeElementLocator(self, "alert")
        if "email" in selector:
            return FakeElementLocator(self, "email")
        return FakeElementLocator(self, "missing")

    def profile_form_locator(self, selector: str) -> FakeElementLocator:
        strict = self.profile_locator_strategy == "strict_attributes"
        if 'name="name"' in selector and strict:
            return FakeElementLocator(self, "profile_name")
        if 'name="age"' in selector and strict and self.profile_form_variant == "numeric_age":
            return FakeElementLocator(self, "profile_age")
        if (
            any(token in selector for token in ('autocomplete="bday"', 'name="birthday"', 'name="birthdate"'))
            and strict
            and self.profile_form_variant == "birthday"
        ):
            return FakeElementLocator(self, "profile_birthday")
        if self.profile_form_variant == "segmented_birthday":
            for segment in ("year", "month", "day"):
                if f'data-type="{segment}"' in selector:
                    return FakeElementLocator(self, f"profile_segment_{segment}")
            if 'input[name="birthday"]' in selector:
                return FakeElementLocator(self, "profile_birthday_value")
        if selector == 'button[type="submit"], button:not([type])':
            return FakeElementLocator(self, "profile_finish")
        return FakeElementLocator(self, "missing")

    def profile_label_locator(self, name: object) -> FakeElementLocator:
        if self.profile_locator_strategy != "semantic_labels":
            return FakeElementLocator(self, "missing")
        pattern = str(getattr(name, "pattern", name)).casefold()
        if "full" in pattern and "name" in pattern:
            return FakeElementLocator(self, "profile_name")
        if "birthday" in pattern or "birth" in pattern:
            return FakeElementLocator(
                self,
                "profile_birthday" if self.profile_form_variant == "birthday" else "missing",
            )
        if "age" in pattern and self.profile_form_variant == "numeric_age":
            return FakeElementLocator(self, "profile_age")
        return FakeElementLocator(self, "missing")

    def get_by_test_id(self, test_id: str) -> FakeElementLocator:
        self.get_by_test_id_calls.append(test_id)
        if test_id == "accounts-profile-button":
            return FakeElementLocator(self, "profile_home_button")
        return FakeElementLocator(self, "missing")

    def get_by_role(self, role: str, **_kwargs: object) -> FakeElementLocator:
        self.get_by_role_calls.append((role, dict(_kwargs)))
        return FakeElementLocator(self, "missing")

    def apply_next_step(self) -> None:
        if self.next_step == "password":
            self.email_visible = False
            self.password_visible = True
            self.body = "Create your password"
        elif self.next_step == "verification":
            self.email_visible = False
            self.verification_visible = True
            self.body = "Check your email Verification code"
        elif self.next_step == "verification_url_loading":
            self.email_visible = False
            self.url = "https://auth.openai.com/email-verification?state=PRIVATE_VALUE"
            self.body = "Loading"
        elif self.next_step == "transitioned":
            self.email_visible = False
            self.url = "https://chatgpt.com/auth/next?state=PRIVATE_VALUE"
            self.body = "Next step"
        elif self.next_step == "rejected":
            self.alert_visible = True
            self.alert_text = "Email address is not accepted"
            self.body = "Email is not accepted"
        elif self.next_step == "challenge":
            self.body = "Verify you are human"
        elif self.next_step == "unknown":
            self.url = "https://chatgpt.com/auth/unexpected?state=PRIVATE_VALUE"
            self.body = "Unknown page"
        elif self.next_step == "loading":
            self.alert_visible = True
            self.alert_text = "Loading"
            self.body = "Loading"
        elif self.next_step == "blank_alert":
            self.alert_visible = True
            self.alert_text = ""
            self.body = "Loading"
        elif self.next_step == "sr_only_rejected":
            self.alert_visible = True
            self.alert_sr_only = True
            self.alert_text = "Email address is not accepted"
            self.body = "Loading"
        elif self.next_step == "post_submit_reset_then_verification":
            if self.continue_click_count == 1:
                self.filled_email = ""
                self.body = "Log in or sign up Email address Continue"
            else:
                self.email_visible = False
                self.verification_visible = True
                self.body = "Check your email Verification code"
        elif self.next_step == "post_submit_reset_twice":
            self.filled_email = ""
            self.body = "Log in or sign up Email address Continue"
        elif self.next_step == "post_submit_transient_empty_then_password":
            self.filled_email = ""
            self.body = "Log in or sign up Email address Continue"
            self.post_submit_transient_empty_pending = True
        elif self.next_step == "stalled_then_password":
            if self.continue_click_count >= 2:
                self.email_visible = False
                self.password_visible = True
                self.body = "Create your password"

    def apply_click_error_outcome(self) -> None:
        if self.click_error_outcome == "verification":
            self.email_visible = False
            self.verification_visible = True
            self.body = "Check your email Verification code"
        elif self.click_error_outcome == "password":
            self.email_visible = False
            self.password_visible = True
            self.body = "Create your password"
        elif self.click_error_outcome == "reset":
            self.filled_email = ""
            self.body = "Log in or sign up Email address Continue"

    def apply_verification_next_step(self) -> None:
        if self.verification_submit_outcome == "transitioned":
            self.verification_visible = False
            self.url = "https://auth.openai.com/about-you?state=PRIVATE_VALUE"
            self.body = "Tell us about you"
            self.profile_name_visible = True
            self.profile_age_visible = True
        elif self.verification_submit_outcome == "rejected":
            self.alert_visible = True
            self.alert_text = "Invalid verification code"
            self.body = "Invalid verification code"
        elif self.verification_submit_outcome == "challenge":
            self.body = "Verify you are human"
        elif self.verification_submit_outcome == "unknown":
            self.verification_visible = False
            self.body = "Unknown next step"
        elif self.verification_submit_outcome == "changed_with_input":
            self.url = "https://auth.openai.com/unexpected?state=PRIVATE_VALUE"
            self.body = "Loading"
        elif self.verification_submit_outcome == "combined_profile":
            self.verification_visible = False
            self.body = "プロフィールを入力してください"
            self.profile_name_visible = True
            self.profile_age_visible = True
        elif self.verification_submit_outcome == "verified_same_url":
            self.verification_visible = False
            self.verification_continue_visible = False
            self.body = "メールが確認されました。メールアドレスはすでに確認済みです。"

    def apply_password_next_step(self) -> None:
        if self.password_submit_outcome == "verification":
            self.password_visible = False
            self.verification_visible = True
            self.url = "https://auth.openai.com/email-verification?state=PRIVATE_VALUE"
            self.body = "Enter the verification code"
        elif self.password_submit_outcome == "rejected":
            self.alert_visible = True
            self.alert_text = "Password is invalid"
            self.body = self.alert_text
        elif self.password_submit_outcome == "challenge":
            self.body = "Verify you are human"

    def apply_profile_next_step(self) -> None:
        if self.profile_submit_outcome == "transitioned":
            self.profile_name_visible = False
            self.profile_age_visible = False
            self.profile_birthday_visible = False
            self.url = "https://chatgpt.com/?state=PRIVATE_VALUE"
            self.body = "Welcome to ChatGPT"
            if self.profile_home_visible_after is None:
                self.profile_home_visible_after = 1
        elif self.profile_submit_outcome == "rejected":
            self.alert_visible = True
            self.alert_text = "Invalid age"
            self.body = "Invalid age"
        elif self.profile_submit_outcome == "challenge":
            self.body = "Verify you are human"
        elif self.profile_submit_outcome == "unknown":
            self.profile_name_visible = False
            self.profile_age_visible = False
            self.profile_birthday_visible = False
            self.body = "Unknown next step"
        elif self.profile_submit_outcome == "changed_with_fields":
            self.url = "https://chatgpt.com/unexpected?state=PRIVATE_VALUE"
            self.body = "Loading"

    async def close(self) -> None:
        self.closed = True

    async def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def evaluate(self, expression: str) -> str:
        assert expression == "document.readyState"
        return self.ready_state

    async def screenshot(self, path: str, **_kwargs: object) -> None:
        self.screenshot_calls += 1
        if self.screenshot_failures > 0:
            self.screenshot_failures -= 1
            raise RuntimeError("PRIVATE_SCREENSHOT_TRANSITION_ERROR")
        Path(path).write_bytes(b"fake-png")


class MultiProfileFormCollection:
    def __init__(self, page: "MultiProfileFormPage") -> None:
        self.page = page

    async def count(self) -> int:
        return 3

    def nth(self, index: int) -> "MultiProfileFormScope":
        return MultiProfileFormScope(self.page, index)


class MultiProfileFormScope:
    def __init__(self, page: "MultiProfileFormPage", index: int) -> None:
        self.page = page
        self.index = index

    async def is_visible(self) -> bool:
        return self.index != 0

    def locator(self, selector: str) -> FakeElementLocator:
        if 'name="name"' in selector:
            return FakeElementLocator(self.page, "profile_name")
        if 'name="age"' in selector:
            return FakeElementLocator(
                self.page,
                "profile_age" if self.index in {0, 2} else "missing",
            )
        if selector == 'button[type="submit"], button:not([type])':
            self.page.profile_submit_form_indices.append(self.index)
            return FakeElementLocator(self.page, "profile_finish")
        return FakeElementLocator(self.page, "missing")

    def get_by_label(self, _name: object) -> FakeElementLocator:
        return FakeElementLocator(self.page, "missing")


class MultiProfileFormPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.profile_submit_form_indices: list[int] = []

    def locator(self, selector: str):
        if selector.startswith('xpath=//form[') and './/input' not in selector:
            return MultiProfileFormCollection(self)
        return super().locator(selector)


class SessionFakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self._payload = payload
        self.status = status
        self.headers = {"content-type": content_type}

    async def body(self) -> bytes:
        return self._payload


class SessionFakePage(FakePage):
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        session_final_url: str = CHATGPT_SESSION_URL,
        session_error: Exception | None = None,
        restore_error: Exception | None = None,
    ) -> None:
        super().__init__(profile_home_visible_after=1)
        self.url = CHATGPT_HOME_URL
        self.body = "Welcome to ChatGPT"
        self.response = SessionFakeResponse(
            payload,
            status=status,
            content_type=content_type,
        )
        self.session_final_url = session_final_url
        self.session_error = session_error
        self.restore_error = restore_error
        self.screenshot_urls: list[str] = []

    async def goto(self, url: str, **kwargs: object) -> SessionFakeResponse | None:
        self.goto_calls.append((url, kwargs.get("wait_until"), kwargs.get("timeout")))
        if url == CHATGPT_SESSION_URL:
            if self.session_error is not None:
                raise self.session_error
            self.url = self.session_final_url
            self.body = "PRIVATE_SESSION_BODY"
            return self.response
        if url == CHATGPT_HOME_URL:
            if self.restore_error is not None:
                raise self.restore_error
            self.url = CHATGPT_HOME_URL
            self.body = "Welcome to ChatGPT"
            return None
        raise AssertionError(f"unexpected navigation: {url}")

    async def screenshot(self, path: str, **kwargs: object) -> None:
        self.screenshot_urls.append(self.url)
        await super().screenshot(path, **kwargs)


class PlanFakeRequest:
    def __init__(self) -> None:
        self.headers = {"accept": "text/html"}


class PlanFakeRoute:
    def __init__(self) -> None:
        self.request = PlanFakeRequest()
        self.continued_headers: dict[str, str] | None = None

    async def continue_(self, *, headers: dict[str, str]) -> None:
        self.continued_headers = headers


class PlanFakePage(SessionFakePage):
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        super().__init__(payload)
        self.response = SessionFakeResponse(payload, status=status)
        self.route_handler = None
        self.route_pattern: str | None = None
        self.unroute_calls: list[str] = []
        self.plan_route = PlanFakeRoute()

    async def route(self, pattern: str, handler: object) -> None:
        self.route_pattern = pattern
        self.route_handler = handler

    async def unroute(self, pattern: str, handler: object) -> None:
        assert handler is self.route_handler
        self.unroute_calls.append(pattern)

    async def goto(self, url: str, **kwargs: object) -> SessionFakeResponse | None:
        if url == CHATGPT_PLAN_URL:
            self.goto_calls.append((url, kwargs.get("wait_until"), kwargs.get("timeout")))
            assert self.route_handler is not None
            await self.route_handler(self.plan_route)
            self.url = CHATGPT_PLAN_URL
            return self.response
        return await super().goto(url, **kwargs)


class CheckoutTypeFakePage(SessionFakePage):
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        super().__init__(b"{}")
        self.checkout_payload = payload
        self.checkout_status = status
        self.checkout_arguments: dict[str, str] | None = None

    async def evaluate(
        self, expression: str, arguments: dict[str, str] | None = None
    ) -> object:
        if expression == "document.readyState":
            return self.ready_state
        assert "fetch(url" in expression
        assert arguments is not None
        assert arguments["url"].endswith("/backend-api/payments/checkout")
        self.checkout_arguments = arguments
        return {
            "status": self.checkout_status,
            "body": json.dumps(self.checkout_payload),
        }


class FakeContext:
    def __init__(self, pages: FakePage | list[FakePage]) -> None:
        self.pages = pages if isinstance(pages, list) else [pages]
        for page in self.pages:
            page.context = self

    async def new_page(self) -> FakePage:
        page = FakePage()
        page.context = self
        self.pages.append(page)
        return page

    def add_page(self, page: FakePage) -> None:
        page.context = self
        self.pages.append(page)


class FakeBrowser:
    def __init__(self, pages: FakePage | list[FakePage]) -> None:
        self.contexts = [FakeContext(pages)]


class FakeChromium:
    def __init__(self, pages: FakePage | list[FakePage]) -> None:
        self.pages = pages
        self.connections: list[tuple[str, int]] = []

    async def connect_over_cdp(self, ws: str, timeout: int) -> FakeBrowser:
        self.connections.append((ws, timeout))
        return FakeBrowser(self.pages)


class FakePlaywright:
    def __init__(self, pages: FakePage | list[FakePage]) -> None:
        self.chromium = FakeChromium(pages)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


class SecurityFakeLocator:
    def __init__(
        self,
        page: "SecurityFakePage",
        kind: str,
        *,
        scope: str = "page",
    ) -> None:
        self.page = page
        self.kind = kind
        self.scope = scope

    @property
    def first(self) -> "SecurityFakeLocator":
        return self

    async def count(self) -> int:
        return int(self.page.security_available(self.kind, self.scope))

    async def is_visible(self) -> bool:
        return self.page.security_available(self.kind, self.scope)

    async def click(
        self,
        *,
        timeout: int,
        no_wait_after: bool = False,
        force: bool = False,
    ) -> None:
        self.page.security_click_timeouts.append(timeout)
        self.page.security_click_no_wait_after.append(no_wait_after)
        self.page.security_click_force.append(force)
        if self.page.click_error_kind == self.kind:
            raise RuntimeError("PRIVATE_SECURITY_CLICK_ERROR")
        self.page.security_clicks.append(self.kind)
        self.page.events.append((f"security_{self.kind}_click", None))
        self.page.apply_security_step(self.kind)

    async def press(self, key: str, **_kwargs: object) -> None:
        self.page.activation_fallbacks.append(f"press:{key}")

    async def dispatch_event(self, event: str, **_kwargs: object) -> None:
        self.page.activation_fallbacks.append(f"dispatch:{event}")

    async def evaluate(self, _expression: str, **_kwargs: object) -> None:
        self.page.activation_fallbacks.append("evaluate")

    def get_by_text(self, text: str, *, exact: bool) -> "SecurityFakeLocator":
        assert exact is True
        return self.page.security_text_locator(text, self.kind)

    def get_by_role(
        self,
        role: str,
        **kwargs: object,
    ) -> "SecurityFakeLocator":
        return self.page.security_role_locator(role, kwargs, self.kind)

    def locator(self, selector: str) -> "SecurityFakeLocator":
        self.page.security_nested_locator_calls.append((self.kind, selector))
        if (
            selector
            == 'xpath=ancestor-or-self::*[self::button or @role="button" '
            'or @tabindex][1]'
            and self.kind in {"security_key_label", "security_key_button"}
        ):
            return SecurityFakeLocator(self.page, "security_key_button")
        return SecurityFakeLocator(self.page, "missing")


class SecurityFakePage(FakePage):
    def __init__(
        self,
        *,
        outcome: str = "same_tab",
        missing_kind: str | None = None,
        click_error_kind: str | None = None,
        has_security_key_clickable_ancestor: bool = True,
        has_button_role: bool = True,
        button_text: str = "Add a Security key or Passkey",
    ) -> None:
        super().__init__()
        self.url = "https://chatgpt.com/?state=PRIVATE_VALUE"
        self.body = "Welcome to ChatGPT"
        self.security_stage = "home"
        self.security_outcome = outcome
        self.missing_kind = missing_kind
        self.click_error_kind = click_error_kind
        self.has_security_key_clickable_ancestor = (
            has_security_key_clickable_ancestor
        )
        self.has_button_role = has_button_role
        self.button_text = button_text
        self.security_clicks: list[str] = []
        self.security_click_timeouts: list[int] = []
        self.security_click_no_wait_after: list[bool] = []
        self.security_click_force: list[bool] = []
        self.activation_fallbacks: list[str] = []
        self.security_nested_locator_calls: list[tuple[str, str]] = []
        self.security_role_calls: list[tuple[str, str, object]] = []
        self.old_page_closed_before_final = False

    def locator(self, selector: str) -> FakeBodyLocator | SecurityFakeLocator:
        self.locator_calls.append(selector)
        if selector == "body":
            return FakeBodyLocator(self)
        if (
            'translate(normalize-space(.),' in selector
            and '"add a security key or passkey"]' in selector
        ):
            return SecurityFakeLocator(
                self,
                "security_key_label",
                scope="page",
            )
        return SecurityFakeLocator(self, "missing")

    def get_by_test_id(self, test_id: str) -> SecurityFakeLocator:
        _ = test_id
        return SecurityFakeLocator(self, "missing")

    def get_by_text(self, text: str, *, exact: bool) -> SecurityFakeLocator:
        assert exact is True
        return self.security_text_locator(text, "page")

    def get_by_role(self, role: str, **kwargs: object) -> SecurityFakeLocator:
        return self.security_role_locator(role, kwargs, "page")

    def security_text_locator(self, text: str, scope: str) -> SecurityFakeLocator:
        _ = text
        return SecurityFakeLocator(self, "missing", scope=scope)

    def security_role_locator(
        self,
        role: str,
        kwargs: dict[str, object],
        scope: str,
    ) -> SecurityFakeLocator:
        name = kwargs.get("name")
        self.security_role_calls.append((scope, role, name))
        if (
            scope == "page"
            and role == "button"
            and isinstance(name, re.Pattern)
            and name.fullmatch(self.button_text) is not None
        ):
            return SecurityFakeLocator(
                self,
                "security_key_button",
                scope="role",
            )
        return SecurityFakeLocator(self, "missing", scope=scope)

    def security_available(self, kind: str, scope: str) -> bool:
        if kind in {"security_key_label", "security_key_button"} and (
            self.missing_kind == "security_key_button"
        ):
            return False
        if kind == self.missing_kind:
            return False
        expected = {
            "security_key_label": self.security_stage == "security_keys"
            and scope == "page",
            "security_key_button": self.security_stage == "security_keys"
            and (
                self.has_button_role
                if scope == "role"
                else self.has_security_key_clickable_ancestor
            ),
        }
        return expected.get(kind, False)

    async def goto(self, url: str, **kwargs: object) -> None:
        self.goto_calls.append((url, kwargs.get("wait_until"), kwargs.get("timeout")))
        self.events.append(("security_direct_settings_goto", url))
        if self.security_outcome == "direct_navigation_failed":
            raise RuntimeError("PRIVATE_DIRECT_NAVIGATION_ERROR")
        self.url = url
        self.body = (
            "Verify you are human"
            if self.security_outcome == "settings_challenge"
            else f"Security keys & passkeys {self.button_text}"
        )
        self.security_stage = "security_keys"

    def apply_security_step(self, kind: str) -> None:
        if kind in {"security_key_button", "security_key_label"}:
            self.security_stage = "request_dispatched"
            if self.security_outcome == "same_tab":
                self.url = (
                    "https://auth.openai.com/passkey-enroll?state=PRIVATE_VALUE"
                )
                self.body = "Set up a security key or passkey"
            elif self.security_outcome == "same_tab_intermediate":
                self.url = "https://auth.openai.com/authorize?state=PRIVATE_VALUE"
                self.body = "Authorizing"

                def finish_redirect() -> None:
                    self.old_page_closed_before_final = self.closed
                    self.url = "https://auth.openai.com/passkey-enroll?state=PRIVATE_VALUE"
                    self.body = "Set up a security key or passkey"

                asyncio.get_running_loop().call_soon(finish_redirect)
            elif self.security_outcome == "new_tab":
                new_page = SecurityFakePage()
                new_page.security_stage = "setup"
                new_page.url = (
                    "https://auth.openai.com/passkey-enroll?state=PRIVATE_VALUE"
                )
                new_page.body = "Set up a security key or passkey"
                self.context.add_page(new_page)
            elif self.security_outcome == "new_tab_intermediate":
                new_page = SecurityFakePage()
                new_page.security_stage = "setup"
                new_page.url = "https://auth.openai.com/authorize?state=PRIVATE_VALUE"
                new_page.body = "Authorizing"
                self.context.add_page(new_page)

                def finish_new_tab_redirect() -> None:
                    self.old_page_closed_before_final = self.closed
                    new_page.url = (
                        "https://auth.openai.com/passkey-enroll?state=PRIVATE_VALUE"
                    )
                    new_page.body = "Set up a security key or passkey"

                asyncio.get_running_loop().call_soon(finish_new_tab_redirect)
            elif self.security_outcome == "untrusted":
                self.url = "https://malicious.example/security-key"
                self.body = "Unexpected destination"
            elif self.security_outcome == "wrong_openai_path":
                self.url = "https://auth.openai.com/unexpected"
                self.body = "Unexpected destination"
            elif self.security_outcome == "challenge":
                self.url = "https://auth.openai.com/passkey-enroll"
                self.body = "Verify you are human"
            elif self.security_outcome == "load_failed":
                self.url = "https://auth.openai.com/passkey-enroll?load=failed"
                self.body = "Loading"
            elif self.security_outcome == "http_intermediate":
                self.url = "http://auth.openai.com/authorize"
                self.body = "Unexpected destination"
            elif self.security_outcome == "credentialed_intermediate":
                self.url = "https://user:pass@auth.openai.com/authorize"
                self.body = "Unexpected destination"

    async def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        if self.security_outcome == "load_failed" or "load-failed" in self.url:
            raise RuntimeError("PRIVATE_LOAD_ERROR")

    def is_closed(self) -> bool:
        return self.closed


async def no_delay_sleep(_seconds: float) -> None:
    return None


FIXED_SUBMITTED_AT = datetime(2026, 8, 9, 1, 30, tzinfo=timezone.utc)


def run_probe(
    page: FakePage,
    screenshot: Path,
    *,
    email: str = "person@example.com",
    **automation_options: object,
):
    playwright = FakePlaywright(page)
    automation_options.setdefault("random_uniform", lambda _minimum, _maximum: 1.0)
    automation_options.setdefault("delay_sleep", no_delay_sleep)
    automation_options.setdefault("utc_now", lambda: FIXED_SUBMITTED_AT)

    async def scenario():
        async with CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            screenshot,
            playwright_factory=lambda: FakeManager(playwright),
            **automation_options,
        ) as automation:
            return await automation.submit_email_and_continue(email)

    return asyncio.run(scenario())


def run_verification_submission(
    page: FakePage,
    screenshot: Path,
    *,
    verification_code: str = "222222",
    verification_visible: bool = True,
    **automation_options: object,
):
    page.url = "https://auth.openai.com/email-verification?state=PRIVATE_VALUE"
    page.body = "Enter the verification code"
    page.email_visible = False
    page.verification_visible = verification_visible
    playwright = FakePlaywright(page)
    automation_options.setdefault("random_uniform", lambda _minimum, _maximum: 1.0)
    automation_options.setdefault("delay_sleep", no_delay_sleep)
    automation_options.setdefault("utc_now", lambda: FIXED_SUBMITTED_AT)

    async def scenario():
        async with CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            screenshot,
            playwright_factory=lambda: FakeManager(playwright),
            **automation_options,
        ) as automation:
            return await automation.submit_verification_code_and_continue(
                verification_code
            )

    return asyncio.run(scenario())


def run_password_submission(
    page: FakePage,
    screenshot: Path,
    *,
    password: str = "Strong7!Password",
    **automation_options: object,
):
    page.url = "https://auth.openai.com/create-account/password"
    page.body = "Create your password"
    page.email_visible = False
    page.password_visible = True
    playwright = FakePlaywright(page)
    automation_options.setdefault("random_uniform", lambda _minimum, _maximum: 1.0)
    automation_options.setdefault("delay_sleep", no_delay_sleep)
    automation_options.setdefault("utc_now", lambda: FIXED_SUBMITTED_AT)

    async def scenario():
        async with CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            screenshot,
            playwright_factory=lambda: FakeManager(playwright),
            **automation_options,
        ) as automation:
            return await automation.submit_password_and_continue(password)

    return asyncio.run(scenario())


def test_password_is_filled_and_advances_to_verification(tmp_path: Path) -> None:
    page = FakePage()

    result = run_password_submission(page, tmp_path / "password.png")

    assert result.next_step == "verification"
    assert result.final_url == "https://auth.openai.com/email-verification"
    assert result.submitted_at_utc == FIXED_SUBMITTED_AT
    assert result.click_completed is True
    assert page.verification_visible is True
    assert ("password_fill", "<redacted>") in page.events
    assert ("password_click", None) in page.events


def test_invalid_generated_password_is_rejected_before_page_access(tmp_path: Path) -> None:
    with pytest.raises(PasswordStepError) as exc_info:
        run_password_submission(
            FakePage(),
            tmp_path / "password.png",
            password="too-short",
        )

    assert exc_info.value.code == "generated_password_invalid"


def test_password_page_rejection_is_reported(tmp_path: Path) -> None:
    page = FakePage(password_submit_outcome="rejected")

    with pytest.raises(PasswordStepError) as exc_info:
        run_password_submission(
            page,
            tmp_path / "password.png",
            next_step_timeout_ms=50,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == "password_rejected"


def run_profile_completion(
    page: FakePage,
    screenshot: Path,
    *,
    profile_url: str = "https://auth.openai.com/about-you?state=PRIVATE_VALUE",
    initial_body: str = "How old are you? Full name Age Finish creating account",
    name_visible: bool = True,
    age_visible: bool = True,
    **automation_options: object,
):
    page.url = profile_url
    page.body = initial_body
    page.email_visible = False
    page.verification_visible = False
    page.profile_name_visible = name_visible
    page.profile_age_visible = age_visible and page.profile_form_variant == "numeric_age"
    page.profile_birthday_visible = age_visible and page.profile_form_variant == "birthday"
    page.profile_segmented_visible = (
        age_visible and page.profile_form_variant == "segmented_birthday"
    )
    playwright = FakePlaywright(page)
    automation_options.setdefault("random_uniform", lambda _minimum, _maximum: 1.0)
    automation_options.setdefault("random_choice", lambda options: options[0])
    automation_options.setdefault("random_randint", lambda _minimum, _maximum: 30)
    automation_options.setdefault("delay_sleep", no_delay_sleep)
    automation_options.setdefault("utc_now", lambda: FIXED_SUBMITTED_AT)

    async def scenario():
        async with CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            screenshot,
            playwright_factory=lambda: FakeManager(playwright),
            **automation_options,
        ) as automation:
            return await automation.complete_profile_if_needed()

    return asyncio.run(scenario())


def run_security_navigation(
    page: SecurityFakePage,
    screenshot: Path,
    **automation_options: object,
):
    playwright = FakePlaywright(page)
    automation_options.setdefault("random_uniform", lambda _minimum, _maximum: 1.0)
    automation_options.setdefault("delay_sleep", no_delay_sleep)
    automation_options.setdefault("utc_now", lambda: FIXED_SUBMITTED_AT)

    async def scenario():
        async with CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            screenshot,
            playwright_factory=lambda: FakeManager(playwright),
            **automation_options,
        ) as automation:
            return await automation.navigate_to_security_key_setup()

    return asyncio.run(scenario())


def test_cdp_probe_masks_ip_strips_query_and_saves_screenshot(tmp_path: Path) -> None:
    page = FakePage()
    playwright = FakePlaywright(page)
    screenshot = tmp_path / "latest.png"

    async def scenario():
        async with CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            screenshot,
            playwright_factory=lambda: FakeManager(playwright),
            random_uniform=lambda _minimum, _maximum: 1.0,
            delay_sleep=no_delay_sleep,
            utc_now=lambda: FIXED_SUBMITTED_AT,
        ) as automation:
            return await automation.submit_email_and_continue("person@example.com")

    result = asyncio.run(scenario())

    assert result.egress_ip_masked == "203.0.*.*"
    assert result.final_url == "https://chatgpt.com/auth/login"
    assert result.next_step == "password"
    assert result.pre_continue_delay_ms == 5_000
    assert result.email_pre_fill_delays_ms == (5_000,)
    assert result.submitted_at_utc == FIXED_SUBMITTED_AT
    assert result.email_fill_attempts == 1
    assert result.email_form_reset_count == 0
    assert result.email_continue_attempts == 1
    assert result.email_post_submit_reset_count == 0
    assert result.login_challenge_observed is False
    assert result.email_form_ready_wait_ms == 5_000
    assert result.email_pre_continue_stable_waits_ms == (5_000,)
    assert result.email_form_stability_reset_count == 0
    assert page.filled_email == "person@example.com"
    assert page.clicked is True
    assert page.fill_timeout == 10_000
    assert page.input_value_timeout == 10_000
    assert page.click_timeout == 10_000
    assert page.get_by_role_calls == []
    assert page.goto_calls == [
        ("https://api64.ipify.org?format=json", "domcontentloaded", 90_000),
        (
            "https://chatgpt.com/auth/login?openaicom_referred=true",
            "domcontentloaded",
            90_000,
        ),
    ]
    assert screenshot.read_bytes() == b"fake-png"
    assert playwright.chromium.connections == [
        ("ws://127.0.0.1:50001/devtools/browser/test", 20_000)
    ]
    assert playwright.stopped is True


def test_cdp_reuses_ip_page_and_closes_extra_roxy_tabs(tmp_path: Path) -> None:
    extra_chatgpt_page = FakePage()
    extra_chatgpt_page.url = CHATGPT_LOGIN_URL
    ip_page = FakePage()
    ip_page.url = IP_CHECK_URL
    pages = [extra_chatgpt_page, ip_page]
    playwright = FakePlaywright(pages)

    async def scenario():
        async with CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            tmp_path / "latest.png",
            playwright_factory=lambda: FakeManager(playwright),
            random_uniform=lambda _minimum, _maximum: 1.0,
            delay_sleep=no_delay_sleep,
            utc_now=lambda: FIXED_SUBMITTED_AT,
        ) as automation:
            return await automation.submit_email_and_continue("person@example.com")

    result = asyncio.run(scenario())

    assert result.next_step == "password"
    assert extra_chatgpt_page.closed is True
    assert ip_page.closed is False
    assert ip_page.clicked is True
    assert extra_chatgpt_page.goto_calls == []
    assert ip_page.goto_calls[0] == (IP_CHECK_URL, "domcontentloaded", 90_000)


def test_verification_code_is_filled_waited_rechecked_and_submitted_once(
    tmp_path: Path,
) -> None:
    page = FakePage()
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        page.events.append(("sleep", seconds))

    def record_submission_boundary() -> datetime:
        page.events.append(("verification_submitted_at", None))
        return FIXED_SUBMITTED_AT

    result = run_verification_submission(
        page,
        tmp_path / "latest.png",
        random_uniform=lambda minimum, maximum: (minimum + maximum) / 2,
        delay_sleep=record_sleep,
        utc_now=record_submission_boundary,
    )

    assert result.final_url == "https://auth.openai.com/about-you"
    assert result.next_step == "transitioned"
    assert result.pre_continue_delay_ms == 2_000
    assert result.submitted_at_utc == FIXED_SUBMITTED_AT
    assert result.continue_attempts == 1
    assert result.click_completed is True
    assert result.click_exception_type is None
    assert result.post_click_state == "transitioned"
    assert result.url_changed is True
    assert result.input_visible_at_end is False
    assert result.button_visible_at_end is True
    assert sleeps == [2.0]
    assert page.filled_verification_code == "222222"
    assert page.verification_clicked is True
    assert page.events == [
        ("verification_fill", "222222"),
        ("verification_input_value", None),
        ("sleep", 2.0),
        ("verification_input_value", None),
        ("verification_submitted_at", None),
        ("verification_click", None),
    ]
    assert (tmp_path / "latest.png").read_bytes() == b"fake-png"


def test_verification_accepts_combined_verification_profile_page(
    tmp_path: Path,
) -> None:
    page = FakePage(verification_submit_outcome="combined_profile")
    result = run_verification_submission(page, tmp_path / "combined-profile.png")
    assert result.next_step == "transitioned"
    assert result.url_changed is False
    assert result.input_visible_at_end is False
    assert page.profile_name_visible is True
    assert page.profile_age_visible is True


def test_verification_accepts_same_url_email_verified_confirmation(
    tmp_path: Path,
) -> None:
    page = FakePage(verification_submit_outcome="verified_same_url")

    result = run_verification_submission(
        page,
        tmp_path / "verified-same-url.png",
    )

    assert result.next_step == "transitioned"
    assert result.url_changed is False
    assert result.input_visible_at_end is False
    assert result.button_visible_at_end is False


def test_verification_click_exception_is_accepted_after_transition(
    tmp_path: Path,
) -> None:
    page = FakePage(
        verification_click_error=RuntimeError("PRIVATE_CLICK_ERROR"),
        verification_click_error_outcome=True,
    )

    result = run_verification_submission(page, tmp_path / "latest.png")

    assert result.next_step == "transitioned"
    assert result.continue_attempts == 1
    assert result.click_completed is False
    assert result.click_exception_type == "RuntimeError"
    assert result.post_click_state == "transitioned"
    assert page.verification_clicked is False


def test_verification_stall_reloads_refills_and_submits_once_more(
    tmp_path: Path,
) -> None:
    page = FakePage(verification_submit_outcome="timeout_then_transitioned")

    result = run_verification_submission(
        page,
        tmp_path / "latest.png",
        next_step_timeout_ms=5,
        verification_retry_timeout_ms=1,
        poll_interval_seconds=0,
    )

    assert result.next_step == "transitioned"
    assert result.continue_attempts == 2
    assert page.reload_calls == 1
    assert [event for event in page.events if event[0] == "verification_fill"] == [
        ("verification_fill", "222222"),
        ("verification_fill", "222222"),
    ]
    assert [event for event in page.events if event[0] == "verification_click"] == [
        ("verification_click", None),
        ("verification_click", None),
    ]


def test_verification_click_failure_reloads_and_retries_once(tmp_path: Path) -> None:
    page = FakePage(
        verification_click_errors=[RuntimeError("PRIVATE_CLICK_ERROR"), None]
    )

    result = run_verification_submission(
        page,
        tmp_path / "latest.png",
        next_step_timeout_ms=5,
        verification_retry_timeout_ms=1,
        poll_interval_seconds=0,
    )

    assert result.next_step == "transitioned"
    assert result.continue_attempts == 2
    assert page.reload_calls == 1
    assert page.verification_clicked is True


@pytest.mark.parametrize(
    ("configure", "expected_code"),
    [
        (
            lambda page: setattr(page, "verification_visible", False),
            "verification_input_missing",
        ),
        (
            lambda page: setattr(page, "verification_continue_visible", False),
            "verification_continue_button_missing",
        ),
        (
            lambda page: setattr(
                page, "verification_fill_error", RuntimeError("PRIVATE_VALUE")
            ),
            "verification_fill_failed",
        ),
        (
            lambda page: setattr(page, "verification_input_value_override", "111111"),
            "verification_value_mismatch",
        ),
        (
            lambda page: setattr(
                page, "verification_click_error", RuntimeError("PRIVATE_VALUE")
            ),
            "verification_continue_click_failed",
        ),
    ],
)
def test_verification_form_errors_are_stable_and_redacted(
    tmp_path: Path,
    configure,
    expected_code: str,
) -> None:
    page = FakePage()
    configure(page)
    timeout_options = (
        {"next_step_timeout_ms": 1, "poll_interval_seconds": 0}
        if expected_code == "verification_continue_click_failed"
        else {}
    )

    with pytest.raises(VerificationStepError) as exc_info:
        run_verification_submission(
            page,
            tmp_path / "latest.png",
            verification_visible=expected_code != "verification_input_missing",
            **timeout_options,
        )

    assert exc_info.value.code == expected_code
    assert "PRIVATE_VALUE" not in exc_info.value.message
    assert page.verification_clicked is False
    if expected_code == "verification_continue_click_failed":
        assert exc_info.value.continue_attempts == 2
        assert exc_info.value.click_completed is False
        assert exc_info.value.click_exception_type == "RuntimeError"
        assert exc_info.value.post_click_state == "form_unchanged"


def test_verification_form_reset_stops_without_click(tmp_path: Path) -> None:
    page = FakePage()

    async def clear_during_delay(_seconds: float) -> None:
        page.filled_verification_code = ""

    with pytest.raises(VerificationStepError) as exc_info:
        run_verification_submission(
            page,
            tmp_path / "latest.png",
            delay_sleep=clear_during_delay,
        )

    assert exc_info.value.code == "verification_form_reset"
    assert page.verification_clicked is False
    assert [event for event in page.events if event[0] == "verification_fill"] == [
        ("verification_fill", "222222")
    ]


def test_rejected_verification_code_is_not_resubmitted(tmp_path: Path) -> None:
    page = FakePage(verification_submit_outcome="rejected")

    with pytest.raises(VerificationStepError) as exc_info:
        run_verification_submission(page, tmp_path / "latest.png")

    assert exc_info.value.code == "verification_code_rejected"
    assert [event for event in page.events if event[0] == "verification_click"] == [
        ("verification_click", None)
    ]
    assert [event for event in page.events if event[0] == "verification_fill"] == [
        ("verification_fill", "222222")
    ]


def test_verification_submit_stops_on_challenge(tmp_path: Path) -> None:
    page = FakePage(verification_submit_outcome="challenge")

    with pytest.raises(TargetChallengeError):
        run_verification_submission(page, tmp_path / "latest.png")

    assert page.verification_clicked is True


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        ("timeout", "verification_form_unchanged_after_click"),
        ("unknown", "verification_next_step_unknown"),
        ("changed_with_input", "verification_next_step_unknown"),
    ],
)
def test_verification_timeout_and_unknown_next_page_are_distinct(
    tmp_path: Path,
    outcome: str,
    expected_code: str,
) -> None:
    page = FakePage(verification_submit_outcome=outcome)

    with pytest.raises(VerificationStepError) as exc_info:
        run_verification_submission(
            page,
            tmp_path / "latest.png",
            next_step_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == expected_code
    assert page.verification_clicked is True
    assert exc_info.value.continue_attempts == (2 if outcome == "timeout" else 1)
    assert exc_info.value.click_completed is True
    assert exc_info.value.click_exception_type is None
    assert exc_info.value.wait_elapsed_ms is not None
    if outcome == "timeout":
        assert page.reload_calls == 1
        assert exc_info.value.post_click_state == "form_unchanged"
        assert exc_info.value.url_changed is False
        assert exc_info.value.input_visible_at_end is True
        assert exc_info.value.button_visible_at_end is True


def test_profile_fills_name_age_waits_and_clicks_finish_once(tmp_path: Path) -> None:
    page = FakePage()
    generated_names = iter(["Alice", "Miller"])
    generated_delays = iter([1.5, 2.5])

    def choose_name(_options: tuple[str, ...]) -> str:
        return next(generated_names)

    def choose_delay(_minimum: float, _maximum: float) -> float:
        return next(generated_delays)

    async def record_sleep(seconds: float) -> None:
        page.events.append(("sleep", seconds))

    def record_submission_boundary() -> datetime:
        page.events.append(("profile_submitted_at", None))
        return FIXED_SUBMITTED_AT

    result = run_profile_completion(
        page,
        tmp_path / "latest.png",
        random_choice=choose_name,
        random_randint=lambda _minimum, _maximum: 30,
        random_uniform=choose_delay,
        delay_sleep=record_sleep,
        utc_now=record_submission_boundary,
    )

    assert result.full_name == "Alice Miller"
    assert result.age == 30
    assert result.name_to_age_delay_ms == 1_500
    assert result.age_to_finish_delay_ms == 2_500
    assert result.submitted_at_utc == FIXED_SUBMITTED_AT
    assert result.final_url == "https://chatgpt.com/"
    assert result.next_step == "account_created"
    assert result.skipped is False
    assert result.skip_reason is None
    assert page.profile_clicked is True
    assert page.events == [
        ("profile_name_fill", "Alice Miller"),
        ("profile_name_input_value", None),
        ("sleep", 1.5),
        ("profile_name_input_value", None),
        ("profile_age_fill", "30"),
        ("profile_age_input_value", None),
        ("sleep", 2.5),
        ("profile_name_input_value", None),
        ("profile_age_input_value", None),
        ("profile_submitted_at", None),
        ("profile_finish_click", None),
    ]
    assert (tmp_path / "latest.png").read_bytes() == b"fake-png"


@pytest.mark.parametrize(
    ("form_variant", "submit_text", "locator_strategy", "expected_submit"),
    [
        ("numeric_age", "Finish creating account", "strict_attributes", "finish_creating_account"),
        ("numeric_age", "Continue", "semantic_labels", "continue"),
        ("numeric_age", "続行", "strict_attributes", "continue"),
        ("birthday", "Finish creating account", "strict_attributes", "finish_creating_account"),
        ("birthday", "Continue", "semantic_labels", "continue"),
        ("birthday", "アカウントの作成を完了", "strict_attributes", "finish_creating_account"),
    ],
)
def test_profile_supports_age_birthday_and_submit_variants(
    tmp_path: Path,
    form_variant: str,
    submit_text: str,
    locator_strategy: str,
    expected_submit: str,
) -> None:
    page = FakePage(
        profile_form_variant=form_variant,
        profile_submit_text=submit_text,
        profile_locator_strategy=locator_strategy,
    )
    generated = iter([30, 0])
    result = run_profile_completion(
        page,
        tmp_path / f"{form_variant}-{expected_submit}.png",
        random_randint=lambda _minimum, _maximum: next(generated),
    )

    assert result.form_variant == form_variant
    assert result.locator_strategy == locator_strategy
    assert result.submit_variant == expected_submit
    assert page.profile_clicked is True
    if form_variant == "birthday":
        birthday_fill = next(value for event, value in page.events if event == "profile_birthday_fill")
        assert birthday_fill == "1995-08-10"
        assert birthday_fill not in repr(result)
    else:
        assert ("profile_age_fill", "30") in page.events


def test_profile_uses_unique_structural_submit_for_unknown_locale(tmp_path: Path) -> None:
    page = FakePage(profile_submit_text="次へ")
    result = run_profile_completion(page, tmp_path / "structural-submit.png")
    assert result.submit_variant == "structural_submit"
    assert page.profile_clicked is True


def test_profile_supports_reference_flow_profile_route(tmp_path: Path) -> None:
    page = FakePage(profile_submit_text="続行")
    result = run_profile_completion(
        page,
        tmp_path / "create-account-profile.png",
        profile_url="https://auth.openai.com/create-account/profile?state=PRIVATE_VALUE",
        initial_body="氏名 年齢 続行",
    )
    assert result.next_step == "account_created"
    assert result.submit_variant == "continue"


def test_profile_fills_react_aria_segmented_birthday(tmp_path: Path) -> None:
    page = FakePage(profile_form_variant="segmented_birthday")
    generated = iter([30, 0])

    result = run_profile_completion(
        page,
        tmp_path / "segmented-birthday.png",
        random_randint=lambda _minimum, _maximum: next(generated),
    )

    assert result.form_variant == "birthday"
    assert result.locator_strategy == "segmented_date"
    assert page.profile_hidden_birthday == "1995-08-10"
    assert ("profile_segment_year_fill", "1995") in page.events
    assert ("profile_segment_month_fill", "08") in page.events
    assert ("profile_segment_day_fill", "10") in page.events
    assert page.profile_clicked is True


def test_profile_ignores_hidden_and_unrelated_forms_for_submit(tmp_path: Path) -> None:
    page = MultiProfileFormPage()
    result = run_profile_completion(page, tmp_path / "latest.png")
    assert result.form_variant == "numeric_age"
    assert page.profile_submit_form_indices == [2]
    assert page.profile_clicked is True


@pytest.mark.parametrize(
    ("reference", "age", "offset", "expected"),
    [
        (datetime(2026, 8, 13, tzinfo=timezone.utc), 25, 0, "2000-08-14"),
        (datetime(2026, 8, 13, tzinfo=timezone.utc), 35, 364, "1991-08-13"),
        (datetime(2024, 2, 29, tzinfo=timezone.utc), 25, 0, "1998-03-01"),
    ],
)
def test_profile_birthday_generation_respects_exact_age_window(
    tmp_path: Path,
    reference: datetime,
    age: int,
    offset: int,
    expected: str,
) -> None:
    page = FakePage(profile_form_variant="birthday")
    values = iter([age, offset])
    run_profile_completion(
        page,
        tmp_path / "birthday.png",
        random_randint=lambda _minimum, _maximum: next(values),
        utc_now=lambda: reference,
    )
    assert ("profile_birthday_fill", expected) in page.events


def test_profile_click_exception_accepts_confirmed_home_without_retry(tmp_path: Path) -> None:
    page = FakePage(
        profile_finish_click_error=RuntimeError("PRIVATE_VALUE"),
        profile_finish_click_error_outcome=True,
        profile_home_visible_after=1,
    )
    result = run_profile_completion(page, tmp_path / "latest.png")
    assert result.next_step == "account_created"
    assert [event for event in page.events if event[0] == "profile_finish_click"] == []


def test_profile_is_skipped_when_account_home_is_already_available(
    tmp_path: Path,
) -> None:
    page = FakePage(
        profile_home_visible_after=1,
        profile_home_div_only=True,
    )

    def unexpected_choice(_options: tuple[str, ...]) -> str:
        raise AssertionError("profile name must not be generated")

    def unexpected_age(_minimum: int, _maximum: int) -> int:
        raise AssertionError("profile age must not be generated")

    def unexpected_delay(_minimum: float, _maximum: float) -> float:
        raise AssertionError("profile delay must not be generated")

    async def unexpected_sleep(_seconds: float) -> None:
        raise AssertionError("profile action delay must not run")

    result = run_profile_completion(
        page,
        tmp_path / "latest.png",
        profile_url="https://chatgpt.com/?state=PRIVATE_VALUE",
        initial_body="Welcome to ChatGPT",
        name_visible=False,
        age_visible=False,
        random_choice=unexpected_choice,
        random_randint=unexpected_age,
        random_uniform=unexpected_delay,
        delay_sleep=unexpected_sleep,
    )

    assert result.final_url == "https://chatgpt.com/"
    assert result.next_step == "account_created"
    assert result.skipped is True
    assert result.skip_reason == "already_configured"
    assert result.full_name is None
    assert result.age is None
    assert result.name_to_age_delay_ms is None
    assert result.age_to_finish_delay_ms is None
    assert result.submitted_at_utc is None
    assert page.profile_clicked is False
    assert page.filled_profile_name == ""
    assert page.filled_profile_age == ""
    assert page.events == []
    assert (
        'xpath=//*[@data-testid="accounts-profile-button" '
        'and @role="button" '
        'and not(ancestor-or-self::*[@inert]) '
        'and not(ancestor-or-self::*[@aria-hidden="true"])]'
        in page.locator_calls
    )
    assert '[data-testid="accounts-profile-button"]:visible' not in page.locator_calls
    assert page.get_by_test_id_calls == []


def test_profile_is_skipped_with_localized_profile_menu_label(
    tmp_path: Path,
) -> None:
    page = FakePage(
        profile_home_visible_after=1,
        profile_home_div_only=True,
        profile_home_localized_only=True,
    )

    result = run_profile_completion(
        page,
        tmp_path / "latest.png",
        profile_url="https://chatgpt.com/?state=PRIVATE_VALUE",
        initial_body="准备好了，随时开始",
        name_visible=False,
        age_visible=False,
    )

    assert result.skipped is True
    assert result.skip_reason == "already_configured"
    assert page.profile_clicked is False
    assert page.filled_profile_name == ""
    assert page.filled_profile_age == ""
    assert page.events == []


def test_profile_waits_for_hydrated_account_home_before_skipping(
    tmp_path: Path,
) -> None:
    page = FakePage(profile_home_visible_after=3)

    result = run_profile_completion(
        page,
        tmp_path / "latest.png",
        profile_url="https://chatgpt.com/?state=PRIVATE_VALUE",
        initial_body="Loading",
        name_visible=False,
        age_visible=False,
        next_step_timeout_ms=100,
        poll_interval_seconds=0,
    )

    assert result.skipped is True
    assert result.skip_reason == "already_configured"
    assert page.profile_home_checks >= 3
    assert page.profile_clicked is False


def test_profile_route_stops_on_challenge_before_form_or_skip(
    tmp_path: Path,
) -> None:
    page = FakePage(profile_home_visible_after=1)

    with pytest.raises(TargetChallengeError):
        run_profile_completion(
            page,
            tmp_path / "latest.png",
            profile_url="https://chatgpt.com/",
            initial_body="Verify you are human",
            name_visible=False,
            age_visible=False,
        )

    assert page.profile_clicked is False


@pytest.mark.parametrize("age", [25, 35])
def test_profile_age_generation_includes_both_boundaries(
    tmp_path: Path,
    age: int,
) -> None:
    result = run_profile_completion(
        FakePage(),
        tmp_path / "latest.png",
        random_randint=lambda _minimum, _maximum: age,
    )

    assert result.age == age


@pytest.mark.parametrize(
    ("page_factory", "helper_options", "expected_code"),
    [
        (
            lambda: FakePage(),
            {
                "profile_url": "https://auth.openai.com/unexpected",
                "next_step_timeout_ms": 1,
                "poll_interval_seconds": 0,
            },
            "profile_page_missing",
        ),
        (
            lambda: FakePage(),
            {
                "profile_url": "https://chatgpt.com/auth/unexpected",
                "initial_body": "Unknown page",
                "next_step_timeout_ms": 1,
                "poll_interval_seconds": 0,
            },
            "profile_page_missing",
        ),
        (
            lambda: FakePage(),
            {
                "name_visible": False,
                "next_step_timeout_ms": 1,
                "poll_interval_seconds": 0,
            },
            "profile_name_input_missing",
        ),
        (
            lambda: FakePage(),
            {
                "age_visible": False,
                "next_step_timeout_ms": 1,
                "poll_interval_seconds": 0,
            },
            "profile_age_input_missing",
        ),
        (
            lambda: FakePage(
                profile_name_fill_error=RuntimeError("PRIVATE_VALUE")
            ),
            {},
            "profile_name_fill_failed",
        ),
        (
            lambda: FakePage(profile_name_value_override="Wrong Name"),
            {},
            "profile_name_value_mismatch",
        ),
        (
            lambda: FakePage(
                profile_age_fill_error=RuntimeError("PRIVATE_VALUE")
            ),
            {},
            "profile_age_fill_failed",
        ),
        (
            lambda: FakePage(profile_age_value_override="99"),
            {},
            "profile_age_value_mismatch",
        ),
        (
            lambda: FakePage(),
            {"finish_visible": False},
            "profile_finish_button_missing",
        ),
        (
            lambda: FakePage(),
            {"finish_enabled": False},
            "profile_finish_button_missing",
        ),
        (
            lambda: FakePage(
                profile_finish_click_error=RuntimeError("PRIVATE_VALUE")
            ),
            {},
            "profile_finish_click_failed",
        ),
    ],
)
def test_profile_form_errors_are_stable_and_redacted(
    tmp_path: Path,
    page_factory,
    helper_options: dict[str, object],
    expected_code: str,
) -> None:
    page = page_factory()
    options = dict(helper_options)
    if "finish_visible" in options:
        page.profile_finish_visible = bool(options.pop("finish_visible"))
    if "finish_enabled" in options:
        page.profile_finish_enabled = bool(options.pop("finish_enabled"))

    with pytest.raises(ProfileStepError) as exc_info:
        run_profile_completion(
            page,
            tmp_path / "latest.png",
            **options,
        )

    assert exc_info.value.code == expected_code
    assert "PRIVATE_VALUE" not in exc_info.value.message
    assert page.profile_clicked is False


@pytest.mark.parametrize("reset_stage", ["before_age", "before_finish"])
def test_profile_form_reset_stops_without_clicking(
    tmp_path: Path,
    reset_stage: str,
) -> None:
    page = FakePage()
    sleep_count = 0

    async def reset_form(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if reset_stage == "before_age" and sleep_count == 1:
            page.filled_profile_name = ""
        if reset_stage == "before_finish" and sleep_count == 2:
            page.filled_profile_age = ""

    with pytest.raises(ProfileStepError) as exc_info:
        run_profile_completion(
            page,
            tmp_path / "latest.png",
            delay_sleep=reset_form,
        )

    assert exc_info.value.code == "profile_form_reset"
    assert page.profile_clicked is False


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        ("rejected", "profile_rejected"),
        ("timeout", "profile_submit_timeout"),
        ("unknown", "profile_next_step_unknown"),
        ("changed_with_fields", "profile_next_step_unknown"),
    ],
)
def test_profile_submit_failures_are_distinct_and_not_retried(
    tmp_path: Path,
    outcome: str,
    expected_code: str,
) -> None:
    page = FakePage(profile_submit_outcome=outcome)

    with pytest.raises(ProfileStepError) as exc_info:
        run_profile_completion(
            page,
            tmp_path / "latest.png",
            next_step_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == expected_code
    assert [event for event in page.events if event[0] == "profile_finish_click"] == [
        ("profile_finish_click", None)
    ]


def test_profile_submit_stops_on_challenge(tmp_path: Path) -> None:
    page = FakePage(profile_submit_outcome="challenge")

    with pytest.raises(TargetChallengeError):
        run_profile_completion(page, tmp_path / "latest.png")

    assert page.profile_clicked is True


def test_invalid_profile_delay_range_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="账号资料步骤随机等待范围无效"):
        CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            tmp_path / "latest.png",
            profile_action_delay_min_seconds=3,
            profile_action_delay_max_seconds=1,
        )


def test_security_navigation_uses_direct_settings_and_single_delay(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage()
    random_calls: list[tuple[float, float]] = []
    sleep_calls: list[float] = []

    def choose_delay(minimum: float, maximum: float) -> float:
        random_calls.append((minimum, maximum))
        return 5.0

    async def record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        page.events.append(("sleep", seconds))

    result = run_security_navigation(
        page,
        tmp_path / "latest.png",
        random_uniform=choose_delay,
        delay_sleep=record_sleep,
    )

    assert page.goto_calls == [
        (CHATGPT_PASSKEY_SETTINGS_URL, "domcontentloaded", 45_000)
    ]
    assert result.final_url == "https://auth.openai.com/passkey-enroll"
    assert result.navigation_mode == "direct_settings"
    assert result.delays_ms == (5_000,)
    assert result.requested_at_utc == FIXED_SUBMITTED_AT
    assert result.opened_new_page is False
    assert random_calls == [(4.0, 6.0)]
    assert sleep_calls == [5.0]
    assert page.security_clicks == ["security_key_button"]
    assert [event[0] for event in page.events] == [
        "security_direct_settings_goto",
        "sleep",
        "security_security_key_button_click",
    ]
    assert page.security_click_timeouts == [10_000]
    assert page.security_click_no_wait_after == [True]
    assert page.security_click_force == [False]
    assert page.activation_fallbacks == []
    assert len(page.security_role_calls) == 1
    role_scope, role, role_name = page.security_role_calls[0]
    assert role_scope == "page"
    assert role == "button"
    assert isinstance(role_name, re.Pattern)
    assert role_name.fullmatch("Add a Security key or Passkey") is not None
    assert not any("accounts-profile-button" in call for call in page.locator_calls)
    assert (tmp_path / "latest.png").exists()


@pytest.mark.parametrize("delay_seconds", [4.0, 6.0])
def test_security_navigation_delay_includes_direct_route_boundaries(
    tmp_path: Path,
    delay_seconds: float,
) -> None:
    result = run_security_navigation(
        SecurityFakePage(),
        tmp_path / "latest.png",
        random_uniform=lambda minimum, maximum: (
            delay_seconds
            if (minimum, maximum) == (4.0, 6.0)
            else pytest.fail("unexpected delay range")
        ),
    )

    assert result.delays_ms == (int(delay_seconds * 1000),)


def test_security_navigation_clicks_final_text_when_no_clickable_ancestor(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(
        has_security_key_clickable_ancestor=False,
        has_button_role=False,
    )

    result = run_security_navigation(page, tmp_path / "latest.png")

    assert result.final_url == "https://auth.openai.com/passkey-enroll"
    assert page.security_clicks == ["security_key_label"]
    assert page.security_clicks.count("security_key_label") == 1
    assert "security_key_button" not in page.security_clicks


@pytest.mark.parametrize(
    "button_text",
    [
        "Add a Security key or Passkey",
        "Add a Security key or PassKey",
    ],
)
def test_security_navigation_button_role_is_case_insensitive(
    button_text: str,
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(button_text=button_text)

    result = run_security_navigation(page, tmp_path / "latest.png")

    assert result.final_url == "https://auth.openai.com/passkey-enroll"
    assert page.security_clicks == ["security_key_button"]
    _, role, role_name = page.security_role_calls[0]
    assert role == "button"
    assert isinstance(role_name, re.Pattern)
    assert role_name.fullmatch(button_text) is not None


def test_security_navigation_uses_nested_text_fallback_for_hidden_role_candidate(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(has_button_role=False)

    result = run_security_navigation(page, tmp_path / "latest.png")

    assert result.final_url == "https://auth.openai.com/passkey-enroll"
    assert page.security_clicks == ["security_key_button"]
    assert any(
        "translate(normalize-space(.)," in selector
        and '"add a security key or passkey"]' in selector
        for selector in page.locator_calls
    )


def test_security_navigation_switches_to_new_page_and_closes_old_page(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(outcome="new_tab")

    result = run_security_navigation(page, tmp_path / "latest.png")

    assert result.opened_new_page is True
    assert result.final_url == "https://auth.openai.com/passkey-enroll"
    assert result.navigation_mode == "direct_settings"
    assert page.closed is True
    assert len([candidate for candidate in page.context.pages if not candidate.closed]) == 1


def test_security_navigation_always_uses_direct_settings_url(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage()
    page.url = "https://auth.openai.com/passkey-enroll?state=PRIVATE_VALUE"

    result = run_security_navigation(page, tmp_path / "latest.png")

    assert page.goto_calls[0][0] == CHATGPT_PASSKEY_SETTINGS_URL
    assert result.final_url == "https://auth.openai.com/passkey-enroll"


def test_security_navigation_missing_button_has_stable_error(tmp_path: Path) -> None:
    page = SecurityFakePage(missing_kind="security_key_button")

    with pytest.raises(SecurityNavigationError) as exc_info:
        run_security_navigation(
            page,
            tmp_path / "latest.png",
            security_action_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.stage == "security_key_request"
    assert exc_info.value.code == "security_key_button_missing"
    assert "PRIVATE" not in exc_info.value.message


def test_security_navigation_click_failure_is_safe_and_not_retried(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(click_error_kind="security_key_button")

    with pytest.raises(SecurityNavigationError) as exc_info:
        run_security_navigation(page, tmp_path / "latest.png")

    assert exc_info.value.stage == "security_key_request"
    assert exc_info.value.code == "security_key_request_click_failed"
    assert page.security_clicks.count("security_key_button") == 0
    assert "PRIVATE_SECURITY_CLICK_ERROR" not in exc_info.value.message


def test_security_direct_settings_navigation_failure_does_not_fallback(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(outcome="direct_navigation_failed")

    with pytest.raises(SecurityNavigationError) as exc_info:
        run_security_navigation(page, tmp_path / "latest.png")

    assert exc_info.value.stage == "security_settings"
    assert exc_info.value.code == "security_settings_navigation_failed"
    assert page.goto_calls == [
        (CHATGPT_PASSKEY_SETTINGS_URL, "domcontentloaded", 45_000)
    ]
    assert page.security_clicks == []


def test_security_direct_settings_challenge_stops_before_delay_and_click(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(outcome="settings_challenge")

    async def unexpected_sleep(_seconds: float) -> None:
        raise AssertionError("direct settings challenge must stop before delay")

    with pytest.raises(SecurityNavigationError) as exc_info:
        run_security_navigation(
            page,
            tmp_path / "latest.png",
            delay_sleep=unexpected_sleep,
        )

    assert exc_info.value.stage == "security_settings"
    assert exc_info.value.code == "security_challenge_detected"
    assert page.security_clicks == []


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        ("untrusted", "security_key_page_untrusted"),
        ("wrong_openai_path", "security_key_page_timeout"),
        ("challenge", "security_challenge_detected"),
        ("load_failed", "security_key_page_load_failed"),
        ("timeout", "security_key_page_timeout"),
    ],
)
def test_security_navigation_destination_failures_are_distinct(
    tmp_path: Path,
    outcome: str,
    expected_code: str,
) -> None:
    page = SecurityFakePage(outcome=outcome)

    with pytest.raises(SecurityNavigationError) as exc_info:
        run_security_navigation(
            page,
            tmp_path / "latest.png",
            security_navigation_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.stage == "security_key_page"
    assert exc_info.value.code == expected_code
    assert page.security_clicks.count("security_key_button") == 1


@pytest.mark.parametrize("outcome", ["same_tab_intermediate", "new_tab_intermediate"])
def test_security_navigation_waits_for_trusted_intermediate_redirect(
    tmp_path: Path,
    outcome: str,
) -> None:
    page = SecurityFakePage(outcome=outcome)

    result = run_security_navigation(
        page,
        tmp_path / "latest.png",
        poll_interval_seconds=0,
    )

    assert result.final_url == "https://auth.openai.com/passkey-enroll"
    assert result.redirect_state == "final_after_trusted_intermediate"
    assert result.redirect_poll_count >= 2
    assert result.redirect_elapsed_ms >= 0
    assert page.security_clicks == ["security_key_button"]
    assert page.old_page_closed_before_final is False
    if outcome == "new_tab_intermediate":
        assert page.closed is True
        assert len([candidate for candidate in page.context.pages if not candidate.closed]) == 1


def test_security_navigation_times_out_on_trusted_intermediate_page(
    tmp_path: Path,
) -> None:
    page = SecurityFakePage(outcome="wrong_openai_path")

    with pytest.raises(SecurityNavigationError) as exc_info:
        run_security_navigation(
            page,
            tmp_path / "latest.png",
            security_navigation_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == "security_key_page_timeout"
    assert exc_info.value.redirect_state == "trusted_intermediate_timeout"
    assert exc_info.value.redirect_poll_count >= 1
    assert exc_info.value.redirect_elapsed_ms >= 0


@pytest.mark.parametrize("outcome", ["http_intermediate", "credentialed_intermediate"])
def test_security_navigation_rejects_unsafe_redirect_immediately(
    tmp_path: Path,
    outcome: str,
) -> None:
    page = SecurityFakePage(outcome=outcome)

    with pytest.raises(SecurityNavigationError) as exc_info:
        run_security_navigation(
            page,
            tmp_path / "latest.png",
            security_navigation_timeout_ms=45_000,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == "security_key_page_untrusted"
    assert exc_info.value.redirect_state == "untrusted_destination"
    assert exc_info.value.redirect_poll_count == 1
    assert exc_info.value.redirect_elapsed_ms >= 0
    assert "PRIVATE_VALUE" not in exc_info.value.message


@pytest.mark.parametrize(
    "options",
    [
        {
            "security_action_delay_min_seconds": 2,
            "security_action_delay_max_seconds": 1,
        },
        {"security_action_timeout_ms": 0},
        {"security_navigation_timeout_ms": 0},
    ],
)
def test_invalid_security_navigation_configuration_is_rejected(
    tmp_path: Path,
    options: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="安全设置导航"):
        CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            tmp_path / "latest.png",
            **options,
        )


@pytest.mark.parametrize(
    ("next_step", "expected", "expected_url"),
    [
        ("password", "password", "https://chatgpt.com/auth/login"),
        ("verification", "verification", "https://chatgpt.com/auth/login"),
        (
            "verification_url_loading",
            "verification",
            "https://auth.openai.com/email-verification",
        ),
    ],
)
def test_email_continue_accepts_supported_next_steps(
    tmp_path: Path,
    next_step: str,
    expected: str,
    expected_url: str,
) -> None:
    result = run_probe(FakePage(next_step=next_step), tmp_path / "latest.png")

    assert result.next_step == expected
    assert result.final_url == expected_url


def test_email_continue_rejects_unrecognized_url_transition(tmp_path: Path) -> None:
    with pytest.raises(EmailStepError) as exc_info:
        run_probe(
            FakePage(next_step="transitioned"),
            tmp_path / "latest.png",
            next_step_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == "email_next_step_unknown"


def test_email_fill_waits_random_delay_before_continue_click(tmp_path: Path) -> None:
    page = FakePage()
    sleep_calls: list[float] = []
    random_bounds: list[tuple[float, float]] = []

    def fixed_uniform(minimum: float, maximum: float) -> float:
        random_bounds.append((minimum, maximum))
        return 4.25 if (minimum, maximum) == (3.0, 5.0) else 2.25

    async def record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        page.events.append(("sleep", seconds))

    def record_submission_boundary() -> datetime:
        page.events.append(("submitted_at", None))
        return FIXED_SUBMITTED_AT

    result = run_probe(
        page,
        tmp_path / "latest.png",
        random_uniform=fixed_uniform,
        delay_sleep=record_sleep,
        utc_now=record_submission_boundary,
    )

    assert random_bounds == [(3.0, 5.0), (3.0, 5.0)]
    assert sleep_calls == [0.5] * 20
    fill_index = page.events.index(("fill", "person@example.com"))
    click_index = page.events.index(("click", None))
    assert sum(
        event[1]
        for event in page.events[:fill_index]
        if event[0] == "sleep"
    ) == 5.0
    assert sum(
        event[1]
        for event in page.events[fill_index:click_index]
        if event[0] == "sleep"
    ) == 5.0
    assert page.events[click_index - 1] == ("submitted_at", None)
    assert result.pre_continue_delay_ms == 5_000
    assert result.email_pre_fill_delays_ms == (5_000,)
    assert result.email_form_ready_wait_ms == 5_000
    assert result.email_pre_continue_stable_waits_ms == (5_000,)
    assert result.email_fill_attempts == 1
    assert result.email_form_reset_count == 0
    assert result.email_continue_attempts == 1
    assert result.email_post_submit_reset_count == 0


def test_email_continue_is_scoped_and_directly_clicked(tmp_path: Path) -> None:
    page = FakePage()

    result = run_probe(page, tmp_path / "latest.png")

    assert result.next_step == "password"
    assert page.continue_trial_count == 0
    assert page.continue_click_invocation_count == 1
    assert page.continue_click_count == 1
    assert any(
        'self::button or self::input' in selector
        and '@type="submit"' in selector
        and "//form[" in selector
        for selector in page.locator_calls
    )
    assert 'button[type="submit"]' not in page.locator_calls


def test_japanese_email_submit_text_uses_form_structure(tmp_path: Path) -> None:
    page = FakePage(continue_text="続行")

    result = run_probe(page, tmp_path / "latest.png")

    assert result.next_step == "password"
    assert page.continue_click_count == 1


def test_email_continue_click_waits_for_temporarily_disabled_button(
    tmp_path: Path,
) -> None:
    page = FakePage(continue_enabled=False)
    sleep_count = 0

    async def enable_after_wait(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            page.continue_enabled = True

    result = run_probe(
        page,
        tmp_path / "latest.png",
        delay_sleep=enable_after_wait,
    )

    assert result.next_step == "password"
    assert page.continue_trial_count == 0
    assert page.continue_click_invocation_count == 1
    assert result.email_form_ready_wait_ms >= 6_000


def test_click_exception_after_navigation_is_reconciled(tmp_path: Path) -> None:
    page = FakePage(
        next_step="password",
        click_errors=[RuntimeError("detached during navigation")],
        click_error_outcome="verification",
    )

    result = run_probe(page, tmp_path / "latest.png")

    assert result.next_step == "verification"
    assert result.email_continue_attempts == 1
    assert result.email_continue_click_failures == 1
    assert result.email_continue_recovery_state == "next_step"
    assert result.email_continue_attempt_states == (
        "next_step_observed_during_click",
    )
    assert result.email_continue_dispatch_observed is True
    assert result.email_continue_click_exception_types == ("RuntimeError",)
    assert page.continue_click_invocation_count == 1


def test_click_exception_on_stable_form_refills_and_retries_once(
    tmp_path: Path,
) -> None:
    page = FakePage(
        next_step="password",
        click_errors=[RuntimeError("detached node"), None],
        click_error_outcome="stable",
    )

    result = run_probe(page, tmp_path / "latest.png")

    assert result.next_step == "password"
    assert result.email_fill_attempts == 2
    assert result.email_continue_attempts == 2
    assert result.email_continue_click_failures == 1
    assert result.email_continue_recovery_state == "stable_form_retry"
    assert result.email_continue_attempt_states[-1] in {
        "click_succeeded",
        "next_step_observed_during_click",
    }
    assert page.continue_click_invocation_count == 2
    assert page.continue_click_count == 1


def test_click_exception_with_reset_refills_and_retries_once(
    tmp_path: Path,
) -> None:
    page = FakePage(
        next_step="verification",
        click_errors=[RuntimeError("page refresh") , None],
        click_error_outcome="reset",
    )

    result = run_probe(page, tmp_path / "latest.png")

    assert result.next_step == "verification"
    assert result.email_fill_attempts == 2
    assert result.email_continue_attempts == 2
    assert result.email_post_submit_reset_count == 1
    assert result.email_continue_click_failures == 1
    assert result.email_continue_recovery_state == "form_reset"
    assert page.continue_click_invocation_count == 2


def test_two_click_exceptions_stop_without_third_click(tmp_path: Path) -> None:
    page = FakePage(
        click_errors=[RuntimeError("first"), RuntimeError("second")],
        click_error_outcome="stable",
    )

    with pytest.raises(EmailStepError) as exc_info:
        run_probe(page, tmp_path / "latest.png")

    assert exc_info.value.code == "continue_click_failed"
    assert exc_info.value.click_attempts == 2
    assert exc_info.value.click_failures == 2
    assert page.continue_click_invocation_count == 2
    assert page.continue_click_count == 0


def test_continue_actionability_failure_stops_after_two_real_attempts(
    tmp_path: Path,
) -> None:
    page = FakePage(continue_enabled=False)

    with pytest.raises(EmailStepError) as exc_info:
        run_probe(
            page,
            tmp_path / "latest.png",
            next_step_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == "email_form_not_stable"
    assert page.continue_trial_count == 0
    assert page.continue_click_invocation_count == 0


def test_failure_screenshot_retries_during_page_transition(tmp_path: Path) -> None:
    page = FakePage(screenshot_failures=2)
    automation = CdpBrowserAutomation(
        "ws://127.0.0.1:50001/devtools/browser/test",
        tmp_path / "latest.png",
    )

    captured = asyncio.run(automation._safe_screenshot_after_settle(page))

    assert captured is True
    assert page.screenshot_calls == 3
    assert (tmp_path / "latest.png").read_bytes() == b"fake-png"


def test_email_is_refilled_once_when_page_resets_before_continue(tmp_path: Path) -> None:
    page = FakePage()
    generated_delays = iter([3.25, 4.25, 4.75, 3.5])
    first_fill_cleared = False

    def sequential_uniform(_minimum: float, _maximum: float) -> float:
        return next(generated_delays)

    async def reset_first_fill(seconds: float) -> None:
        nonlocal first_fill_cleared
        page.events.append(("sleep", seconds))
        if page.filled_email and not first_fill_cleared:
            page.filled_email = ""
            first_fill_cleared = True

    def record_submission_boundary() -> datetime:
        page.events.append(("submitted_at", None))
        return FIXED_SUBMITTED_AT

    result = run_probe(
        page,
        tmp_path / "latest.png",
        random_uniform=sequential_uniform,
        delay_sleep=reset_first_fill,
        utc_now=record_submission_boundary,
    )

    assert [event for event in page.events if event[0] == "fill"] == [
        ("fill", "person@example.com"),
        ("fill", "person@example.com"),
    ]
    assert len([event for event in page.events if event[0] == "click"]) == 1
    assert result.pre_continue_delay_ms == 5_000
    assert result.email_pre_fill_delays_ms == (5_000, 5_000)
    assert result.email_fill_attempts == 2
    assert result.email_form_reset_count == 1
    assert result.email_continue_attempts == 1
    assert result.email_post_submit_reset_count == 0
    assert page.clicked is True


def test_repeated_email_form_reset_stops_without_clicking(tmp_path: Path) -> None:
    page = FakePage()

    async def reset_every_fill(seconds: float) -> None:
        page.events.append(("sleep", seconds))
        if page.filled_email:
            page.filled_email = ""

    with pytest.raises(EmailStepError) as exc_info:
        run_probe(
            page,
            tmp_path / "latest.png",
            random_uniform=lambda _minimum, _maximum: 1.5,
            delay_sleep=reset_every_fill,
        )

    assert exc_info.value.code == "email_form_reset"
    assert [event for event in page.events if event[0] == "fill"] == [
        ("fill", "person@example.com"),
        ("fill", "person@example.com"),
    ]
    assert [event for event in page.events if event[0] == "click"] == []
    assert all(
        event == ("sleep", 0.5)
        for event in page.events
        if event[0] == "sleep"
    )
    assert page.clicked is False
    assert (tmp_path / "latest.png").read_bytes() == b"fake-png"


def test_post_submit_refresh_is_refilled_and_submitted_once_more(
    tmp_path: Path,
) -> None:
    page = FakePage(next_step="post_submit_reset_then_verification")
    generated_delays = iter([3.25, 4.25, 4.75, 3.5])
    submitted_times = iter(
        [
            FIXED_SUBMITTED_AT,
            datetime(2026, 8, 9, 1, 30, 5, tzinfo=timezone.utc),
        ]
    )
    sleep_calls: list[float] = []
    submission_calls: list[datetime] = []

    def sequential_uniform(_minimum: float, _maximum: float) -> float:
        return next(generated_delays)

    async def record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def record_submission_boundary() -> datetime:
        submitted_at = next(submitted_times)
        submission_calls.append(submitted_at)
        return submitted_at

    result = run_probe(
        page,
        tmp_path / "latest.png",
        random_uniform=sequential_uniform,
        delay_sleep=record_sleep,
        utc_now=record_submission_boundary,
    )

    assert result.next_step == "verification"
    assert result.submitted_at_utc == submission_calls[-1]
    assert result.pre_continue_delay_ms == 5_000
    assert result.email_pre_fill_delays_ms == (5_000, 5_000)
    assert result.email_fill_attempts == 2
    assert result.email_form_reset_count == 0
    assert result.email_continue_attempts == 2
    assert result.email_post_submit_reset_count == 1
    assert sleep_calls == [0.5] * 40
    assert [event for event in page.events if event[0] == "fill"] == [
        ("fill", "person@example.com"),
        ("fill", "person@example.com"),
    ]
    assert [event for event in page.events if event[0] == "click"] == [
        ("click", None),
        ("click", None),
    ]
    assert page.continue_click_count == 2


def test_stalled_email_submission_retries_once_like_reference_flow(
    tmp_path: Path,
) -> None:
    page = FakePage(next_step="stalled_then_password")

    result = run_probe(
        page,
        tmp_path / "latest.png",
        next_step_timeout_ms=1,
        poll_interval_seconds=0,
    )

    assert result.next_step == "password"
    assert result.email_continue_attempts == 2
    assert result.email_continue_recovery_state == "stalled_form_reload_retry"
    assert page.continue_click_count == 2
    assert [call[0] for call in page.goto_calls].count(CHATGPT_LOGIN_URL) == 2


def test_transient_empty_email_observation_does_not_resubmit(tmp_path: Path) -> None:
    page = FakePage(next_step="post_submit_transient_empty_then_password")

    result = run_probe(page, tmp_path / "latest.png")

    assert result.next_step == "password"
    assert result.email_fill_attempts == 1
    assert result.email_continue_attempts == 1
    assert result.email_post_submit_reset_count == 0
    assert page.continue_click_count == 1
    assert [event for event in page.events if event[0] == "fill"] == [
        ("fill", "person@example.com")
    ]


def test_next_step_arriving_during_refill_delay_does_not_resubmit(
    tmp_path: Path,
) -> None:
    page = FakePage(next_step="post_submit_reset_twice")
    transitioned = False

    async def transition_during_refill_delay(_seconds: float) -> None:
        nonlocal transitioned
        if (
            page.continue_click_count == 1
            and page.filled_email == ""
            and not transitioned
        ):
            page.email_visible = False
            page.verification_visible = True
            page.body = "Check your email Verification code"
            transitioned = True

    result = run_probe(
        page,
        tmp_path / "latest.png",
        delay_sleep=transition_during_refill_delay,
    )

    assert result.next_step == "verification"
    assert result.email_fill_attempts == 1
    assert result.email_continue_attempts == 1
    assert result.email_post_submit_reset_count == 1
    assert page.continue_click_count == 1
    assert len([event for event in page.events if event[0] == "fill"]) == 1


def test_second_post_submit_reset_stops_without_third_attempt(tmp_path: Path) -> None:
    page = FakePage(next_step="post_submit_reset_twice")

    with pytest.raises(EmailStepError) as exc_info:
        run_probe(page, tmp_path / "latest.png")

    assert exc_info.value.code == "email_post_submit_reset"
    assert len([event for event in page.events if event[0] == "fill"]) == 2
    assert len([event for event in page.events if event[0] == "click"]) == 2
    assert page.continue_click_count == 2
    assert (tmp_path / "latest.png").read_bytes() == b"fake-png"


def test_post_submit_reset_stops_when_pre_click_reset_used_refill_budget(
    tmp_path: Path,
) -> None:
    page = FakePage(next_step="post_submit_reset_twice")
    first_fill_cleared = False

    async def reset_first_fill_before_click(seconds: float) -> None:
        nonlocal first_fill_cleared
        _ = seconds
        if page.filled_email and not first_fill_cleared:
            page.filled_email = ""
            first_fill_cleared = True

    with pytest.raises(EmailStepError) as exc_info:
        run_probe(
            page,
            tmp_path / "latest.png",
            delay_sleep=reset_first_fill_before_click,
        )

    assert exc_info.value.code == "email_post_submit_reset"
    assert len([event for event in page.events if event[0] == "fill"]) == 2
    assert len([event for event in page.events if event[0] == "click"]) == 1
    assert page.continue_click_count == 1


@pytest.mark.parametrize(
    "options",
    [
        {"pre_fill_delay_min_seconds": -1},
        {
            "pre_fill_delay_min_seconds": 2,
            "pre_fill_delay_max_seconds": 1,
        },
    ],
)
def test_invalid_pre_fill_delay_range_is_rejected(
    options: dict[str, float],
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="邮箱输入前随机等待范围无效"):
        CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            tmp_path / "latest.png",
            **options,
        )


@pytest.mark.parametrize(
    "options",
    [
        {"email_pre_continue_delay_min_seconds": -1},
        {
            "email_pre_continue_delay_min_seconds": 5,
            "email_pre_continue_delay_max_seconds": 3,
        },
    ],
)
def test_invalid_email_pre_continue_delay_range_is_rejected(
    options: dict[str, float],
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="邮箱提交前随机等待范围无效"):
        CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            tmp_path / "latest.png",
            **options,
        )


def test_invalid_email_form_stability_window_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="邮箱表单稳定窗口"):
        CdpBrowserAutomation(
            "ws://127.0.0.1:50001/devtools/browser/test",
            tmp_path / "latest.png",
            email_form_stability_seconds=0,
        )


@pytest.mark.parametrize(
    ("page_factory", "code"),
    [
        (lambda: FakePage(email_visible=False), "email_form_not_stable"),
        (lambda: FakePage(continue_visible=False), "email_form_not_stable"),
        (
            lambda: FakePage(fill_error=RuntimeError("PRIVATE_VALUE")),
            "email_fill_failed",
        ),
        (
            lambda: FakePage(input_value_override="different@example.com"),
            "email_value_mismatch",
        ),
        (
            lambda: FakePage(click_error=RuntimeError("PRIVATE_VALUE")),
            "continue_click_failed",
        ),
        (lambda: FakePage(next_step="rejected"), "email_rejected"),
    ],
)
def test_email_step_errors_are_stable_and_do_not_expose_raw_errors(
    tmp_path: Path,
    page_factory,
    code: str,
) -> None:
    with pytest.raises(EmailStepError) as exc_info:
        run_probe(page_factory(), tmp_path / "latest.png")

    assert exc_info.value.code == code
    assert "PRIVATE_VALUE" not in exc_info.value.message


@pytest.mark.parametrize(
    ("next_step", "code"),
    [
        ("timeout", "email_continue_timeout"),
        ("unknown", "email_next_step_unknown"),
    ],
)
def test_email_continue_timeout_and_unknown_page_are_distinct(
    tmp_path: Path,
    next_step: str,
    code: str,
) -> None:
    with pytest.raises(EmailStepError) as exc_info:
        run_probe(
            FakePage(next_step=next_step),
            tmp_path / "latest.png",
            next_step_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == code


@pytest.mark.parametrize("next_step", ["loading", "blank_alert", "sr_only_rejected"])
def test_loading_and_accessibility_alerts_are_not_email_rejections(
    tmp_path: Path,
    next_step: str,
) -> None:
    with pytest.raises(EmailStepError) as exc_info:
        run_probe(
            FakePage(next_step=next_step),
            tmp_path / "latest.png",
            next_step_timeout_ms=1,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == "email_continue_timeout"


def test_email_continue_stops_on_challenge(tmp_path: Path) -> None:
    page = FakePage(next_step="challenge")

    with pytest.raises(TargetChallengeError):
        run_probe(page, tmp_path / "latest.png")

    assert page.continue_click_count == 1
    assert len([event for event in page.events if event[0] == "fill"]) == 1


def test_login_cloudflare_structure_waits_then_uses_email_form(tmp_path: Path) -> None:
    page = FakePage(login_challenge_checks_before_release=3)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = run_probe(
        page,
        tmp_path / "latest.png",
        delay_sleep=record_sleep,
        login_challenge_wait_seconds=2,
        challenge_poll_interval_seconds=0.5,
        email_form_stability_seconds=0.5,
        pre_fill_delay_min_seconds=0,
        pre_fill_delay_max_seconds=0,
        email_pre_continue_delay_min_seconds=0,
        email_pre_continue_delay_max_seconds=0,
    )

    assert result.next_step == "password"
    assert page.login_challenge_checks > 0
    assert result.login_challenge_observed is True
    assert page.continue_click_count == 1
    assert [event for event in page.events if event[0] == "fill"] == [
        ("fill", "person@example.com")
    ]
    assert sleeps[0] == 0.5


def test_login_cloudflare_structure_times_out_without_interaction(
    tmp_path: Path,
) -> None:
    page = FakePage(login_challenge_checks_before_release=-1)

    with pytest.raises(TargetChallengeError) as exc_info:
        run_probe(
            page,
            tmp_path / "latest.png",
        )

    assert exc_info.value.stage == "login"
    assert exc_info.value.wait_ms == 60_000
    assert exc_info.value.login_challenge_observed is True
    assert page.continue_click_count == 0
    assert [event for event in page.events if event[0] == "fill"] == []
    assert page.screenshot_calls >= 1


@pytest.mark.parametrize(
    ("unstable_state", "challenge_observed"),
    [
        ("challenge", True),
        ("input_detached", False),
        ("button_disabled", False),
    ],
)
def test_email_form_stability_window_restarts_when_state_changes(
    tmp_path: Path,
    unstable_state: str,
    challenge_observed: bool,
) -> None:
    page = FakePage()
    sleep_count = 0

    async def change_state_during_ready_gate(seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        page.events.append(("sleep", seconds))
        if sleep_count == 3:
            if unstable_state == "challenge":
                page.body = "Verify you are human"
            elif unstable_state == "input_detached":
                page.email_visible = False
            else:
                page.continue_enabled = False
        elif sleep_count == 4:
            page.body = "Log in or sign up Email address Continue"
            page.email_visible = True
            page.continue_enabled = True

    result = run_probe(
        page,
        tmp_path / "latest.png",
        delay_sleep=change_state_during_ready_gate,
    )

    fill_index = page.events.index(("fill", "person@example.com"))
    assert sum(
        event[1]
        for event in page.events[:fill_index]
        if event[0] == "sleep"
    ) == 7.0
    assert result.email_form_ready_wait_ms == 7_000
    assert result.email_form_stability_reset_count == 1
    assert result.login_challenge_observed is challenge_observed
    assert page.continue_click_count == 1


def test_email_form_unstable_before_continue_times_out_without_click(
    tmp_path: Path,
) -> None:
    page = FakePage()

    async def disable_after_fill(_seconds: float) -> None:
        if page.filled_email:
            page.continue_enabled = False

    with pytest.raises(EmailStepError) as exc_info:
        run_probe(
            page,
            tmp_path / "latest.png",
            delay_sleep=disable_after_fill,
            login_challenge_wait_seconds=2,
            pre_fill_delay_min_seconds=0,
            pre_fill_delay_max_seconds=0,
            email_pre_continue_delay_min_seconds=0,
            email_pre_continue_delay_max_seconds=0,
            email_form_stability_seconds=0.5,
        )

    assert exc_info.value.code == "email_form_unstable_before_continue"
    assert exc_info.value.email_pre_continue_stable_waits_ms == (2_000,)
    assert exc_info.value.email_form_stability_reset_count == 1
    assert page.continue_click_count == 0


def test_email_rejection_does_not_refill_or_resubmit(tmp_path: Path) -> None:
    page = FakePage(next_step="rejected")

    with pytest.raises(EmailStepError) as exc_info:
        run_probe(page, tmp_path / "latest.png")

    assert exc_info.value.code == "email_rejected"
    assert page.continue_click_count == 1
    assert len([event for event in page.events if event[0] == "fill"]) == 1


@pytest.mark.parametrize(
    ("page_factory", "stage", "code", "timeout_ms", "net_error"),
    [
        (
            lambda: FakePage(goto_errors={"ip": TimeoutError("PRIVATE_VALUE")}),
            "ip_navigation",
            "ip_navigation_timeout",
            90_000,
            None,
        ),
        (
            lambda: FakePage(
                goto_errors={
                    "ip": ConnectionError(
                        "PRIVATE_VALUE net::ERR_PROXY_CONNECTION_FAILED https://secret.invalid"
                    )
                }
            ),
            "ip_navigation",
            "ip_navigation_failed",
            90_000,
            "net::ERR_PROXY_CONNECTION_FAILED",
        ),
        (
            lambda: FakePage(body_errors={"ip": TimeoutError("PRIVATE_VALUE")}),
            "ip_response_read",
            "ip_response_read_failed",
            5_000,
            None,
        ),
        (
            lambda: FakePage(ip_body="PRIVATE_VALUE invalid-json"),
            "ip_response_parse",
            "ip_response_invalid",
            None,
            None,
        ),
        (
            lambda: FakePage(goto_errors={"login": TimeoutError("PRIVATE_VALUE")}),
            "login_navigation",
            "login_navigation_timeout",
            90_000,
            None,
        ),
        (
            lambda: FakePage(
                goto_errors={
                    "login": ConnectionError(
                        "PRIVATE_VALUE net::ERR_CONNECTION_RESET https://secret.invalid"
                    )
                }
            ),
            "login_navigation",
            "login_navigation_failed",
            90_000,
            "net::ERR_CONNECTION_RESET",
        ),
        (
            lambda: FakePage(body_errors={"login": TimeoutError("PRIVATE_VALUE")}),
            "login_content_read",
            "login_content_read_failed",
            10_000,
            None,
        ),
    ],
)
def test_proxy_navigation_errors_are_structured_and_redacted(
    tmp_path: Path,
    page_factory,
    stage: str,
    code: str,
    timeout_ms: int | None,
    net_error: str | None,
) -> None:
    with pytest.raises(ProxyNavigationError) as exc_info:
        run_probe(page_factory(), tmp_path / "latest.png")

    error = exc_info.value
    record = error.as_attempt_error(attempt=1, proxy_id="proxy-id")
    assert record["attempt"] == 1
    assert record["proxyId"] == "proxy-id"
    assert record["stage"] == stage
    assert record["code"] == code
    assert record["message"]
    assert record["exceptionType"]
    assert record["netError"] == net_error
    assert record["elapsedMs"] >= 0
    assert record["timeoutMs"] == timeout_ms
    serialized = json.dumps(record)
    assert "PRIVATE_VALUE" not in serialized
    assert "secret.invalid" not in serialized


def test_redaction_helpers_cover_ipv4_ipv6_url_and_challenges() -> None:
    assert mask_ip("198.51.100.7") == "198.51.*.*"
    assert mask_ip("2001:db8::1") == "2001:0db8:*"
    assert sanitize_url("https://example.com/path?token=secret#fragment") == "https://example.com/path"
    assert contains_challenge("Verify you are human") is True
    assert contains_challenge("Verifying...") is True
    assert contains_challenge("Just a moment…") is True
    assert contains_challenge("人間であることを確認") is True
    assert contains_challenge("ブラウザを確認しています") is True
    assert contains_challenge("Normal log in or sign up page") is False


def _test_jwt(expires_at: datetime) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'RS256', 'typ': 'JWT'})}.{encode({'exp': int(expires_at.timestamp())})}.TEST_SIGNATURE"


def test_extract_access_token_uses_session_json_then_restores_home(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    token = _test_jwt(datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc))
    session_token = "TEST_SESSION_TOKEN_MUST_NOT_LEAK"
    page = SessionFakePage(
        json.dumps(
            {
                "accessToken": token,
                "sessionToken": session_token,
                "user": {"email": "private@example.test"},
            }
        ).encode("utf-8")
    )
    automation = CdpBrowserAutomation(
        "ws://private.invalid",
        tmp_path / "latest.png",
        utc_now=lambda: now,
    )
    automation._active_page = page

    result = asyncio.run(automation.extract_chatgpt_access_token())

    assert result.access_token == token
    assert result.expires_at_utc == datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
    assert result.extracted_at_utc == now
    assert result.final_url == CHATGPT_HOME_URL
    assert result.homepage_restored is True
    assert page.goto_calls == [
        (CHATGPT_SESSION_URL, "domcontentloaded", 45_000),
        (CHATGPT_HOME_URL, "domcontentloaded", 45_000),
    ]
    assert page.screenshot_urls == [CHATGPT_HOME_URL]
    assert token not in repr(result)
    assert session_token not in repr(result)


def test_playwright_plan_check_injects_bearer_and_restores_home(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-id", "plan_type": "free"},
                    "entitlement": {
                        "subscription_plan": "chatgptfreeplan",
                        "has_active_subscription": False,
                    },
                    "eligible_promo_campaigns": {"plus": {"id": "trial"}},
                }
            }
        }
    ).encode()
    page = PlanFakePage(payload)
    automation = CdpBrowserAutomation(
        "ws://private.invalid",
        tmp_path / "latest.png",
        utc_now=lambda: datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc),
    )
    automation._active_page = page

    result = asyncio.run(
        automation.extract_chatgpt_account_plan("TEST_AT_DO_NOT_LOG")
    )

    assert result.plus_trial_eligible is True
    assert page.plan_route.continued_headers is not None
    assert page.plan_route.continued_headers["authorization"] == (
        "Bearer TEST_AT_DO_NOT_LOG"
    )
    assert page.goto_calls == [
        (CHATGPT_PLAN_URL, "domcontentloaded", 45_000),
        (CHATGPT_HOME_URL, "domcontentloaded", 45_000),
    ]
    assert page.unroute_calls == ["**/backend-api/accounts/check/v4-2023-04-27*"]
    assert page.url == CHATGPT_HOME_URL
    assert "TEST_AT_DO_NOT_LOG" not in repr(result)


def test_playwright_plan_401_is_redacted_and_restores_home(tmp_path: Path) -> None:
    page = PlanFakePage(b'{"private":"PRIVATE_RESPONSE_BODY"}', status=401)
    automation = CdpBrowserAutomation(
        "ws://private.invalid",
        tmp_path / "latest.png",
    )
    automation._active_page = page

    with pytest.raises(PlanCheckError) as exc_info:
        asyncio.run(automation.extract_chatgpt_account_plan("TEST_AT_DO_NOT_LOG"))

    assert exc_info.value.code == "access_token_unauthorized"
    assert exc_info.value.http_status == 401
    assert page.url == CHATGPT_HOME_URL
    assert "TEST_AT_DO_NOT_LOG" not in repr(exc_info.value)
    assert "PRIVATE_RESPONSE_BODY" not in repr(exc_info.value)


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [("oaics_fixture", "oaics"), ("cs_fixture", "cs")],
)
def test_registration_checkout_type_uses_current_browser_session(
    tmp_path: Path, session_id: str, expected: str
) -> None:
    page = CheckoutTypeFakePage({"checkout_session_id": session_id})
    automation = CdpBrowserAutomation(
        "ws://private.invalid",
        tmp_path / "latest.png",
    )
    automation._active_page = page

    result = asyncio.run(
        automation.extract_chatgpt_checkout_type("TEST_AT_DO_NOT_LOG", country="JP")
    )

    assert result.checkout_type == expected
    assert page.checkout_arguments is not None
    assert page.checkout_arguments["country"] == "JP"
    assert page.checkout_arguments["token"] == "TEST_AT_DO_NOT_LOG"
    assert page.url == CHATGPT_HOME_URL
    assert "TEST_AT_DO_NOT_LOG" not in repr(result)


@pytest.mark.parametrize(
    ("page_factory", "expected_code"),
    [
        (
            lambda now: SessionFakePage(json.dumps({"sessionToken": "PRIVATE"}).encode()),
            "access_token_missing",
        ),
        (
            lambda now: SessionFakePage(
                json.dumps({"accessToken": "not-a-jwt"}).encode()
            ),
            "access_token_invalid",
        ),
        (
            lambda now: SessionFakePage(
                json.dumps(
                    {"accessToken": _test_jwt(now.replace(minute=0, second=0))}
                ).encode()
            ),
            "access_token_expired",
        ),
        (
            lambda now: SessionFakePage(b"{}", content_type="text/html"),
            "session_json_invalid",
        ),
        (
            lambda now: SessionFakePage(b"{}", status=503),
            "session_http_failed",
        ),
        (
            lambda now: SessionFakePage(b"x" * 65_537),
            "session_response_too_large",
        ),
        (
            lambda now: SessionFakePage(
                b"{}", session_final_url="https://evil.example/session"
            ),
            "session_response_untrusted",
        ),
    ],
)
def test_extract_access_token_failures_are_classified_and_never_screenshot_session(
    tmp_path: Path,
    page_factory,
    expected_code: str,
) -> None:
    now = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)
    page = page_factory(now)
    automation = CdpBrowserAutomation(
        "ws://private.invalid",
        tmp_path / "latest.png",
        utc_now=lambda: now,
    )
    automation._active_page = page

    with pytest.raises(AccessTokenExtractionError) as exc_info:
        asyncio.run(automation.extract_chatgpt_access_token())

    assert exc_info.value.code == expected_code
    assert exc_info.value.homepage_restored is True
    assert page.screenshot_urls == [CHATGPT_HOME_URL]
    serialized = json.dumps(
        {
            "stage": exc_info.value.stage,
            "code": exc_info.value.code,
            "message": exc_info.value.message,
        }
    )
    assert "PRIVATE" not in serialized
