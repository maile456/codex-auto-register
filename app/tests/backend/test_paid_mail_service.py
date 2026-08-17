from __future__ import annotations

import pytest

from backend import paid_mail_service


def test_confirmation_page_requires_account_and_payment_markers(monkeypatch) -> None:
    page = """
    <html><head><title>Latest email</title></head><body>
      <div class="subject">ChatGPT - New plan</div>
      <div>paid@example.test</div>
      <div>2026-08-15 05:47:15</div>
      <main>OpenAI ChatGPT Plus Subscription successfully subscribed Order sub_fixture Payment method PayPal</main>
    </body></html>
    """
    monkeypatch.setattr(paid_mail_service, "_fetch_mail_page", lambda _url: page)

    result = paid_mail_service.check_paid_confirmation(
        "https://mail.example.test/mailbox/token", "paid@example.test"
    )

    assert result.status == "confirmed"
    assert result.subject == "ChatGPT - New plan"
    assert result.received_at is not None


def test_confirmation_page_does_not_match_another_recipient(monkeypatch) -> None:
    monkeypatch.setattr(
        paid_mail_service,
        "_fetch_mail_page",
        lambda _url: "OpenAI ChatGPT Plus Subscription PayPal other@example.test",
    )

    result = paid_mail_service.check_paid_confirmation(
        "https://mail.example.test/mailbox/token", "paid@example.test"
    )

    assert result.status == "not_found"


def test_mail_check_rejects_private_network_urls() -> None:
    with pytest.raises(paid_mail_service.PaidMailCheckError, match="非公开"):
        paid_mail_service._validate_public_url("http://127.0.0.1/mailbox/token")
