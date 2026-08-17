from __future__ import annotations

import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests


MAX_MAIL_PAGE_BYTES = 1_000_000
MAX_REDIRECTS = 3


class PaidMailCheckError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PaidMailCheckResult:
    status: str
    subject: str
    received_at: datetime | None
    error_code: str | None = None
    order_id: str | None = None


def _decode_cfemail(value: str) -> str:
    try:
        raw = bytes.fromhex(value)
        if len(raw) < 2:
            return ""
        key = raw[0]
        return bytes(byte ^ key for byte in raw[1:]).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


class _MailboxHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.subject_parts: list[str] = []
        self.title_parts: list[str] = []
        self._subject_depth = 0
        self._title_depth = 0
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        classes = set(attributes.get("class", "").casefold().split())
        if tag.casefold() == "div" and "subject" in classes:
            self._subject_depth = 1
        elif self._subject_depth:
            self._subject_depth += 1
        if tag.casefold() == "title":
            self._title_depth = 1
        elif self._title_depth:
            self._title_depth += 1
        protected = attributes.get("data-cfemail", "")
        if protected:
            decoded = _decode_cfemail(protected)
            if decoded:
                self.parts.append(decoded)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if self._subject_depth:
            self._subject_depth -= 1
        if self._title_depth:
            self._title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not data.strip():
            return
        self.parts.append(data)
        if self._subject_depth:
            self.subject_parts.append(data)
        if self._title_depth:
            self.title_parts.append(data)

    @staticmethod
    def _join(values: list[str]) -> str:
        return re.sub(r"\s+", " ", html.unescape(" ".join(values))).strip()

    @property
    def text(self) -> str:
        return self._join(self.parts)

    @property
    def subject(self) -> str:
        return self._join(self.subject_parts) or self._join(self.title_parts)


