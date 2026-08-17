from __future__ import annotations

import imaplib
import os
import re
import socket
import ssl
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlsplit

import socks


IMAP_HOST = "imap.mail.com"
IMAP_PORT = 993
CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
VERIFICATION_PATTERN = re.compile(
    r"chatgpt|openai|verification\s+code|temporary\s+code|"
    r"验证码|驗證碼|認証コード|確認コード|doğrulama\s+kodu",
    re.IGNORECASE,
)
ALLOWED_FOLDERS = {"INBOX", "Spam", "Junk"}


class _SocksImap4Ssl(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        proxy_host: str,
        proxy_port: int,
        timeout: float,
    ) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout: float | None):
        connection = socks.socksocket()
        connection.set_proxy(
            socks.SOCKS5,
            addr=self._proxy_host,
            port=self._proxy_port,
            rdns=True,
        )
        connection.settimeout(timeout)
        connection.connect((self.host, self.port))
        return self.ssl_context.wrap_socket(connection, server_hostname=self.host)


class _ForwardedImap4Ssl(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_host: str,
        connect_port: int,
        timeout: float,
    ) -> None:
        self._connect_host = connect_host
        self._connect_port = connect_port
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout: float | None):
        connection = socket.create_connection(
            (self._connect_host, self._connect_port),
            timeout=timeout,
        )
        return self.ssl_context.wrap_socket(connection, server_hostname=self.host)


class MailboxError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        normalized = " ".join(data.split())
        if normalized:
            self.texts.append(normalized)


def _html_text(value: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return ""
    return "\n".join(parser.texts)


def _message_text(message: Any) -> str:
    values: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            raw = part.get_payload(decode=True)
            if not isinstance(raw, bytes):
                continue
            content = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(content, str):
            continue
        values.append(_html_text(content) if content_type == "text/html" else content)
    return "\n".join(values)


def _received_at(message: Any) -> str | None:
    try:
        parsed = parsedate_to_datetime(str(message.get("Date") or ""))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class MailSummary:
    uid: str
    folder: str
    subject: str
    sender: str
    recipients: str
    received_at: str | None
    verification_code: str | None
    preview: str

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "uid": data["uid"],
            "folder": data["folder"],
            "subject": data["subject"],
            "sender": data["sender"],
            "recipients": data["recipients"],
            "receivedAt": data["received_at"],
            "verificationCode": data["verification_code"],
            "preview": data["preview"],
        }


