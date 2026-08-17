from __future__ import annotations

import pytest

from backend.pipeline_service import (
    AccountPipelineService,
    PipelineServiceError,
    SmsReceiverSettingsUpdate,
)


def test_local_mailbox_url_is_rewritten_to_server() -> None:
    actual = AccountPipelineService._public_mailbox_url(
        "http://127.0.0.1:3211/api/mail/latest?email=user%40example.com",
        "https://mail.example.test/mailbox",
    )
    assert actual == (
        "https://mail.example.test/mailbox/api/mail/latest"
        "?email=user%40example.com"
    )


def test_existing_public_mailbox_url_is_kept() -> None:
    public_url = "https://api.example.test/get_code?email=user%40example.com"
    assert AccountPipelineService._public_mailbox_url(public_url, "") == public_url


def test_local_mailbox_url_requires_public_server_base() -> None:
    with pytest.raises(PipelineServiceError) as captured:
        AccountPipelineService._public_mailbox_url(
            "http://localhost:3211/api/mail/latest?email=user%40example.com",
            "",
        )
    assert captured.value.code == "mailbox_public_base_url_required"


def test_receiver_server_urls_are_normalized() -> None:
    settings = SmsReceiverSettingsUpdate(
        enabled=True,
        baseUrl="https://sms.example.test/",
        mailboxPublicBaseUrl="https://mail.example.test/",
    )
    assert settings.baseUrl == "https://sms.example.test"
    assert settings.mailboxPublicBaseUrl == "https://mail.example.test"
