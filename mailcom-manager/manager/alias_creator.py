from __future__ import annotations

import asyncio
import inspect
import random
import re
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Browser, Frame, Page, async_playwright


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AliasCreationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AliasCreationResult:
    remote_before: tuple[str, ...]
    created: tuple[str, ...]
    remote_after: tuple[str, ...]


def _candidate_local(email: str) -> str:
    local = email.split("@", 1)[0].casefold()
    clean = re.sub(r"[^a-z0-9]", "", local)[:22] or "mailbox"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{clean}{suffix}"


def _extract_emails(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        candidate = value.strip().casefold()
        if EMAIL_PATTERN.fullmatch(candidate):
            found.add(candidate)
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_extract_emails(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_extract_emails(item))
    return found


async def _login(page: Page, email: str, password: str) -> bool:
    await page.goto(
        "https://www.mail.com/homepage.html#navlogin",
        wait_until="domcontentloaded",
        timeout=45_000,
    )
    await page.wait_for_timeout(2_500)
    email_input = page.locator(
        'input[type="email"], input[name="username"], input[name="email"], '
        'input[autocomplete="username"]'
    ).first
    password_input = page.locator(
        'input[type="password"], input[autocomplete="current-password"]'
    ).first
    if not await email_input.is_visible():
        login_link = page.locator('a[href*="navlogin"]').first
        if await login_link.count():
            await login_link.evaluate("element => element.click()")
            await email_input.wait_for(state="visible", timeout=5_000)
    await email_input.fill(email)
    await password_input.fill(password)
    submit = page.locator(
        'form:has(#login-email) button[type="submit"], '
        'form:has(#login-email) input[type="submit"]'
    ).first
    await submit.evaluate("element => element.click()")
    try:
        await page.wait_for_url(
            re.compile(r"https://navigator-lxa\.mail\.com/"), timeout=30_000
        )
    except Exception:
        return False
    return urlsplit(page.url).netloc == "navigator-lxa.mail.com"


async def _wait_for_mailset(page: Page) -> Frame:
    deadline = asyncio.get_running_loop().time() + 45
    while asyncio.get_running_loop().time() < deadline:
        for frame in page.frames:
            if urlsplit(frame.url).netloc == "mailset-root.mail.com":
                button = frame.get_by_role(
                    "button", name="Sender Addresses", exact=True
                )
                if await button.count():
                    return frame
        await page.wait_for_timeout(500)
    raise AliasCreationError("MAILSET_FRAME_TIMEOUT")


async def _open_sender_addresses(page: Page) -> tuple[Frame, set[str]]:
    deadline = asyncio.get_running_loop().time() + 45
    settings_button = None
    while asyncio.get_running_loop().time() < deadline:
        for frame in page.frames:
            if urlsplit(frame.url).netloc == "webmailer.mail.com":
                locator = frame.get_by_title("Settings for your mail.com account")
                if await locator.count():
                    settings_button = locator
                    break
        if settings_button is not None:
            break
        await page.wait_for_timeout(500)
    if settings_button is None:
        raise AliasCreationError("WEBMAIL_SETTINGS_TIMEOUT")
    await settings_button.click()
    frame = await _wait_for_mailset(page)
    try:
        async with page.expect_response(
            lambda response: (
                response.request.method.upper() == "GET"
                and urlsplit(response.url).netloc == "settings-cats.mail.com"
                and urlsplit(response.url).path.endswith("/emailAddresses")
            ),
            timeout=30_000,
        ) as response_info:
            await frame.get_by_role(
                "button", name="Sender Addresses", exact=True
            ).click(force=True)
        response = await response_info.value
        addresses = _extract_emails(await response.json())
    except Exception as exc:
        raise AliasCreationError("REMOTE_ADDRESS_LIST_FAILED") from exc
    if not addresses:
        raise AliasCreationError("REMOTE_ADDRESS_LIST_EMPTY")
    alias_input = frame.locator('input[placeholder="e.g. your-name"]').first
    await alias_input.wait_for(state="visible", timeout=30_000)
    await alias_input.scroll_into_view_if_needed()
    return frame, addresses


async def _domain_control(frame: Frame, primary_domain: str):
    selects = frame.locator("select")
    best_select = None
    best_options: list[tuple[str, str]] = []
    for index in range(await selects.count()):
        rows = await selects.nth(index).locator("option").evaluate_all(
            """options => options.map(item => ({
                value: item.value,
                text: (item.textContent || '').trim()
            }))"""
        )
        options = [
            (
                str(item.get("value") or "").strip(),
                str(item.get("text") or "").strip().lstrip("@"),
            )
            for item in rows
            if re.fullmatch(
                r"[a-z0-9.-]+\.[a-z]{2,}",
                str(item.get("text") or "").strip().lstrip("@").casefold(),
            )
        ]
        if len(options) > len(best_options):
            best_select = selects.nth(index)
            best_options = options
    if best_select is None or not best_options:
        raise AliasCreationError("ALIAS_DOMAIN_SELECT_NOT_FOUND")
    available = {label.casefold(): (value, label) for value, label in best_options}
    preferred = [
        primary_domain.casefold(),
        "mail.com",
        "email.com",
        "iname.com",
        "workmail.com",
    ]
    value, label = next(
        (available[item] for item in preferred if item in available),
        best_options[0],
    )
    return best_select, value, label


async def _create_one(page: Page, frame: Frame, email: str) -> str:
    primary_domain = email.rsplit("@", 1)[1].casefold()
    domain_select, selected_value, selected_label = await _domain_control(
        frame, primary_domain
    )
    local = _candidate_local(email)
    alias = f"{local}@{selected_label.casefold()}"
    alias_input = frame.locator('input[placeholder="e.g. your-name"]').first
    await alias_input.fill(local)
    if selected_value:
        await domain_select.select_option(value=selected_value)
    else:
        await domain_select.select_option(label=selected_label)
    responses = []

    def record_response(response) -> None:
        parsed = urlsplit(response.url)
        if (
            parsed.netloc == "settings-cats.mail.com"
            and parsed.path.endswith("/emailAddresses")
            and response.request.method.upper() in {"POST", "PUT", "PATCH"}
        ):
            responses.append(response)

    page.on("response", record_response)
    try:
        await frame.get_by_role("button", name="Add address", exact=True).click()
        await page.wait_for_timeout(8_000)
    finally:
        page.remove_listener("response", record_response)
    status = responses[-1].status if responses else 0
    if status not in {200, 201, 202, 204}:
        raise AliasCreationError(f"ALIAS_CREATE_FAILED_STATUS_{status}")
    return alias


class MailComAliasCreator:
    def __init__(
        self,
        *,
        chrome_path: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        login_attempts: int = 3,
    ) -> None:
        self.chrome_path = chrome_path
        self.login_attempts = max(1, login_attempts)

    async def create_to_total(
        self,
        email: str,
        password: str,
        target_total: int = 10,
        on_created: Callable[[str], Awaitable[object] | object] | None = None,
    ) -> AliasCreationResult:
        target_total = min(10, max(1, target_total))
        last_error: BaseException | None = None
        async with async_playwright() as playwright:
            browser: Browser = await playwright.chromium.launch(
                headless=True,
                executable_path=self.chrome_path,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                for _attempt in range(self.login_attempts):
                    context = await browser.new_context(
                        locale="en-US",
                        timezone_id="Europe/London",
                        viewport={"width": 1440, "height": 1000},
                    )
                    try:
                        page = await context.new_page()
                        if not await _login(page, email, password):
                            raise AliasCreationError("LOGIN_FAILED")
                        frame, remote_before = await _open_sender_addresses(page)
                        created: list[str] = []
                        for _ in range(max(0, target_total - len(remote_before))):
                            alias = await _create_one(page, frame, email)
                            created.append(alias)
                            if on_created is not None:
                                callback_result = on_created(alias)
                                if inspect.isawaitable(callback_result):
                                    await callback_result
                            await page.wait_for_timeout(1_000)
                        remote_after = set(remote_before)
                        remote_after.update(created)
                        return AliasCreationResult(
                            remote_before=tuple(sorted(remote_before)),
                            created=tuple(created),
                            remote_after=tuple(sorted(remote_after)),
                        )
                    except Exception as exc:
                        last_error = exc
                    finally:
                        await context.close()
            finally:
                await browser.close()
        if isinstance(last_error, AliasCreationError):
            raise last_error
        raise AliasCreationError("ALIAS_CREATE_FAILED") from last_error