class ImapMailboxService:
    def __init__(
        self,
        *,
        factory: Callable[..., Any] | None = None,
        timeout_seconds: float = 15,
        max_message_bytes: int = 2 * 1024 * 1024,
        proxy_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.host = (host or os.getenv("MAILCOM_IMAP_HOST", IMAP_HOST)).strip()
        self.port = int(port or os.getenv("MAILCOM_IMAP_PORT", str(IMAP_PORT)))
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("MailCom IMAP endpoint is invalid")
        self.connect_host = os.getenv("MAILCOM_IMAP_CONNECT_HOST", "").strip()
        self.connect_port = int(
            os.getenv("MAILCOM_IMAP_CONNECT_PORT", str(self.port))
        )
        if self.connect_host and not 1 <= self.connect_port <= 65535:
            raise ValueError("MailCom IMAP forwarded endpoint is invalid")
        self.proxy_url = (
            os.getenv("MAILCOM_IMAP_PROXY", "") if proxy_url is None else proxy_url
        ).strip()
        self.proxy_host = ""
        self.proxy_port = 0
        if self.proxy_url:
            parsed = urlsplit(self.proxy_url)
            if parsed.scheme.casefold() not in {"socks5", "socks5h"} or not parsed.hostname:
                raise ValueError("MAILCOM_IMAP_PROXY must be a SOCKS5 URL")
            self.proxy_host = parsed.hostname
            self.proxy_port = parsed.port or 1080
        self.factory = factory or self._default_factory
        self.timeout_seconds = timeout_seconds
        self.max_message_bytes = max_message_bytes

    @property
    def route(self) -> str:
        if self.proxy_host:
            return "socks5"
        if self.connect_host:
            return "forwarded"
        return "direct"

    def _default_factory(self, host: str, port: int, *, timeout: float) -> Any:
        if self.proxy_host:
            return _SocksImap4Ssl(
                host,
                port,
                proxy_host=self.proxy_host,
                proxy_port=self.proxy_port,
                timeout=timeout,
            )
        if self.connect_host:
            return _ForwardedImap4Ssl(
                host,
                port,
                connect_host=self.connect_host,
                connect_port=self.connect_port,
                timeout=timeout,
            )
        return imaplib.IMAP4_SSL(host, port, timeout=timeout)

    def _connect(self, email: str, password: str) -> Any:
        client: Any | None = None
        try:
            client = self.factory(self.host, self.port, timeout=self.timeout_seconds)
            status, _ = client.login(email, password)
            if str(status).upper() != "OK":
                raise MailboxError("auth_failed", "邮箱或密码错误，或者 IMAP 未启用")
            return client
        except MailboxError:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            raise
        except imaplib.IMAP4.error:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            raise MailboxError("auth_failed", "邮箱或密码错误，或者 IMAP 未启用") from None
        except (OSError, socket.timeout, TimeoutError):
            raise MailboxError(
                "connection_failed", "mail.com IMAP 连接失败", retryable=True
            ) from None

    @staticmethod
    def _logout(client: Any) -> None:
        try:
            client.logout()
        except Exception:
            pass

    def test(self, email: str, password: str) -> dict[str, Any]:
        client = self._connect(email, password)
        try:
            status, data = client.select("INBOX", readonly=True)
            if str(status).upper() != "OK":
                raise MailboxError("inbox_failed", "收件箱读取失败", retryable=True)
            count = int(data[0]) if data and bytes(data[0]).isdigit() else 0
            return {"ok": True, "messageCount": count}
        except MailboxError:
            raise
        except (imaplib.IMAP4.error, OSError, socket.timeout, TimeoutError):
            raise MailboxError("inbox_failed", "收件箱读取失败", retryable=True) from None
        finally:
            self._logout(client)

    def messages(
        self,
        email: str,
        password: str,
        *,
        folder: str = "INBOX",
        limit: int = 20,
    ) -> list[MailSummary]:
        if folder not in ALLOWED_FOLDERS:
            raise MailboxError("folder_invalid", "邮箱文件夹无效")
        client = self._connect(email, password)
        try:
            status, _ = client.select(folder, readonly=True)
            if str(status).upper() != "OK":
                return []
            status, data = client.search(None, "ALL")
            if str(status).upper() != "OK" or not data or not isinstance(data[0], bytes):
                return []
            ids = data[0].split()[-max(1, min(limit, 100)) :]
            results: list[MailSummary] = []
            for message_id in reversed(ids):
                fetch_status, fetch_data = client.fetch(message_id, "(BODY.PEEK[])")
                if str(fetch_status).upper() != "OK" or not fetch_data:
                    continue
                payload = next(
                    (
                        item[1]
                        for item in fetch_data
                        if isinstance(item, tuple)
                        and len(item) >= 2
                        and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if payload is None or len(payload) > self.max_message_bytes:
                    continue
                try:
                    message = BytesParser(policy=policy.default).parsebytes(payload)
                except Exception:
                    continue
                subject = str(message.get("Subject") or "(无主题)")[:500]
                sender = str(message.get("From") or "")[:500]
                recipients = " | ".join(
                    str(message.get(name) or "")
                    for name in ("To", "Delivered-To", "X-Original-To", "Envelope-To")
                    if message.get(name)
                )[:1000]
                body = _message_text(message)
                normalized = " ".join(body.split())
                candidate_text = f"{subject}\n{body}"
                code_match = (
                    CODE_PATTERN.search(candidate_text)
                    if VERIFICATION_PATTERN.search(candidate_text)
                    else None
                )
                results.append(
                    MailSummary(
                        uid=message_id.decode("ascii", errors="replace"),
                        folder=folder,
                        subject=subject,
                        sender=sender,
                        recipients=recipients,
                        received_at=_received_at(message),
                        verification_code=code_match.group(1) if code_match else None,
                        preview=normalized[:500],
                    )
                )
            return results
        except MailboxError:
            raise
        except (imaplib.IMAP4.error, OSError, socket.timeout, TimeoutError):
            raise MailboxError("inbox_failed", "邮件读取失败", retryable=True) from None
        finally:
            self._logout(client)
