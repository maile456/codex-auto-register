from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manager.crypto import DpapiCredentialCipher  # noqa: E402
from manager.storage import AccountStore  # noqa: E402


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}{fragment}"


async def main() -> int:
    if len(sys.argv) != 2:
        print("USAGE_ERROR")
        return 2
    requested = sys.argv[1].strip()
    store = AccountStore(ROOT / "data" / "mailcom.db", DpapiCredentialCipher())
    credentials = store.get_credentials_by_email(requested)
    if credentials is None:
        print("ACCOUNT_NOT_FOUND")
        return 3
    email, password = credentials
    print(f"ACCOUNT={mask_email(email)}")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="en-US",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 1000},
        )
        page = await context.new_page()
        network_events: list[dict[str, object]] = []

        def record_request(request) -> None:
            parsed = urlsplit(request.url)
            if parsed.netloc.endswith("mail.com") and request.resource_type in {
                "document",
                "xhr",
                "fetch",
            }:
                network_events.append(
                    {
                        "kind": "request",
                        "method": request.method,
                        "type": request.resource_type,
                        "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                    }
                )

        def record_response(response) -> None:
            parsed = urlsplit(response.url)
            request = response.request
            if parsed.netloc.endswith("mail.com") and request.resource_type in {
                "document",
                "xhr",
                "fetch",
            }:
                network_events.append(
                    {
                        "kind": "response",
                        "status": response.status,
                        "method": request.method,
                        "type": request.resource_type,
                        "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                    }
                )

        page.on("request", record_request)
        page.on("response", record_response)
        try:
            await page.goto(
                "https://www.mail.com/homepage.html#navlogin",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            await page.wait_for_timeout(3_000)
            descriptors = await page.locator("input").evaluate_all(
                """elements => elements.map(item => ({
                    type: item.type,
                    name: item.name,
                    id: item.id,
                    placeholder: item.placeholder,
                    aria: item.getAttribute('aria-label')
                }))"""
            )
            print(f"PRE_URL={page.url}")
            print(f"INPUTS={descriptors}")
            email_input = page.locator(
                'input[type="email"], input[name="username"], '
                'input[name="email"], input[autocomplete="username"]'
            ).first
            password_input = page.locator(
                'input[type="password"], input[autocomplete="current-password"]'
            ).first
            if not await email_input.is_visible():
                await page.locator('a[href*="navlogin"]').first.evaluate(
                    "element => element.click()"
                )
                await page.wait_for_timeout(500)
            await email_input.fill(email)
            await password_input.fill(password)
            await page.locator(
                'form:has(#login-email) button[type="submit"], '
                'form:has(#login-email) input[type="submit"]'
            ).first.click()
            await page.wait_for_timeout(8_000)
            parsed_page_url = urlsplit(page.url)
            print(f"URL={parsed_page_url.scheme}://{parsed_page_url.netloc}{parsed_page_url.path}")
            print(f"TITLE={await page.title()}")
            body = re.sub(r"\s+", " ", (await page.locator("body").inner_text())[:8000])
            body = body.replace(email, mask_email(email)).replace(password, "[REDACTED]")
            print(f"BODY={body[:3000]}")
            for index, frame in enumerate(page.frames):
                parsed_frame_url = urlsplit(frame.url)
                safe_url = (
                    f"{parsed_frame_url.scheme}://{parsed_frame_url.netloc}"
                    f"{parsed_frame_url.path}"
                )
                try:
                    frame_text = re.sub(
                        r"\s+", " ", (await frame.locator("body").inner_text())[:5000]
                    )
                except Exception:
                    frame_text = ""
                frame_text = frame_text.replace(email, mask_email(email)).replace(
                    password, "[REDACTED]"
                )
                print(f"FRAME_{index}={safe_url} :: {frame_text[:1500]}")
                if "webmailer.mail.com" in parsed_frame_url.netloc:
                    controls = await frame.locator(
                        "button, a, [role=button], [role=menuitem]"
                    ).evaluate_all(
                        """elements => elements.slice(0, 300).map(item => ({
                            tag: item.tagName,
                            text: (item.innerText || '').trim().slice(0, 120),
                            title: item.getAttribute('title'),
                            aria: item.getAttribute('aria-label'),
                            testid: item.getAttribute('data-testid')
                        }))"""
                    )
                    print(f"WEBMAIL_CONTROLS={controls}")
                    await frame.get_by_title(
                        "Settings for your mail.com account"
                    ).click()
                    await page.wait_for_timeout(6_000)
                    screenshot_path = ROOT.parent / "artifacts" / "mailcom-settings-pilot.png"
                    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    print(f"SCREENSHOT={screenshot_path}")
                    settings_text = re.sub(
                        r"\s+", " ", (await frame.locator("body").inner_text())[:8000]
                    )
                    settings_text = settings_text.replace(
                        email, mask_email(email)
                    ).replace(password, "[REDACTED]")
                    print(f"SETTINGS_BODY={settings_text[:3500]}")
                    settings_controls = await frame.locator(
                        "button, a, [role=button], [role=menuitem]"
                    ).evaluate_all(
                        """elements => elements.slice(0, 400).map(item => ({
                            tag: item.tagName,
                            text: (item.innerText || '').trim().slice(0, 120),
                            title: item.getAttribute('title'),
                            aria: item.getAttribute('aria-label')
                        }))"""
                    )
                    print(f"SETTINGS_CONTROLS={settings_controls}")
                    for page_index, candidate_page in enumerate(context.pages):
                        candidate_url = urlsplit(candidate_page.url)
                        print(
                            f"POST_SETTINGS_PAGE_{page_index}="
                            f"{candidate_url.scheme}://{candidate_url.netloc}"
                            f"{candidate_url.path}"
                        )
                        for frame_index, candidate_frame in enumerate(
                            candidate_page.frames
                        ):
                            candidate_frame_url = urlsplit(candidate_frame.url)
                            print(
                                f"POST_SETTINGS_FRAME_{page_index}_{frame_index}="
                                f"{candidate_frame_url.scheme}://"
                                f"{candidate_frame_url.netloc}"
                                f"{candidate_frame_url.path}"
                            )
                            if "mailset-root.mail.com" in candidate_frame_url.netloc:
                                mailset_text = re.sub(
                                    r"\s+",
                                    " ",
                                    (
                                        await candidate_frame.locator("body").inner_text()
                                    )[:12000],
                                )
                                mailset_text = mailset_text.replace(
                                    email, mask_email(email)
                                ).replace(password, "[REDACTED]")
                                print(f"MAILSET_BODY={mailset_text[:6000]}")
                                mailset_controls = await candidate_frame.locator(
                                    "button, a, input, select, [role=button], "
                                    "[role=menuitem], [role=tab]"
                                ).evaluate_all(
                                    """elements => elements.slice(0, 500).map(item => ({
                                        tag: item.tagName,
                                        type: item.type,
                                        text: (item.innerText || '').trim().slice(0, 160),
                                        title: item.getAttribute('title'),
                                        aria: item.getAttribute('aria-label'),
                                        name: item.getAttribute('name'),
                                        placeholder: item.getAttribute('placeholder')
                                    }))"""
                                )
                                print(f"MAILSET_CONTROLS={mailset_controls}")
                                sender_addresses = candidate_frame.get_by_role(
                                    "button", name="Sender Addresses", exact=True
                                )
                                if await sender_addresses.count():
                                    sender_button = await sender_addresses.first.evaluate(
                                        """element => ({
                                            html: element.outerHTML,
                                            parent: element.parentElement?.outerHTML?.slice(0, 3000),
                                            visible: Boolean(element.offsetWidth || element.offsetHeight),
                                            disabled: Boolean(element.disabled)
                                        })"""
                                    )
                                    print(
                                        "SENDER_BUTTON="
                                        + json.dumps(sender_button, ensure_ascii=False)
                                    )
                                    network_events.clear()
                                    await sender_addresses.click(force=True)
                                    await page.wait_for_timeout(10_000)
                                    after_path = await candidate_frame.evaluate(
                                        """() => ({
                                            href: location.origin + location.pathname + location.hash,
                                            title: document.title,
                                            history: history.state
                                        })"""
                                    )
                                    print(
                                        "SENDER_ROUTE="
                                        + json.dumps(after_path, ensure_ascii=False)
                                    )
                                    screenshot_after = (
                                        ROOT.parent
                                        / "artifacts"
                                        / "mailcom-sender-addresses-pilot.png"
                                    )
                                    await page.screenshot(
                                        path=str(screenshot_after), full_page=True
                                    )
                                    print(f"SENDER_SCREENSHOT={screenshot_after}")
                                    sender_text = re.sub(
                                        r"\s+",
                                        " ",
                                        (
                                            await candidate_frame.locator(
                                                "body"
                                            ).inner_text()
                                        )[:12000],
                                    )
                                    sender_text = sender_text.replace(
                                        email, mask_email(email)
                                    ).replace(password, "[REDACTED]")
                                    print(
                                        f"SENDER_ADDRESSES_BODY={sender_text[:6000]}"
                                    )
                                    sender_controls = await candidate_frame.locator(
                                        "button, a, input, select, option, "
                                        "[role=button], [role=menuitem], [role=tab]"
                                    ).evaluate_all(
                                        """elements => elements.slice(0, 600).map(item => ({
                                            tag: item.tagName,
                                            type: item.type,
                                            text: (item.innerText || '').trim().slice(0, 180),
                                            title: item.getAttribute('title'),
                                            aria: item.getAttribute('aria-label'),
                                            name: item.getAttribute('name'),
                                            placeholder: item.getAttribute('placeholder')
                                        }))"""
                                    )
                                    print(
                                        f"SENDER_ADDRESSES_CONTROLS={sender_controls}"
                                    )
                                    diagnostic_controls = await candidate_frame.locator(
                                        "input, select, textarea, button"
                                    ).evaluate_all(
                                        """elements => elements.map(item => ({
                                            tag: item.tagName,
                                            type: item.type || '',
                                            name: item.getAttribute('name'),
                                            id: item.id || null,
                                            text: (item.innerText || '').trim().slice(0, 120),
                                            placeholder: item.getAttribute('placeholder'),
                                            aria: item.getAttribute('aria-label'),
                                            visible: Boolean(item.offsetWidth || item.offsetHeight),
                                            disabled: Boolean(item.disabled),
                                            formAction: item.formAction ? new URL(item.formAction).pathname : null
                                        })).filter(item => item.visible)"""
                                    )
                                    print(
                                        "SENDER_VISIBLE_CONTROLS="
                                        + json.dumps(
                                            diagnostic_controls, ensure_ascii=False
                                        )
                                    )
                                    alias_input = candidate_frame.locator(
                                        'input[placeholder="e.g. your-name"]'
                                    ).first
                                    if await alias_input.count():
                                        await alias_input.scroll_into_view_if_needed()
                                        alias_section = await alias_input.evaluate(
                                            """element => {
                                                let node = element;
                                                for (let i = 0; i < 5 && node.parentElement; i += 1) {
                                                    node = node.parentElement;
                                                }
                                                return {
                                                    text: (node.innerText || '').replace(/\s+/g, ' ').slice(0, 2000),
                                                    html: node.outerHTML.slice(0, 5000)
                                                };
                                            }"""
                                        )
                                        print(
                                            "ALIAS_SECTION="
                                            + json.dumps(alias_section, ensure_ascii=False)
                                        )
                                        alias_screenshot = (
                                            ROOT.parent
                                            / "artifacts"
                                            / "mailcom-alias-form-pilot.png"
                                        )
                                        await page.screenshot(
                                            path=str(alias_screenshot), full_page=False
                                        )
                                        print(f"ALIAS_SCREENSHOT={alias_screenshot}")
                                    print(
                                        "SENDER_NETWORK="
                                        + json.dumps(network_events, ensure_ascii=False)
                                    )
        finally:
            await context.close()
            await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
