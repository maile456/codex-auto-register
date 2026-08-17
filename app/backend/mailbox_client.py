from __future__ import annotations

import asyncio
import hashlib
import imaplib
import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from time import monotonic
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests


UTC = timezone.utc
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
PUBLIC_DNS_URL = "https://dns.google/resolve"
API798_HOSTS = frozenset({"api798.com", "www.api798.com"})
LAIMAIL_HOSTS = frozenset({"laimail.com", "www.laimail.com"})
MAILCOM_MANAGER_HOSTS = frozenset({"127.0.0.1", "localhost"})
MAILCOM_MANAGER_PORT = 3211
MAILCOM_MANAGER_PATH = "/api/mail/latest"
MAILCOM_IMAP_SCHEME = "mailcom-imap"
MAILCOM_IMAP_HOST = "imap.mail.com"
MAILCOM_IMAP_PORT = 993
MAILCOM_WEBMAIL_URL = "https://www.mail.com/int/"


def mailbox_source_for_document(document: dict[str, Any]) -> str:
    """Build the private mailbox source consumed by MailboxClient."""
    access_url = str(document.get("accessUrl") or document.get("emailAccessUrl") or "")
    if str(document.get("mailboxKind") or "") != "mailcom_imap":
        return access_url
    email = str(document.get("email") or "").strip()
    password = str(document.get("mailboxPassword") or "")
    if not email or not password:
        raise MailboxClientError(
            "mailbox_auth_missing",
            "mail.com 邮箱密码缺失",
        )
    return urlunsplit(
        (
            MAILCOM_IMAP_SCHEME,
            f"{quote(email, safe='')}:{quote(password, safe='')}@{MAILCOM_IMAP_HOST}:{MAILCOM_IMAP_PORT}",
            "",
            "",
            "",
        )
    )


