from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.browser_automation import (
    AccessTokenExtractionResult,
    CdpBrowserAutomation,
)
from backend.totp import generate_totp


SECRET = "JBSWY3DPEHPK3PXP"


class VisibleLocator:
    async def count(self) -> int:
        return 1

    @property
    def first(self):
        return self

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self, timeout: int = 0) -> str:
        _ = timeout
        return "Check your email"


class FakeContext:
    async def cookies(self, url: str):
        assert url == "https://chatgpt.com"
        return [{"name": "oai-did", "value": "device-fixture"}]


class MfaPage:
    def __init__(self) -> None:
        self.url = "https://chatgpt.com/"
        self.context = FakeContext()
        self.activation_body = None

    def locator(self, _selector: str) -> VisibleLocator:
        return VisibleLocator()

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def evaluate(self, script: str, arguments=None):
        arguments = arguments or {}
        if "api/auth/csrf" in script:
            return {
                "ok": True,
                "stage": "signin",
                "status": 200,
                "url": "https://auth.openai.com/email-verification",
            }
        url = arguments.get("url", "")
        if url.endswith("/mfa/enroll"):
            return {
                "ok": True,
                "status": 200,
                "data": {"secret": SECRET, "session_id": "session-fixture"},
            }
        if url.endswith("/activate_enrollment"):
            self.activation_body = arguments["body"]
            return {"ok": True, "status": 200, "data": {"success": True}}
        if url.endswith("/models"):
            return {"ok": True, "status": 200, "data": {}}
        raise AssertionError(f"unexpected evaluate call: {url}")


class MfaAutomation(CdpBrowserAutomation):
    def __init__(self, page: MfaPage, screenshot: Path) -> None:
        super().__init__("ws://fixture", screenshot, delay_sleep=lambda _seconds: asyncio.sleep(0))
        self.test_page = page
        self.submitted_code = ""

    async def _page(self):
        return self.test_page

    async def _is_confirmed_chatgpt_home(self, _page) -> bool:
        return True

    async def submit_verification_code_and_continue(self, code: str):
        self.submitted_code = code
        return object()

    async def extract_chatgpt_access_token(self) -> AccessTokenExtractionResult:
        now = datetime.now(timezone.utc)
        self.test_page.url = "https://chatgpt.com/"
        return AccessTokenExtractionResult(
            access_token="TOKEN_FIXTURE",
            expires_at_utc=now + timedelta(hours=1),
            extracted_at_utc=now,
            final_url=self.test_page.url,
            homepage_restored=True,
        )


def test_totp_enrollment_reauth_enrolls_and_activates(tmp_path: Path) -> None:
    page = MfaPage()
    automation = MfaAutomation(page, tmp_path / "latest.png")

    challenge = asyncio.run(automation.begin_totp_enrollment("user@example.com"))
    assert challenge.final_url == "https://auth.openai.com/email-verification"

    result = asyncio.run(automation.complete_totp_enrollment("123456"))
    assert automation.submitted_code == "123456"
    assert result.secret == SECRET
    assert page.activation_body["session_id"] == "session-fixture"
    assert page.activation_body["factor_type"] == "totp"
    assert page.activation_body["code"] == generate_totp(SECRET)
