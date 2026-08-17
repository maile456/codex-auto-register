from __future__ import annotations

import asyncio
import imaplib
import json
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from backend.mailbox_client import (
    MailboxClient,
    MailboxClientError,
    MailboxSnapshot,
    direct_mailbox_access_url,
    mailbox_source_for_document,
    parse_mail_datetime,
    parse_mailbox_snapshot,
)


UTC = timezone.utc
EMAIL = "person@example.com"
TOKEN = "opaque-token-value"


class FakeImapClient:
    def __init__(self, message: bytes, *, login_error: bool = False) -> None:
        self.message = message
        self.login_error = login_error
        self.selected: list[tuple[str, bool]] = []
        self.fetch_queries: list[str] = []
        self.logged_out = False

    def login(self, username: str, password: str):
        if self.login_error:
            raise imaplib.IMAP4.error("authentication failed")
        assert username == EMAIL
        assert password == "mail-password"
        return "OK", [b"authenticated"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "Spam"']

    def select(self, folder: str, readonly: bool = False):
        self.selected.append((folder, readonly))
        if folder in {"INBOX", "Spam"}:
            return "OK", [b"1"]
        return "NO", [b"missing"]

    def search(self, _charset, criterion: str):
        assert criterion == "ALL"
        return "OK", [b"1"]

    def fetch(self, _message_id: bytes, query: str):
        self.fetch_queries.append(query)
        return "OK", [(b"1 (BODY[] {1}", self.message), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logout"]


def mailcom_message(code: str = "123456") -> bytes:
    return (
        "From: noreply@example.test\r\n"
        "To: person@example.com\r\n"
        "Date: Mon, 17 Aug 2026 08:30:00 +0000\r\n"
        "Subject: Your temporary ChatGPT verification code\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Enter this temporary verification code to continue:\r\n"
        f"{code}\r\n"
    ).encode()


def test_api798_direct_access_url_includes_configured_auth_code() -> None:
    assert direct_mailbox_access_url(
        "https://api798.com/get_code?email=person%40example.com",
        "person@example.com",
        api798_auth_code="AUTH_FIXTURE",
    ) == (
        "https://api798.com/latest?email=person%40example.com"
        "&auth_code=AUTH_FIXTURE"
    )


def test_direct_access_url_leaves_unmatched_or_unconfigured_urls_unchanged() -> None:
    mismatched = "https://api798.com/get_code?email=other%40example.com"
    assert direct_mailbox_access_url(
        mismatched,
        "person@example.com",
        api798_auth_code="AUTH_FIXTURE",
    ) == mismatched
    unconfigured = "https://api798.com/get_code?email=person%40example.com"
    assert direct_mailbox_access_url(
        unconfigured,
        "person@example.com",
        api798_auth_code="",
    ) == unconfigured
    ordinary = "https://mail.example.test/inbox/private"
    assert direct_mailbox_access_url(
        ordinary,
        "person@example.com",
        api798_auth_code="AUTH_FIXTURE",
    ) == ordinary


def test_mailcom_imap_source_reads_code_without_marking_message_seen() -> None:
    fake = FakeImapClient(mailcom_message())
    factory_calls: list[tuple[str, int, float]] = []

    def factory(host: str, port: int, *, timeout: float):
        factory_calls.append((host, port, timeout))
        return fake

    source = mailbox_source_for_document(
        {
            "email": EMAIL,
            "accessUrl": "https://www.mail.com/int/",
            "mailboxKind": "mailcom_imap",
            "mailboxPassword": "mail-password",
        }
    )
    client = MailboxClient(imap_factory=factory)
    snapshot = asyncio.run(client.get_snapshot(source, EMAIL))

    assert factory_calls == [("imap.mail.com", 993, 15)]
    assert snapshot.verification_code == "123456"
    assert snapshot.received_at_utc == datetime(2026, 8, 17, 8, 30, tzinfo=UTC)
    assert snapshot.received_offset == "+00:00"
    assert fake.selected[0] == ("INBOX", True)
    assert fake.fetch_queries == ["(BODY.PEEK[])"]
    assert fake.logged_out is True
    assert "mail-password" not in str(snapshot)


def test_mailcom_imap_authentication_failure_is_not_retryable() -> None:
    fake = FakeImapClient(mailcom_message(), login_error=True)
    client = MailboxClient(imap_factory=lambda *_args, **_kwargs: fake)
    source = mailbox_source_for_document(
        {
            "email": EMAIL,
            "mailboxKind": "mailcom_imap",
            "mailboxPassword": "mail-password",
        }
    )

    with pytest.raises(MailboxClientError) as exc_info:
        asyncio.run(client.get_snapshot(source, EMAIL))

    assert exc_info.value.code == "mailbox_auth_failed"
    assert exc_info.value.retryable is False
    assert "mail-password" not in exc_info.value.message


def test_local_mailcom_manager_url_returns_registration_code() -> None:
    payload = json.dumps(
        {
            "ok": True,
            "email": EMAIL,
            "found": True,
            "subject": "Your temporary ChatGPT verification code",
            "body": "Enter this temporary verification code to continue:\n654321",
            "verification_code": "654321",
            "receivedAt": "2026-08-17T08:30:00+00:00",
        }
    )
    session = FakeSession(
        [FakeResponse(payload, headers={"Content-Type": "application/json"})]
    )
    client = MailboxClient(session=session, resolver=lambda *_args: [])

    snapshot = asyncio.run(
        client.get_snapshot(
            "http://127.0.0.1:3211/api/mail/latest?email=person%40example.com",
            EMAIL,
        )
    )

    assert snapshot.verification_code == "654321"
    assert snapshot.received_at_utc == datetime(2026, 8, 17, 8, 30, tzinfo=UTC)
    assert session.requests[0][0].startswith("http://127.0.0.1:3211/api/mail/latest")


def test_local_mailcom_manager_url_rejects_mismatched_email() -> None:
    client = MailboxClient(session=FakeSession([]), resolver=lambda *_args: [])
    with pytest.raises(MailboxClientError) as exc_info:
        asyncio.run(
            client.get_snapshot(
                "http://127.0.0.1:3211/api/mail/latest?email=other%40example.com",
                EMAIL,
            )
        )
    assert exc_info.value.code == "mailbox_url_invalid"


class FakeResponse:
    def __init__(
        self,
        body: str,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body.encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.trust_env = True

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append((url, kwargs))
        return self.responses.popleft()


def public_resolver(_host: str, _port: int) -> list[str]:
    return ["8.8.8.8"]


def verification_html(
    code: str,
    received_at: str,
    *,
    extra_six_digits: str = "353740",
) -> str:
    return f"""
    <!doctype html>
    <html>
      <head>
        <title>Your temporary ChatGPT verification code</title>
        <style>.card {{ color: #{extra_six_digits}; }}</style>
      </head>
      <body>
        <div class="su">Your temporary ChatGPT verification code</div>
        <div class="dt">{received_at}</div>
        <p>Enter this temporary verification code to continue:</p>
        <p>{code}</p>
        <script>const tracking = 999999;</script>
      </body>
    </html>
    """


@pytest.mark.parametrize(
    ("raw", "expected_utc", "expected_offset"),
    [
        (
            "Sat, 08 Aug 2026 17:21:37 +0000 (UTC)",
            datetime(2026, 8, 8, 17, 21, 37, tzinfo=UTC),
            "+00:00",
        ),
        (
            "2026-08-09T09:30:00+08:00",
            datetime(2026, 8, 9, 1, 30, tzinfo=UTC),
            "+08:00",
        ),
        (
            "2026-08-09T01:30:00Z",
            datetime(2026, 8, 9, 1, 30, tzinfo=UTC),
            "+00:00",
        ),
        (
            "2026-08-09T09:30:00+0800",
            datetime(2026, 8, 9, 1, 30, tzinfo=UTC),
            "+08:00",
        ),
    ],
)
def test_mail_datetime_is_normalized_to_aware_utc(
    raw: str,
    expected_utc: datetime,
    expected_offset: str,
) -> None:
    assert parse_mail_datetime(raw) == (expected_utc, expected_offset)


def test_mail_datetime_without_timezone_is_rejected() -> None:
    assert parse_mail_datetime("2026-08-09 09:30:00") is None


def test_html_parser_uses_context_and_ignores_unrelated_six_digit_values() -> None:
    snapshot = parse_mailbox_snapshot(
        verification_html("123456", "Sat, 08 Aug 2026 17:21:37 +0000 (UTC)")
    )

    assert snapshot.verification_code == "123456"
    assert snapshot.received_at_utc == datetime(2026, 8, 8, 17, 21, 37, tzinfo=UTC)
    assert snapshot.received_offset == "+00:00"
    assert len(snapshot.fingerprint) == 64


def test_japanese_verification_mail_is_parsed_without_timezone() -> None:
    snapshot = parse_mailbox_snapshot(
        """
        <html>
          <head><title>ChatGPT の一時的な認証コード</title></head>
          <body>
            <div>時間</div><div>2026-08-14 14:32:19</div>
            <p>この一時検証コードを入力して続行してください:</p>
            <p>246810</p>
          </body>
        </html>
        """
    )
    assert snapshot.verification_code == "246810"
    assert snapshot.received_at_utc is None
    assert snapshot.received_offset is None


def test_turkish_verification_mail_is_parsed_with_beijing_timestamp() -> None:
    snapshot = parse_mailbox_snapshot(
        """
        <html>
          <head><title>Geçici ChatGPT parola sıfırlama</title></head>
          <body>
            <div>2026年08月15日 16:22:39 (北京时间)</div>
            <p>Geçici ChatGPT parola sıfırlama kodunuz</p>
            <p>Devam etmek için bu geçici doğrulama kodunu gir:</p>
            <p>135790</p>
          </body>
        </html>
        """
    )

    assert snapshot.verification_code == "135790"
    assert snapshot.received_at_utc == datetime(2026, 8, 15, 8, 22, 39, tzinfo=UTC)
    assert snapshot.received_offset == "+08:00"


def test_json_and_plain_text_responses_use_the_same_parser() -> None:
    json_snapshot = parse_mailbox_snapshot(
        """{
          "message": {
            "subject": "Your temporary ChatGPT verification code",
            "date": "2026-08-09T09:30:00+08:00",
            "text": "Enter this temporary verification code to continue:\\n654321"
          }
        }""",
        "application/json",
    )
    text_snapshot = parse_mailbox_snapshot(
        "Your temporary ChatGPT verification code\n"
        "2026-08-09T01:30:00Z\n"
        "Enter this temporary verification code to continue:\n654321",
        "text/plain",
    )

    assert json_snapshot.verification_code == "654321"
    assert json_snapshot.received_at_utc == datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    assert text_snapshot.verification_code == "654321"


def test_real_mailbox_msg_and_time_shape_is_parsed() -> None:
    snapshot = parse_mailbox_snapshot(
        """{
          "attachments": [],
          "mailbox": "INBOX",
          "msg": "<html><head><title>Your temporary ChatGPT verification code</title></head><body><p>Enter this temporary verification code to continue:</p><p>246810</p></body></html>",
          "status": true,
          "time": "Wed, 12 Aug 2026 15:15:12 +0000 (UTC)"
        }""",
        "application/json",
    )

    assert snapshot.verification_code == "246810"
    assert snapshot.received_at_utc == datetime(2026, 8, 12, 15, 15, 12, tzinfo=UTC)
    assert snapshot.received_offset == "+00:00"


def test_json_payload_is_detected_when_content_type_is_inaccurate() -> None:
    response = FakeResponse(
        """{
          "msg": "<p>Enter this temporary verification code to continue:</p><p>135790</p>",
          "time": "Wed, 12 Aug 2026 15:15:12 +0000 (UTC)"
        }""",
        headers={"Content-Type": "application/octet-stream"},
    )
    client = MailboxClient(
        session=FakeSession([response]),
        resolver=public_resolver,
    )

    snapshot = client.fetch_snapshot(
        f"https://mail.example/s/{TOKEN}/person%40example.com",
        EMAIL,
    )

    assert snapshot.verification_code == "135790"
    assert snapshot.received_at_utc == datetime(2026, 8, 12, 15, 15, 12, tzinfo=UTC)


def test_api798_iframe_script_content_and_beijing_time_are_parsed() -> None:
    embedded = (
        "<html><head><title>ChatGPT の一時的な認証コード</title></head>"
        "<body><p>この一時的な認証コードを入力してください</p>"
        "<p>246810</p></body></html>"
    )
    payload = (
        "<html><body><div>接收时间：</div>"
        "<div>2026年08月15日 14:26:17 (北京时间)</div>"
        "<iframe id='emailFrame'></iframe><script>"
        f"var htmlContent = {json.dumps(embedded)};"
        "document.getElementById('emailFrame').srcdoc = htmlContent;"
        "</script></body></html>"
    )

    snapshot = parse_mailbox_snapshot(payload, "text/html")

    assert snapshot.verification_code == "246810"
    assert snapshot.received_at_utc == datetime(2026, 8, 15, 6, 26, 17, tzinfo=UTC)
    assert snapshot.received_offset == "+08:00"


@pytest.mark.parametrize("prefix", ["s", "messages", "custom/prefix"])
def test_generic_token_email_path_contract(prefix: str) -> None:
    response = FakeResponse("<html><body>No mail</body></html>")
    session = FakeSession([response])
    client = MailboxClient(session=session, resolver=public_resolver)
    url = f"https://mail.example/{prefix}/{TOKEN}/person%40example.com"

    snapshot = client.fetch_snapshot(url, EMAIL)

    assert snapshot.verification_code is None
    assert session.trust_env is False
    assert session.requests[0][1]["allow_redirects"] is False
    assert session.requests[0][1]["stream"] is True
    assert response.closed is True


def test_opaque_token_url_does_not_need_email_in_path() -> None:
    response = FakeResponse("<html><body>No mail</body></html>")
    session = FakeSession([response])
    client = MailboxClient(session=session, resolver=public_resolver)

    snapshot_result = client.fetch_snapshot(
        f"https://mail.example/code/{TOKEN}",
        EMAIL,
    )

    assert snapshot_result.verification_code is None
    assert session.requests[0][0] == f"https://mail.example/code/{TOKEN}"


def test_api798_get_code_url_is_adapted_to_authenticated_latest_endpoint() -> None:
    response = FakeResponse("<h1>未找到匹配的邮件</h1>")
    session = FakeSession([response])
    client = MailboxClient(
        session=session,
        resolver=public_resolver,
        api798_auth_code="AUTH_FIXTURE",
    )

    result = client.fetch_snapshot(
        "https://api798.com/get_code?email=person%40example.com",
        "person@example.com",
    )

    assert result.verification_code is None
    assert session.requests[0][0] == (
        "https://api798.com/latest?email=person%40example.com&auth_code=AUTH_FIXTURE"
    )


def test_api798_existing_latest_auth_code_is_preserved() -> None:
    response = FakeResponse(
        "<p>Enter this temporary ChatGPT verification code to continue:</p><p>246810</p>"
    )
    session = FakeSession([response])
    client = MailboxClient(
        session=session,
        resolver=public_resolver,
        api798_auth_code="ENV_FIXTURE",
    )

    result = client.fetch_snapshot(
        "https://api798.com/latest?email=person%40example.com&auth_code=URL_FIXTURE",
        "person@example.com",
    )

    assert result.verification_code == "246810"
    assert session.requests[0][0].endswith("auth_code=URL_FIXTURE")


def test_laimail_http_entry_fetches_https_list_and_iframe_message() -> None:
    list_response = FakeResponse(
        """
        <a href="?email=person%40example.com&amp;pass=PASS_FIXTURE&amp;host=127.0.0.1&amp;uid=7&amp;page=1&amp;limit=30">
          message
        </a>
        """
    )
    detail_response = FakeResponse(
        """
        <html>
          <head><title>Your temporary ChatGPT verification code</title></head>
          <body>
            <span class="k">时间：</span><span class="v">2026-08-17 02:10:46</span>
            <iframe srcdoc="&lt;p&gt;Enter this temporary verification code to continue:&lt;/p&gt;&lt;p&gt;135790&lt;/p&gt;"></iframe>
          </body>
        </html>
        """
    )
    session = FakeSession([list_response, detail_response])
    client = MailboxClient(session=session, resolver=public_resolver)

    result = client.fetch_snapshot(
        "http://laimail.com/m.php?u=person%40example.com&p=PASS_FIXTURE",
        "person@example.com",
    )

    assert result.verification_code == "135790"
    assert result.received_at_utc == datetime(2026, 8, 16, 18, 10, 46, tzinfo=UTC)
    assert result.received_offset == "+08:00"
    assert session.requests[0][0] == (
        "https://laimail.com/api/mail_lists.php?"
        "email=person%40example.com&pass=PASS_FIXTURE"
    )
    assert session.requests[1][0].startswith(
        "https://laimail.com/api/mail_lists.php?email=person%40example.com"
    )
    assert "uid=7" in session.requests[1][0]


@pytest.mark.parametrize(
    "url",
    [
        "http://laimail.com/m.php?u=other%40example.com&p=PASS_FIXTURE",
        "http://laimail.com/m.php?u=person%40example.com&p=",
    ],
)
def test_laimail_rejects_mismatched_email_or_missing_password(url: str) -> None:
    client = MailboxClient(session=FakeSession([]), resolver=public_resolver)

    with pytest.raises(MailboxClientError) as exc_info:
        client.fetch_snapshot(url, "person@example.com")

    assert exc_info.value.code == "mailbox_url_invalid"
    assert "person@example.com" not in exc_info.value.message
    assert "PASS_FIXTURE" not in exc_info.value.message


@pytest.mark.parametrize(
    ("url", "configured_code", "expected_code"),
    [
        (
            "https://api798.com/get_code?email=other%40example.com",
            "AUTH_FIXTURE",
            "mailbox_url_invalid",
        ),
        (
            "https://api798.com/get_code?email=person%40example.com",
            "",
            "mailbox_auth_missing",
        ),
    ],
)
def test_api798_rejects_mismatched_email_or_missing_auth(
    url: str,
    configured_code: str,
    expected_code: str,
) -> None:
    client = MailboxClient(
        session=FakeSession([]),
        resolver=public_resolver,
        api798_auth_code=configured_code,
    )

    with pytest.raises(MailboxClientError) as exc_info:
        client.fetch_snapshot(url, "person@example.com")

    assert exc_info.value.code == expected_code
    assert "person@example.com" not in exc_info.value.message
    assert "AUTH_FIXTURE" not in exc_info.value.message


def test_fake_ip_dns_is_accepted_after_public_dns_verification() -> None:
    response = FakeResponse("<html><body>No mail</body></html>")
    session = FakeSession([response])
    verified_hosts: list[tuple[str, int]] = []

    def public_dns(host: str, port: int) -> list[str]:
        verified_hosts.append((host, port))
        return ["104.21.72.41", "2606:4700:3030::6815:4829"]

    client = MailboxClient(
        session=session,
        resolver=lambda _host, _port: ["198.18.0.4"],
        public_resolver=public_dns,
    )

    snapshot_result = client.fetch_snapshot(
        f"https://mail.example/s/{TOKEN}/person%40example.com",
        EMAIL,
    )

    assert snapshot_result.verification_code is None
    assert verified_hosts == [("mail.example", 443)]


@pytest.mark.parametrize(
    "verified_addresses",
    [
        ["127.0.0.1"],
        ["104.21.72.41", "10.0.0.1"],
        ["not-an-ip-address"],
    ],
)
def test_fake_ip_dns_is_blocked_without_exclusively_public_verification(
    verified_addresses: list[str],
) -> None:
    client = MailboxClient(
        session=FakeSession([]),
        resolver=lambda _host, _port: ["198.18.0.4"],
        public_resolver=lambda _host, _port: verified_addresses,
    )

    with pytest.raises(MailboxClientError) as exc_info:
        client.fetch_snapshot(
            f"https://mail.example/s/{TOKEN}/person%40example.com",
            EMAIL,
        )

    assert exc_info.value.code == "mailbox_target_blocked"


def test_mixed_system_dns_answers_do_not_use_fake_ip_fallback() -> None:
    public_dns_called = False

    def public_dns(_host: str, _port: int) -> list[str]:
        nonlocal public_dns_called
        public_dns_called = True
        return ["104.21.72.41"]

    client = MailboxClient(
        session=FakeSession([]),
        resolver=lambda _host, _port: ["198.18.0.4", "104.21.72.41"],
        public_resolver=public_dns,
    )

    with pytest.raises(MailboxClientError) as exc_info:
        client.fetch_snapshot(
            f"https://mail.example/s/{TOKEN}/person%40example.com",
            EMAIL,
        )

    assert exc_info.value.code == "mailbox_target_blocked"
    assert public_dns_called is False


@pytest.mark.parametrize(
    ("url", "resolver", "code"),
    [
        (
            f"http://mail.example/s/{TOKEN}/person%40example.com",
            public_resolver,
            "mailbox_url_invalid",
        ),
        (
            f"https://mail.example/s/{TOKEN}/person%40example.com",
            lambda _host, _port: ["127.0.0.1"],
            "mailbox_target_blocked",
        ),
    ],
)
def test_invalid_or_non_public_mailbox_targets_are_blocked(
    url: str,
    resolver,
    code: str,
) -> None:
    client = MailboxClient(session=FakeSession([]), resolver=resolver)

    with pytest.raises(MailboxClientError) as exc_info:
        client.fetch_snapshot(url, EMAIL)

    assert exc_info.value.code == code
    assert TOKEN not in exc_info.value.message
    assert EMAIL not in exc_info.value.message


def test_every_redirect_target_is_revalidated() -> None:
    redirect = FakeResponse(
        "",
        status_code=302,
        headers={"Location": f"https://internal.example/s/{TOKEN}/person%40example.com"},
    )
    session = FakeSession([redirect])

    def resolver(host: str, _port: int) -> list[str]:
        return ["127.0.0.1"] if host == "internal.example" else ["8.8.8.8"]

    client = MailboxClient(session=session, resolver=resolver)
    with pytest.raises(MailboxClientError) as exc_info:
        client.fetch_snapshot(
            f"https://mail.example/s/{TOKEN}/person%40example.com",
            EMAIL,
        )

    assert exc_info.value.code == "mailbox_target_blocked"
    assert redirect.closed is True


def test_response_size_limit_is_enforced() -> None:
    response = FakeResponse(
        "too large",
        headers={"Content-Type": "text/html", "Content-Length": "999"},
    )
    client = MailboxClient(
        session=FakeSession([response]),
        resolver=public_resolver,
        max_response_bytes=10,
    )

    with pytest.raises(MailboxClientError) as exc_info:
        client.fetch_snapshot(
            f"https://mail.example/s/{TOKEN}/person%40example.com",
            EMAIL,
        )

    assert exc_info.value.code == "mailbox_response_too_large"


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self.elapsed = 0.0

    def utc_now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    async def sleep(self, seconds: float) -> None:
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)


def snapshot(
    fingerprint: str,
    code: str | None,
    received_at: datetime | None,
    offset: str | None = "+00:00",
) -> MailboxSnapshot:
    return MailboxSnapshot(fingerprint, code, received_at, offset if received_at else None)


def run_poll(
    responses: list[MailboxSnapshot | MailboxClientError],
    submitted_at: datetime,
    *,
    timeout_seconds: float = 6,
    baseline: MailboxSnapshot | None = None,
):
    client = MailboxClient(session=FakeSession([]), resolver=public_resolver)
    queue = deque(responses)
    last = responses[-1]

    async def get_snapshot(_url: str, _email: str) -> MailboxSnapshot:
        value = queue.popleft() if queue else last
        if isinstance(value, MailboxClientError):
            raise value
        return value

    client.get_snapshot = get_snapshot  # type: ignore[method-assign]
    clock = FakeClock(submitted_at)
    return asyncio.run(
        client.wait_for_new_code(
            f"https://mail.example/s/{TOKEN}/person%40example.com",
            EMAIL,
            submitted_at,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=2,
            baseline=baseline,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
            monotonic_now=clock.monotonic,
        )
    )


def test_poll_ignores_stale_mail_then_accepts_fresh_timestamped_code() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    stale = snapshot("old", "111111", submitted - timedelta(days=1))
    fresh = snapshot("new", "222222", submitted + timedelta(seconds=1), "+08:00")

    result = run_poll([stale, fresh], submitted)

    assert result.verification_code == "222222"
    assert result.received_at_utc == submitted + timedelta(seconds=1)
    assert result.received_offset == "+08:00"
    assert result.wait_ms == 2_000
    assert result.mail_age_ms == 1_000


@pytest.mark.parametrize(
    ("candidate", "expected_code"),
    [
        (
            snapshot(
                "new",
                "222222",
                None,
                None,
            ),
            "mail_time_missing",
        ),
        (
            snapshot(
                "new",
                "222222",
                datetime(2026, 8, 8, tzinfo=UTC),
            ),
            "stale_verification_email",
        ),
        (
            snapshot(
                "new",
                "222222",
                datetime(2026, 8, 9, 2, 0, tzinfo=UTC),
            ),
            "stale_verification_email",
        ),
    ],
)
def test_poll_rejects_missing_stale_and_far_future_mail(
    candidate: MailboxSnapshot,
    expected_code: str,
) -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)

    with pytest.raises(MailboxClientError) as exc_info:
        run_poll([candidate], submitted, timeout_seconds=2)

    assert exc_info.value.code == expected_code


def test_poll_accepts_mail_at_exact_submission_boundary() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    candidate = snapshot("new", "222222", submitted)

    result = run_poll([candidate], submitted)

    assert result.verification_code == "222222"
    assert result.received_at_utc == submitted


def test_poll_accepts_mail_within_five_seconds_before_submission_boundary() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    candidate = snapshot("new", "222222", submitted - timedelta(seconds=5))

    result = run_poll([candidate], submitted)

    assert result.verification_code == "222222"
    assert result.received_at_utc == submitted - timedelta(seconds=5)


def test_poll_rejects_mail_before_five_second_submission_tolerance() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    candidate = snapshot(
        "new",
        "222222",
        submitted - timedelta(seconds=5, microseconds=1),
    )

    with pytest.raises(MailboxClientError) as exc_info:
        run_poll([candidate], submitted, timeout_seconds=2)

    assert exc_info.value.code == "stale_verification_email"


def test_poll_accepts_stable_undated_code_only_after_baseline_changes() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    old = snapshot("old", "111111", None, None)
    fresh = snapshot("new", "222222", None, None)

    result = run_poll([fresh, fresh], submitted, baseline=old)

    assert result.verification_code == "222222"
    assert result.received_at_utc is None
    assert result.received_offset is None
    assert result.mail_age_ms is None
    assert result.wait_ms == 2_000


def test_poll_rejects_unchanged_undated_baseline_code() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    old = snapshot("old", "111111", None, None)

    with pytest.raises(MailboxClientError) as exc_info:
        run_poll([old], submitted, timeout_seconds=2, baseline=old)

    assert exc_info.value.code == "mail_time_missing"


def test_successful_empty_response_is_not_overwritten_by_later_network_error() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    empty = snapshot("empty", None, None)
    unavailable = MailboxClientError(
        "mailbox_unavailable",
        "接码服务请求失败",
        retryable=True,
    )

    with pytest.raises(MailboxClientError) as exc_info:
        run_poll([empty, unavailable], submitted, timeout_seconds=2)

    assert exc_info.value.code == "verification_code_timeout"


def test_all_network_failures_report_mailbox_unavailable() -> None:
    submitted = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)
    unavailable = MailboxClientError(
        "mailbox_unavailable",
        "接码服务请求失败",
        retryable=True,
    )

    with pytest.raises(MailboxClientError) as exc_info:
        run_poll([unavailable], submitted, timeout_seconds=2)

    assert exc_info.value.code == "mailbox_unavailable"