def direct_mailbox_access_url(
    value: str,
    email: str,
    *,
    api798_auth_code: str | None = None,
) -> str:
    """Return a browser-ready mailbox URL without changing stored source data."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if (parsed.hostname or "").casefold() not in API798_HOSTS:
        return value
    if parsed.path.rstrip("/").casefold() not in {"/get_code", "/latest"}:
        return value

    query = parse_qs(parsed.query, keep_blank_values=True)
    requested_emails = query.get("email", [])
    if (
        len(requested_emails) != 1
        or requested_emails[0].strip().casefold() != email.strip().casefold()
    ):
        return value
    supplied_codes = query.get("auth_code", [])
    configured_code = (
        os.getenv("API798_AUTH_CODE", "")
        if api798_auth_code is None
        else api798_auth_code
    ).strip()
    auth_code = (
        supplied_codes[0].strip()
        if len(supplied_codes) == 1 and supplied_codes[0].strip()
        else configured_code
    )
    if not auth_code:
        return value
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/latest",
            urlencode({"email": requested_emails[0].strip(), "auth_code": auth_code}),
            "",
        )
    )


VERIFICATION_PATTERN = re.compile(
    r"temporary\s+(?:chatgpt\s+)?verification\s+code|"
    r"(?:chatgpt|openai).{0,80}verification\s+code|"
    r"enter\s+this\s+temporary\s+verification\s+code|"
    r"(?:chatgpt|openai).{0,80}(?:验证码|驗證碼)|临时验证码|臨時驗證碼|"
    r"(?:chatgpt|openai).{0,80}(?:一時的な)?(?:認証|検証|確認)コード|"
    r"(?:一時的な|一時)?(?:認証|検証|確認)コード|"
    r"この一時(?:的な)?(?:認証|検証|確認)コードを入力|"
    r"geçici.{0,40}doğrulama\s+kodu|"
    r"(?:bu\s+)?geçici\s+doğrulama\s+kodunu\s+gir",
    re.IGNORECASE | re.DOTALL,
)
CODE_PATTERN = re.compile(r"^[0-9]{6}$")
RFC2822_PATTERN = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+[A-Z][a-z]{2}\s+"
    r"\d{4}\s+\d{2}:\d{2}:\d{2}\s+(?:[+-]\d{4}|GMT|UTC)",
    re.IGNORECASE,
)
ISO8601_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})",
    re.IGNORECASE,
)
BEIJING_TIME_PATTERN = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})\s*\(北京时间\)"
)
EMBEDDED_HTML_PATTERN = re.compile(
    r"\b(?:var|let|const)\s+htmlContent\s*=\s*"
    r"(\"(?:\\.|[^\"\\])*\")\s*;",
    re.DOTALL,
)
JSON_TEXT_KEYS = {
    "subject",
    "title",
    "html",
    "text",
    "body",
    "message",
    "msg",
    "content",
    "receivedat",
    "createdat",
    "timestamp",
    "time",
    "date",
    "code",
    "verification_code",
}


def _is_mailcom_manager_url(value: str, email: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port or 80
    except ValueError:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    requested_emails = query.get("email", [])
    return (
        parsed.scheme.casefold() == "http"
        and (parsed.hostname or "").casefold() in MAILCOM_MANAGER_HOSTS
        and port == MAILCOM_MANAGER_PORT
        and parsed.path.rstrip("/").casefold() == MAILCOM_MANAGER_PATH
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and set(query) == {"email"}
        and len(requested_emails) == 1
        and requested_emails[0].strip().casefold() == email.strip().casefold()
    )


class MailboxClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class MailboxSnapshot:
    fingerprint: str
    verification_code: str | None
    received_at_utc: datetime | None
    received_offset: str | None


@dataclass(frozen=True, slots=True)
class VerificationCodeResult:
    verification_code: str
    received_at_utc: datetime | None
    received_offset: str | None
    wait_ms: int
    mail_age_ms: int | None


class _VisibleTextParser(HTMLParser):
    SKIPPED_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self, *, parse_srcdoc: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self._skip_depth = 0
        self._parse_srcdoc = parse_srcdoc

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self.SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if normalized_tag == "iframe" and self._parse_srcdoc:
            srcdoc = next(
                (value for name, value in attrs if name.casefold() == "srcdoc" and value),
                None,
            )
            if srcdoc:
                embedded_parser = _VisibleTextParser(parse_srcdoc=False)
                embedded_parser.feed(srcdoc)
                embedded_parser.close()
                self.texts.extend(embedded_parser.texts)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        normalized = " ".join(data.split())
        if normalized:
            self.texts.append(normalized)


class _MailboxLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = next(
            (value for name, value in attrs if name.casefold() == "href" and value),
            None,
        )
        if href:
            self.links.append(href)


def _format_offset(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        raise ValueError("missing timezone offset")
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def parse_mail_datetime(value: str) -> tuple[datetime, str] | None:
    candidate = " ".join(value.strip().split())
    candidate = re.sub(r"\s*\((?:UTC|GMT)\)\s*$", "", candidate, flags=re.IGNORECASE)

    iso_match = ISO8601_PATTERN.search(candidate)
    if iso_match:
        iso_value = iso_match.group(0)
        if iso_value.endswith(("Z", "z")):
            iso_value = f"{iso_value[:-1]}+00:00"
        if re.search(r"[+-]\d{4}$", iso_value):
            iso_value = f"{iso_value[:-2]}:{iso_value[-2:]}"
        try:
            parsed = datetime.fromisoformat(iso_value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC), _format_offset(parsed)

    rfc_match = RFC2822_PATTERN.search(candidate)
    if rfc_match:
        try:
            parsed = parsedate_to_datetime(rfc_match.group(0))
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC), _format_offset(parsed)

    beijing_match = BEIJING_TIME_PATTERN.search(candidate)
    if beijing_match:
        try:
            parsed = datetime(
                *(int(part) for part in beijing_match.groups()),
                tzinfo=timezone(timedelta(hours=8)),
            )
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.astimezone(UTC), "+08:00"
    return None


def _json_texts(value: Any, key: str | None = None) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for child_key, child_value in value.items():
            result.extend(_json_texts(child_value, str(child_key).casefold()))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_json_texts(child, key))
        return result
    if isinstance(value, str) and (key is None or key in JSON_TEXT_KEYS):
        return [value]
    return []


def _visible_texts(payload: str, content_type: str) -> list[str]:
    values: list[str]
    lowered_type = content_type.casefold()
    stripped_payload = payload.lstrip()
    declared_json = "json" in lowered_type
    looks_like_json = stripped_payload.startswith(("{", "["))
    parsed_json: Any | None = None
    if declared_json or looks_like_json:
        try:
            parsed_json = json.loads(payload)
        except (TypeError, ValueError):
            if declared_json:
                raise MailboxClientError(
                    "mailbox_response_invalid",
                    "接码服务返回了无效 JSON",
                ) from None

    if parsed_json is not None:
        values = _json_texts(parsed_json)
    elif any(kind in lowered_type for kind in ("html", "text")):
        values = [payload]
    else:
        raise MailboxClientError(
            "mailbox_response_invalid",
            "接码服务返回了不支持的内容类型",
        )

    texts: list[str] = []
    for value in values:
        if re.search(r"<[/!A-Za-z][^>]*>", value):
            parser = _VisibleTextParser()
            try:
                parser.feed(value)
                parser.close()
            except Exception:
                raise MailboxClientError(
                    "mailbox_response_invalid",
                    "接码服务返回了无法解析的 HTML",
                ) from None
            texts.extend(parser.texts)
            for match in EMBEDDED_HTML_PATTERN.finditer(value):
                try:
                    embedded = json.loads(match.group(1))
                except (TypeError, ValueError):
                    continue
                if not isinstance(embedded, str):
                    continue
                embedded_parser = _VisibleTextParser()
                try:
                    embedded_parser.feed(embedded)
                    embedded_parser.close()
                except Exception:
                    continue
                texts.extend(embedded_parser.texts)
        else:
            texts.extend(part for part in (" ".join(line.split()) for line in value.splitlines()) if part)
    return texts


def parse_mailbox_snapshot(payload: str, content_type: str = "text/html") -> MailboxSnapshot:
    texts = _visible_texts(payload, content_type)
    normalized = "\n".join(texts)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    code: str | None = None
    code_index: int | None = None
    if VERIFICATION_PATTERN.search(normalized):
        for index, text in enumerate(texts):
            if not CODE_PATTERN.fullmatch(text):
                continue
            context = "\n".join(texts[max(0, index - 12) : index])
            if VERIFICATION_PATTERN.search(context):
                code = text
                code_index = index
                break

    dated: list[tuple[int, datetime, str]] = []
    for index, text in enumerate(texts):
        parsed = parse_mail_datetime(text)
        if parsed is not None:
            dated.append((index, parsed[0], parsed[1]))

    received_at: datetime | None = None
    received_offset: str | None = None
    if dated:
        eligible = [item for item in dated if code_index is None or item[0] <= code_index]
        selected = eligible[-1] if eligible else dated[0]
        received_at, received_offset = selected[1], selected[2]

    return MailboxSnapshot(
        fingerprint=fingerprint,
        verification_code=code,
        received_at_utc=received_at,
        received_offset=received_offset,
    )


def _parse_laimail_snapshot(payload: str, content_type: str) -> MailboxSnapshot:
    snapshot = parse_mailbox_snapshot(payload, content_type)
    if snapshot.received_at_utc is not None:
        return snapshot
    timestamp_match = re.search(
        r"\b(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\b",
        payload,
    )
    if timestamp_match is None:
        return snapshot
    try:
        received_at = datetime.strptime(
            timestamp_match.group(1),
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        return snapshot
    return MailboxSnapshot(
        fingerprint=snapshot.fingerprint,
        verification_code=snapshot.verification_code,
        received_at_utc=received_at.astimezone(UTC),
        received_offset="+08:00",
    )


def _is_laimail_list_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if (parsed.hostname or "").casefold() not in LAIMAIL_HOSTS:
        return False
    if parsed.path.rstrip("/").casefold() != "/api/mail_lists.php":
        return False
    return not parse_qs(parsed.query, keep_blank_values=True).get("uid")


def _laimail_message_urls(payload: str, list_url: str, email: str) -> list[str]:
    source = urlsplit(list_url)
    source_query = parse_qs(source.query, keep_blank_values=True)
    expected_passwords = source_query.get("pass", [])
    if len(expected_passwords) != 1 or not expected_passwords[0]:
        return []
    parser = _MailboxLinkParser()
    try:
        parser.feed(payload)
        parser.close()
    except Exception:
        return []

    expected_email = email.strip().casefold()
    message_urls: list[str] = []
    for href in parser.links:
        try:
            candidate = urlsplit(urljoin(list_url, href))
        except ValueError:
            continue
        query = parse_qs(candidate.query, keep_blank_values=True)
        candidate_emails = query.get("email", [])
        candidate_passwords = query.get("pass", [])
        candidate_uids = query.get("uid", [])
        if (
            (candidate.hostname or "").casefold() not in LAIMAIL_HOSTS
            or candidate.path.rstrip("/").casefold() != "/api/mail_lists.php"
            or len(candidate_emails) != 1
            or candidate_emails[0].strip().casefold() != expected_email
            or candidate_passwords != expected_passwords
            or len(candidate_uids) != 1
            or not candidate_uids[0].strip()
        ):
            continue
        normalized = urlunsplit(
            ("https", "laimail.com", "/api/mail_lists.php", candidate.query, "")
        )
        if normalized not in message_urls:
            message_urls.append(normalized)
        if len(message_urls) >= 10:
            break
    return message_urls


def _default_resolver(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise MailboxClientError(
            "mailbox_unavailable",
            "接码服务域名无法解析",
            retryable=True,
        ) from None
    return sorted({str(record[4][0]) for record in records})


def _default_public_resolver(host: str, _port: int) -> list[str]:
    addresses: set[str] = set()
    try:
        with requests.Session() as session:
            session.trust_env = False
            for record_type in ("A", "AAAA"):
                response = session.get(
                    PUBLIC_DNS_URL,
                    params={"name": host, "type": record_type},
                    headers={
                        "Accept": "application/dns-json",
                        "User-Agent": "AutoRegister-MailboxProbe/1.0",
                    },
                    timeout=(5, 10),
                )
                try:
                    response.raise_for_status()
                    payload = response.json()
                finally:
                    response.close()
                if int(payload.get("Status", -1)) != 0:
                    continue
                for answer in payload.get("Answer", []):
                    if not isinstance(answer, dict) or answer.get("type") not in (1, 28):
                        continue
                    value = str(answer.get("data", "")).strip()
                    try:
                        ipaddress.ip_address(value)
                    except ValueError:
                        continue
                    addresses.add(value)
    except (requests.RequestException, TypeError, ValueError):
        raise MailboxClientError(
            "mailbox_unavailable",
            "无法复核接码服务的公共 DNS",
            retryable=True,
        ) from None
    if not addresses:
        raise MailboxClientError(
            "mailbox_unavailable",
            "接码服务没有可复核的公共 DNS 地址",
            retryable=True,
        )
    return sorted(addresses)


def _default_imap_factory(host: str, port: int, *, timeout: float) -> Any:
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


def _message_datetime(value: str) -> tuple[datetime, str] | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return parsed.astimezone(UTC), f"{sign}{hours:02d}:{minutes:02d}"


def _parse_imap_message(payload: bytes) -> MailboxSnapshot:
    try:
        message = BytesParser(policy=policy.default).parsebytes(payload)
    except Exception:
        raise MailboxClientError(
            "mailbox_response_invalid",
            "mail.com 返回了无法解析的邮件",
        ) from None

    texts = [str(message.get("Subject") or "")]
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
        if content_type == "text/html":
            texts.extend(_visible_texts(content, "text/html"))
        else:
            texts.extend(content.splitlines())

    snapshot = parse_mailbox_snapshot("\n".join(texts), "text/plain")
    received = _message_datetime(str(message.get("Date") or ""))
    if received is None:
        return snapshot
    return MailboxSnapshot(
        fingerprint=snapshot.fingerprint,
        verification_code=snapshot.verification_code,
        received_at_utc=received[0],
        received_offset=received[1],
    )


class MailboxClient:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        resolver: Callable[[str, int], list[str]] = _default_resolver,
        public_resolver: Callable[[str, int], list[str]] = _default_public_resolver,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        connect_timeout_seconds: float = 5,
        read_timeout_seconds: float = 15,
        max_redirects: int = 3,
        api798_auth_code: str | None = None,
        imap_factory: Callable[..., Any] | None = None,
        max_imap_messages: int = 20,
    ) -> None:
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.resolver = resolver
        self.public_resolver = public_resolver
        self.max_response_bytes = max_response_bytes
        self.timeout = (connect_timeout_seconds, read_timeout_seconds)
        self.max_redirects = max_redirects
        self.imap_factory = imap_factory or _default_imap_factory
        self.max_imap_messages = max(1, max_imap_messages)
        self.api798_auth_code = (
            os.getenv("API798_AUTH_CODE", "")
            if api798_auth_code is None
            else api798_auth_code
        ).strip()

    @staticmethod
    def _mailcom_credentials(value: str, email: str) -> tuple[str, str]:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise MailboxClientError(
                "mailbox_auth_invalid",
                "mail.com IMAP 凭据格式无效",
            ) from None
        username = unquote(parsed.username or "").strip()
        password = unquote(parsed.password or "")
        if (
            parsed.scheme.casefold() != MAILCOM_IMAP_SCHEME
            or (parsed.hostname or "").casefold() != MAILCOM_IMAP_HOST
            or port != MAILCOM_IMAP_PORT
            or username.casefold() != email.strip().casefold()
            or not password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise MailboxClientError(
                "mailbox_auth_invalid",
                "mail.com IMAP 凭据与当前邮箱不匹配",
            )
        return username, password

    def _fetch_mailcom_snapshot(self, source: str, email: str) -> MailboxSnapshot:
        username, password = self._mailcom_credentials(source, email)
        client: Any | None = None
        try:
            client = self.imap_factory(
                MAILCOM_IMAP_HOST,
                MAILCOM_IMAP_PORT,
                timeout=max(self.timeout),
            )
            try:
                status, _ = client.login(username, password)
            except imaplib.IMAP4.error:
                raise MailboxClientError(
                    "mailbox_auth_failed",
                    "mail.com 邮箱登录失败，请检查密码或 IMAP 权限",
                ) from None
            if str(status).upper() != "OK":
                raise MailboxClientError(
                    "mailbox_auth_failed",
                    "mail.com 邮箱登录失败，请检查密码或 IMAP 权限",
                )

            folders = ["INBOX", "Spam", "Junk"]
            try:
                list_status, list_data = client.list()
            except imaplib.IMAP4.error:
                list_status, list_data = "NO", []
            if str(list_status).upper() == "OK":
                for raw_folder in list_data or []:
                    decoded = (
                        raw_folder.decode("utf-8", errors="replace")
                        if isinstance(raw_folder, bytes)
                        else str(raw_folder)
                    )
                    match = re.search(r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*$', decoded)
                    if match is None:
                        continue
                    folder = match.group(1).replace(r'\"', '"')
                    if re.search(r"spam|junk", folder, re.IGNORECASE) and folder not in folders:
                        folders.append(folder)

            mailbox_fingerprints: list[str] = []
            selected_any = False
            for folder in folders:
                try:
                    select_status, _ = client.select(folder, readonly=True)
                except imaplib.IMAP4.error:
                    continue
                if str(select_status).upper() != "OK":
                    continue
                selected_any = True
                search_status, search_data = client.search(None, "ALL")
                if str(search_status).upper() != "OK" or not search_data:
                    continue
                raw_ids = search_data[0]
                message_ids = raw_ids.split() if isinstance(raw_ids, bytes) else []
                mailbox_fingerprints.append(
                    f"{folder}:{','.join(value.decode('ascii', errors='ignore') for value in message_ids[-self.max_imap_messages:])}"
                )
                for message_id in reversed(message_ids[-self.max_imap_messages:]):
                    fetch_status, fetch_data = client.fetch(message_id, "(BODY.PEEK[])")
                    if str(fetch_status).upper() != "OK" or not fetch_data:
                        continue
                    raw_message = next(
                        (
                            item[1]
                            for item in fetch_data
                            if isinstance(item, tuple)
                            and len(item) >= 2
                            and isinstance(item[1], bytes)
                        ),
                        None,
                    )
                    if raw_message is None or len(raw_message) > self.max_response_bytes:
                        continue
                    snapshot = _parse_imap_message(raw_message)
                    if snapshot.verification_code is not None:
                        return snapshot

            if not selected_any:
                raise MailboxClientError(
                    "mailbox_unavailable",
                    "mail.com 收件箱暂时不可用",
                    retryable=True,
                )
            fingerprint = hashlib.sha256(
                "\n".join(mailbox_fingerprints).encode("utf-8")
            ).hexdigest()
            return MailboxSnapshot(
                fingerprint=fingerprint,
                verification_code=None,
                received_at_utc=None,
                received_offset=None,
            )
        except MailboxClientError:
            raise
        except (imaplib.IMAP4.error, OSError, socket.timeout, TimeoutError):
            raise MailboxClientError(
                "mailbox_unavailable",
                "mail.com IMAP 连接失败",
                retryable=True,
            ) from None
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass

    def _request_url(self, value: str, email: str) -> str:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return value
        hostname = (parsed.hostname or "").casefold()
        path = parsed.path.rstrip("/").casefold()
        if hostname in LAIMAIL_HOSTS and path in {"/m.php", "/api/mail_lists.php"}:
            query = parse_qs(parsed.query, keep_blank_values=True)
            email_key = "u" if path == "/m.php" else "email"
            password_key = "p" if path == "/m.php" else "pass"
            requested_emails = query.get(email_key, [])
            passwords = query.get(password_key, [])
            if (
                len(requested_emails) != 1
                or requested_emails[0].strip().casefold() != email.strip().casefold()
                or len(passwords) != 1
                or not passwords[0].strip()
            ):
                raise MailboxClientError(
                    "mailbox_url_invalid",
                    "LaiMail 接码地址与当前邮箱不匹配",
                )
            if path == "/m.php":
                return urlunsplit(
                    (
                        "https",
                        "laimail.com",
                        "/api/mail_lists.php",
                        urlencode(
                            {
                                "email": requested_emails[0].strip(),
                                "pass": passwords[0],
                            }
                        ),
                        "",
                    )
                )
            return urlunsplit(
                ("https", "laimail.com", "/api/mail_lists.php", parsed.query, "")
            )

        if hostname not in API798_HOSTS:
            return value
        if path not in {"/get_code", "/latest"}:
            return value

        query = parse_qs(parsed.query, keep_blank_values=True)
        requested_emails = query.get("email", [])
        if (
            len(requested_emails) != 1
            or requested_emails[0].strip().casefold() != email.strip().casefold()
        ):
            raise MailboxClientError(
                "mailbox_url_invalid",
                "api798 接码地址与当前邮箱不匹配",
            )
        supplied_codes = query.get("auth_code", [])
        auth_code = (
            supplied_codes[0].strip()
            if len(supplied_codes) == 1
            else self.api798_auth_code
        )
        if not auth_code:
            raise MailboxClientError(
                "mailbox_auth_missing",
                "api798 接码认证码未配置",
            )
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/latest",
                urlencode({"email": requested_emails[0].strip(), "auth_code": auth_code}),
                "",
            )
        )

    def _validate_url(self, value: str, email: str) -> str:
        if _is_mailcom_manager_url(value, email):
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port or 443
        except ValueError:
            raise MailboxClientError("mailbox_url_invalid", "接码地址格式无效") from None
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise MailboxClientError("mailbox_url_invalid", "接码地址必须是安全 HTTPS 地址")
        addresses = self.resolver(parsed.hostname, port)
        if not addresses:
            raise MailboxClientError(
                "mailbox_unavailable",
                "接码服务域名没有可用地址",
                retryable=True,
            )
        try:
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except ValueError:
            parsed_addresses = []
        if parsed_addresses and all(address.is_global for address in parsed_addresses):
            return value

        fake_ip_answers = parsed_addresses and all(
            address in FAKE_IP_NETWORK for address in parsed_addresses
        )
        if fake_ip_answers:
            verified_addresses = self.public_resolver(parsed.hostname, port)
            try:
                parsed_verified_addresses = [
                    ipaddress.ip_address(address) for address in verified_addresses
                ]
            except ValueError:
                parsed_verified_addresses = []
            if parsed_verified_addresses and all(
                address.is_global for address in parsed_verified_addresses
            ):
                return value

        if not parsed_addresses or not all(
            address.is_global for address in parsed_addresses
        ):
            raise MailboxClientError(
                "mailbox_target_blocked",
                "接码地址指向了非公网目标",
            )
        return value

    def fetch_snapshot(self, access_url: str, email: str) -> MailboxSnapshot:
        try:
            source_scheme = urlsplit(access_url).scheme.casefold()
        except ValueError:
            source_scheme = ""
        if source_scheme == MAILCOM_IMAP_SCHEME:
            return self._fetch_mailcom_snapshot(access_url, email)
        current_url = self._validate_url(self._request_url(access_url, email), email)
        response: requests.Response | None = None
        try:
            for redirect_index in range(self.max_redirects + 1):
                try:
                    response = self.session.get(
                        current_url,
                        headers={
                            "Accept": "text/html,text/plain,application/json",
                            "Cache-Control": "no-cache",
                            "Pragma": "no-cache",
                            "User-Agent": "AutoRegister-MailboxProbe/1.0",
                        },
                        timeout=self.timeout,
                        allow_redirects=False,
                        stream=True,
                    )
                except requests.RequestException:
                    raise MailboxClientError(
                        "mailbox_unavailable",
                        "接码服务请求失败",
                        retryable=True,
                    ) from None

                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location")
                    response.close()
                    response = None
                    if not location or redirect_index >= self.max_redirects:
                        raise MailboxClientError(
                            "mailbox_unavailable",
                            "接码服务重定向无效",
                            retryable=True,
                        )
                    current_url = self._validate_url(urljoin(current_url, location), email)
                    continue
                break

            if response is None or response.status_code != 200:
                raise MailboxClientError(
                    "mailbox_unavailable",
                    "接码服务暂时不可用",
                    retryable=True,
                )

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > self.max_response_bytes:
                        raise MailboxClientError(
                            "mailbox_response_too_large",
                            "接码服务响应超过大小限制",
                        )
                except ValueError:
                    pass

            content = bytearray()
            for chunk in response.iter_content(chunk_size=65_536):
                if not chunk:
                    continue
                content.extend(chunk)
                if len(content) > self.max_response_bytes:
                    raise MailboxClientError(
                        "mailbox_response_too_large",
                        "接码服务响应超过大小限制",
                    )

            encoding = response.encoding or "utf-8"
            payload = bytes(content).decode(encoding, errors="replace")
            content_type = response.headers.get("Content-Type", "text/html")
            if _is_laimail_list_url(current_url):
                list_snapshot = parse_mailbox_snapshot(payload, content_type)
                for message_url in _laimail_message_urls(
                    payload,
                    current_url,
                    email,
                ):
                    try:
                        message_snapshot = self.fetch_snapshot(message_url, email)
                    except MailboxClientError as exc:
                        if not exc.retryable:
                            raise
                        continue
                    if message_snapshot.verification_code is not None:
                        return message_snapshot
                return list_snapshot
            if (urlsplit(current_url).hostname or "").casefold() in LAIMAIL_HOSTS:
                return _parse_laimail_snapshot(payload, content_type)
            return parse_mailbox_snapshot(payload, content_type)
        finally:
            if response is not None:
                response.close()

    async def get_snapshot(self, access_url: str, email: str) -> MailboxSnapshot:
        return await asyncio.to_thread(self.fetch_snapshot, access_url, email)

    async def wait_for_new_code(
        self,
        access_url: str,
        email: str,
        submitted_at_utc: datetime,
        *,
        timeout_seconds: float = 120,
        poll_interval_seconds: float = 2,
        future_clock_skew_seconds: float = 120,
        past_clock_skew_seconds: float = 5,
        baseline: MailboxSnapshot | None = None,
        baseline_stable_seconds: float = 0.75,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_now: Callable[[], float] = monotonic,
    ) -> VerificationCodeResult:
        if submitted_at_utc.tzinfo is None:
            raise ValueError("submitted_at_utc must be timezone-aware")
        submitted_at_utc = submitted_at_utc.astimezone(UTC)
        try:
            is_mailcom = urlsplit(access_url).scheme.casefold() == MAILCOM_IMAP_SCHEME
        except ValueError:
            is_mailcom = False
        if is_mailcom:
            poll_interval_seconds = max(5, poll_interval_seconds)
        started = monotonic_now()
        deadline = started + timeout_seconds
        saw_missing_time = False
        saw_stale = False
        saw_successful_response = False
        last_retryable_error: MailboxClientError | None = None
        baseline_code = baseline.verification_code if baseline is not None else None
        baseline_available = baseline is not None
        undated_candidate: str | None = None
        undated_candidate_since: float | None = None

        while True:
            try:
                snapshot = await self.get_snapshot(access_url, email)
                saw_successful_response = True
            except MailboxClientError as exc:
                if not exc.retryable:
                    raise
                last_retryable_error = exc
                snapshot = None

            now = utc_now().astimezone(UTC)
            if snapshot is not None and snapshot.verification_code is not None:
                if snapshot.received_at_utc is None or snapshot.received_offset is None:
                    saw_missing_time = True
                    code_changed_from_baseline = (
                        baseline_available
                        and snapshot.verification_code != baseline_code
                    )
                    if code_changed_from_baseline:
                        if snapshot.verification_code != undated_candidate:
                            undated_candidate = snapshot.verification_code
                            undated_candidate_since = monotonic_now()
                        elif (
                            undated_candidate_since is not None
                            and monotonic_now() - undated_candidate_since
                            >= max(0, baseline_stable_seconds)
                        ):
                            return VerificationCodeResult(
                                verification_code=snapshot.verification_code,
                                received_at_utc=None,
                                received_offset=None,
                                wait_ms=max(
                                    0,
                                    int((monotonic_now() - started) * 1000),
                                ),
                                mail_age_ms=None,
                            )
                    else:
                        undated_candidate = None
                        undated_candidate_since = None
                else:
                    received = snapshot.received_at_utc.astimezone(UTC)
                    lower_bound = submitted_at_utc - timedelta(
                        seconds=max(0, past_clock_skew_seconds)
                    )
                    upper_bound = now + timedelta(seconds=future_clock_skew_seconds)
                    if lower_bound <= received <= upper_bound:
                        return VerificationCodeResult(
                            verification_code=snapshot.verification_code,
                            received_at_utc=received,
                            received_offset=snapshot.received_offset,
                            wait_ms=max(0, int((monotonic_now() - started) * 1000)),
                            mail_age_ms=int((now - received).total_seconds() * 1000),
                        )
                    saw_stale = True

            remaining = deadline - monotonic_now()
            if remaining <= 0:
                if saw_missing_time:
                    raise MailboxClientError(
                        "mail_time_missing",
                        "验证邮件缺少明确时区的发送时间",
                    )
                if saw_stale:
                    raise MailboxClientError(
                        "stale_verification_email",
                        "接码页中只有旧的验证邮件",
                    )
                if not saw_successful_response and last_retryable_error is not None:
                    raise last_retryable_error
                raise MailboxClientError(
                    "verification_code_timeout",
                    "等待新验证码超时",
                )
            await sleep(min(poll_interval_seconds, remaining))
