from __future__ import annotations

from manager.imap_client import ImapMailboxService


RAW_MESSAGE = (
    "From: OpenAI <noreply@example.test>\r\n"
    "To: alias@gardener.com\r\n"
    "Delivered-To: alias@gardener.com\r\n"
    "Date: Mon, 17 Aug 2026 08:30:00 +0000\r\n"
    "Subject: Your temporary ChatGPT verification code\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Enter this temporary verification code to continue:\r\n"
    "123456\r\n"
).encode()


class FakeImap:
    def __init__(self) -> None:
        self.fetch_query = ""
        self.readonly = False
        self.logged_out = False

    def login(self, email: str, password: str):
        assert email == "alias@gardener.com"
        assert password == "mail-password"
        return "OK", [b"authenticated"]

    def select(self, folder: str, readonly: bool = False):
        assert folder == "INBOX"
        self.readonly = readonly
        return "OK", [b"1"]

    def search(self, charset, criterion: str):
        assert charset is None
        assert criterion == "ALL"
        return "OK", [b"1"]

    def fetch(self, message_id: bytes, query: str):
        assert message_id == b"1"
        self.fetch_query = query
        return "OK", [(b"1 (BODY[])", RAW_MESSAGE), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logout"]


def test_imap_reader_uses_readonly_peek_and_extracts_recipient_code() -> None:
    fake = FakeImap()
    service = ImapMailboxService(factory=lambda *_args, **_kwargs: fake)

    result = service.test("alias@gardener.com", "mail-password")
    assert result == {"ok": True, "messageCount": 1}

    messages = service.messages(
        "alias@gardener.com", "mail-password", folder="INBOX", limit=20
    )
    assert len(messages) == 1
    assert messages[0].verification_code == "123456"
    assert "alias@gardener.com" in messages[0].recipients
    assert fake.readonly is True
    assert fake.fetch_query == "(BODY.PEEK[])"
    assert fake.logged_out is True


def test_forwarded_endpoint_preserves_mail_host_for_tls(monkeypatch) -> None:
    monkeypatch.setenv("MAILCOM_IMAP_CONNECT_HOST", "127.0.0.1")
    monkeypatch.setenv("MAILCOM_IMAP_CONNECT_PORT", "1993")
    service = ImapMailboxService()

    assert service.host == "imap.mail.com"
    assert service.port == 993
    assert service.connect_host == "127.0.0.1"
    assert service.connect_port == 1993
    assert service.route == "forwarded"
