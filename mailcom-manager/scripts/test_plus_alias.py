from __future__ import annotations

import secrets
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import socks


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manager.crypto import DpapiCredentialCipher  # noqa: E402
from manager.imap_client import ImapMailboxService  # noqa: E402
from manager.storage import AccountStore  # noqa: E402


class SocksSmtpSsl(smtplib.SMTP_SSL):
    def _get_socket(self, host: str, port: int, timeout: float):
        connection = socks.socksocket()
        connection.set_proxy(
            socks.SOCKS5,
            addr="127.0.0.1",
            port=7897,
            rdns=True,
        )
        connection.settimeout(timeout)
        connection.connect((host, port))
        context = self.context or ssl.create_default_context()
        return context.wrap_socket(connection, server_hostname=host)


def plus_alias(email: str) -> str:
    local, domain = email.strip().lower().split("@", 1)
    local = local.split("+", 1)[0]
    return f"{local}+{secrets.token_hex(3)}@{domain}"


def mask(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:2]}***@{domain}"


def main() -> int:
    if len(sys.argv) != 3:
        print("USAGE_ERROR")
        return 2
    target_email = sys.argv[1].strip().lower()
    sender_email = sys.argv[2].strip().lower()
    store = AccountStore(ROOT / "data" / "mailcom.db", DpapiCredentialCipher())
    target = store.get_credentials_by_email(target_email)
    sender = store.get_credentials_by_email(sender_email)
    if target is None or sender is None:
        print("ACCOUNT_NOT_FOUND")
        return 3

    alias = plus_alias(target[0])
    nonce = secrets.token_hex(8)
    subject = f"AutoRegister plus alias test {nonce}"
    message = EmailMessage()
    message["From"] = sender[0]
    message["To"] = alias
    message["Subject"] = subject
    message.set_content(f"Alias delivery verification {nonce}")

    try:
        with SocksSmtpSsl("smtp.mail.com", 465, timeout=30) as smtp:
            smtp.login(sender[0], sender[1])
            smtp.send_message(message)
    except Exception as exc:
        print(f"SMTP_FAILED={type(exc).__name__}")
        return 4
    print(f"SMTP_ACCEPTED=True TARGET={mask(alias)}")

    mailbox = ImapMailboxService(timeout_seconds=20)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        for folder in ("INBOX", "Spam", "Junk"):
            try:
                messages = mailbox.messages(
                    target[0], target[1], folder=folder, limit=30
                )
            except Exception:
                continue
            for item in messages:
                if item.subject == subject:
                    exact_recipient = alias.casefold() in item.recipients.casefold()
                    print(f"DELIVERED=True FOLDER={folder}")
                    print(f"RECIPIENT_PRESERVED={exact_recipient}")
                    print(f"ALIAS={alias}")
                    return 0 if exact_recipient else 5
        time.sleep(5)
    print("DELIVERED=False")
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