def _validate_public_url(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError as exc:
        raise PaidMailCheckError("邮箱 URL 格式无效") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise PaidMailCheckError("邮箱 URL 必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password:
        raise PaidMailCheckError("邮箱 URL 不允许包含地址栏凭据")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PaidMailCheckError("邮箱 URL 域名解析失败") from exc
    if not addresses:
        raise PaidMailCheckError("邮箱 URL 域名没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise PaidMailCheckError("邮箱 URL 指向了非公开网络地址")
    return text


def _same_origin(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return (
        a.scheme.casefold(),
        (a.hostname or "").casefold(),
        a.port or (443 if a.scheme.casefold() == "https" else 80),
    ) == (
        b.scheme.casefold(),
        (b.hostname or "").casefold(),
        b.port or (443 if b.scheme.casefold() == "https" else 80),
    )


def _fetch_mail_page(url: str) -> str:
    current = _validate_public_url(url)
    session = requests.Session()
    session.trust_env = False
    try:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = session.get(
                    current,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "User-Agent": "AutoRegister-MailCheck/1.0",
                    },
                    allow_redirects=False,
                    timeout=(8, 15),
                    stream=True,
                )
            except requests.RequestException as exc:
                raise PaidMailCheckError(f"邮箱页面请求失败：{type(exc).__name__}") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                target = _validate_public_url(urljoin(current, location))
                if not _same_origin(url, target):
                    raise PaidMailCheckError("邮箱页面发生了跨域跳转")
                current = target
                continue
            if response.status_code != 200:
                raise PaidMailCheckError(f"邮箱页面返回 HTTP {response.status_code}")
            content_type = response.headers.get("Content-Type", "").casefold()
            if "html" not in content_type and "text" not in content_type:
                raise PaidMailCheckError("邮箱页面没有返回 HTML 文本")
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > MAX_MAIL_PAGE_BYTES:
                    raise PaidMailCheckError("邮箱页面内容超过大小限制")
            encoding = response.encoding or "utf-8"
            return bytes(content).decode(encoding, errors="replace")
        raise PaidMailCheckError("邮箱页面跳转次数过多")
    finally:
        session.close()


def _local_mailcom_confirmation(
    url: str,
    expected_email: str,
    paid_at: datetime,
) -> PaidMailCheckResult | None:
    parsed = urlsplit(str(url or "").strip())
    if not (
        parsed.scheme.casefold() == "http"
        and (parsed.hostname or "").casefold() in {"127.0.0.1", "localhost"}
        and (parsed.port or 80) == 3211
        and parsed.path.rstrip("/").casefold() == "/api/mail/latest"
    ):
        return None
    endpoint = f"http://{parsed.hostname}:3211/api/mail/payment-confirmation"
    session = requests.Session()
    session.trust_env = False
    try:
        try:
            response = session.get(
                endpoint,
                params={"email": expected_email, "since": paid_at.isoformat()},
                headers={"Accept": "application/json"},
                timeout=(5, 30),
            )
        except requests.RequestException as exc:
            raise PaidMailCheckError(f"MailCom 到账查询失败：{type(exc).__name__}") from exc
        if response.status_code != 200:
            raise PaidMailCheckError(f"MailCom 到账查询返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaidMailCheckError("MailCom 到账查询没有返回 JSON") from exc
        if str(payload.get("email") or "").strip().casefold() != expected_email.strip().casefold():
            raise PaidMailCheckError("MailCom 到账查询返回了其他邮箱")
        received_at = None
        if payload.get("receivedAt"):
            try:
                received_at = datetime.fromisoformat(
                    str(payload["receivedAt"]).replace("Z", "+00:00")
                )
            except ValueError:
                received_at = None
        confirmed = payload.get("status") == "confirmed" and bool(payload.get("found"))
        return PaidMailCheckResult(
            status="confirmed" if confirmed else "not_found",
            subject=str(payload.get("subject") or "")[:300],
            received_at=received_at,
            error_code=None if confirmed else "confirmation_pending",
            order_id=str(payload.get("orderId") or "") or None,
        )
    finally:
        session.close()


def _received_at(text: str) -> datetime | None:
    matches = re.findall(r"\b(20\d{2}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\b", text)
    if not matches:
        return None
    try:
        return datetime.fromisoformat(" ".join(matches[0]))
    except ValueError:
        return None


def check_paid_confirmation(
    url: str,
    expected_email: str,
    paid_at: datetime | None = None,
) -> PaidMailCheckResult:
    normalized_paid_at = paid_at or datetime.now().astimezone()
    local_result = _local_mailcom_confirmation(url, expected_email, normalized_paid_at)
    if local_result is not None:
        return local_result
    parser = _MailboxHtmlParser()
    parser.feed(_fetch_mail_page(url))
    text = parser.text
    lowered = text.casefold()
    expected = str(expected_email or "").strip().casefold()
    recipient_matches = bool(expected and expected in lowered)
    plus_marker = "chatgpt plus subscription" in lowered or "chatgpt plus" in lowered
    provider_marker = "openai" in lowered
    order_marker = "paypal" in lowered or bool(re.search(r"\bsub_[a-z0-9]+", lowered))
    success_marker = any(
        marker in lowered
        for marker in (
            "successfully subscribed",
            "subscription is active",
            "subscription confirmed",
            "正常に登録",
            "订阅成功",
            "訂閱成功",
            "erfolgreich abonniert",
            "abonnement confirmé",
            "başarıyla abone",
        )
    )
    received_at = _received_at(text)
    after_payment = paid_at is None or received_at is None or received_at >= paid_at.replace(tzinfo=None)
    order_match = re.search(r"\bsub_[a-z0-9]+\b", text, flags=re.IGNORECASE)
    confirmed = recipient_matches and plus_marker and provider_marker and success_marker and order_marker and after_payment
    return PaidMailCheckResult(
        status="confirmed" if confirmed else "not_found",
        subject=parser.subject[:300],
        received_at=received_at,
        error_code=None if confirmed else "confirmation_markers_missing",
        order_id=order_match.group(0) if order_match else None,
    )
