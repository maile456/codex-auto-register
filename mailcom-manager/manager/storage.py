from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


class CredentialCipher(Protocol):
    def encrypt(self, value: str) -> bytes: ...

    def decrypt(self, value: bytes) -> str: ...


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountStore:
    def __init__(self, path: Path, cipher: CredentialCipher) -> None:
        self.path = path
        self.cipher = cipher
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    email_normalized TEXT NOT NULL UNIQUE,
                    password_encrypted BLOB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    message_count INTEGER,
                    last_checked_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_updated
                    ON accounts(updated_at DESC);
                CREATE TABLE IF NOT EXISTS aliases (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    email_normalized TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_aliases_account
                    ON aliases(account_id, created_at DESC);
                """
            )

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "email": row["email"],
            "status": row["status"],
            "messageCount": row["message_count"],
            "lastCheckedAt": row["last_checked_at"],
            "lastError": row["last_error"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "aliasCount": int(row["alias_count"]) if "alias_count" in row.keys() else 0,
        }

    @staticmethod
    def _public_alias(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "accountId": row["account_id"],
            "email": row["email"],
            "label": row["label"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def import_account(self, email: str, password: str) -> bool:
        normalized = email.strip().casefold()
        now = utc_now_text()
        encrypted = self.cipher.encrypt(password)
        with self._lock, self._connect() as connection:
            alias_exists = connection.execute(
                "SELECT 1 FROM aliases WHERE email_normalized = ?",
                (normalized,),
            ).fetchone()
            if alias_exists is not None:
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO accounts (
                    id, email, email_normalized, password_encrypted,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), email.strip(), normalized, encrypted, now, now),
            )
            return cursor.rowcount == 1

    def list_accounts(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        where = ""
        params: list[Any] = []
        if query.strip():
            where = "WHERE a.email_normalized LIKE ?"
            params.append(f"%{query.strip().casefold()}%")
        with self._lock, self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM accounts a {where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT a.*, (
                    SELECT COUNT(*) FROM aliases x WHERE x.account_id = a.id
                ) AS alias_count
                FROM accounts a {where}
                ORDER BY a.created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [self._public(row) for row in rows], total

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.*, (
                    SELECT COUNT(*) FROM aliases x WHERE x.account_id = a.id
                ) AS alias_count
                FROM accounts a WHERE a.id = ?
                """,
                (account_id,),
            ).fetchone()
        return self._public(row) if row else None

    def underfilled_accounts(self, target_aliases: int) -> list[dict[str, Any]]:
        target = max(0, int(target_aliases))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, (
                    SELECT COUNT(*) FROM aliases x WHERE x.account_id = a.id
                ) AS alias_count
                FROM accounts a
                WHERE (
                    SELECT COUNT(*) FROM aliases x WHERE x.account_id = a.id
                ) < ?
                ORDER BY alias_count DESC, a.created_at ASC
                """,
                (target,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def get_credentials(self, account_id: str) -> tuple[str, str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT email, password_encrypted FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["email"]), self.cipher.decrypt(bytes(row["password_encrypted"]))

    def get_credentials_by_email(self, email: str) -> tuple[str, str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT email, password_encrypted FROM accounts WHERE email_normalized = ?",
                (email.strip().casefold(),),
            ).fetchone()
        if row is None:
            return None
        return str(row["email"]), self.cipher.decrypt(bytes(row["password_encrypted"]))

    def get_mailbox_credentials(
        self, email: str
    ) -> tuple[str, str, str, bool] | None:
        normalized = email.strip().casefold()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT email AS requested_email, email AS login_email,
                       password_encrypted, 0 AS is_alias
                FROM accounts WHERE email_normalized = ?
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT x.email AS requested_email, a.email AS login_email,
                           a.password_encrypted, 1 AS is_alias
                    FROM aliases x
                    JOIN accounts a ON a.id = x.account_id
                    WHERE x.email_normalized = ?
                    """,
                    (normalized,),
                ).fetchone()
        if row is None:
            return None
        return (
            str(row["requested_email"]),
            str(row["login_email"]),
            self.cipher.decrypt(bytes(row["password_encrypted"])),
            bool(row["is_alias"]),
        )

    def all_emails(self) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT email FROM accounts ORDER BY created_at DESC"
            ).fetchall()
        return [str(row["email"]) for row in rows]

    def all_mailboxes(self) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT email, created_at FROM accounts
                UNION ALL
                SELECT email, created_at FROM aliases
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [str(row["email"]) for row in rows]

    def registration_items(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT email, email AS account_email, 0 AS is_alias, created_at
                FROM accounts
                UNION ALL
                SELECT x.email, a.email AS account_email, 1 AS is_alias, x.created_at
                FROM aliases x
                JOIN accounts a ON a.id = x.account_id
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            {
                "email": str(row["email"]),
                "accountEmail": str(row["account_email"]),
                "isAlias": bool(row["is_alias"]),
            }
            for row in rows
        ]

    def sync_snapshot(self) -> dict[str, Any]:
        """Return a complete credential snapshot for an explicit server push."""
        with self._lock, self._connect() as connection:
            account_rows = connection.execute(
                """
                SELECT email, password_encrypted
                FROM accounts
                ORDER BY created_at ASC
                """
            ).fetchall()
            alias_rows = connection.execute(
                """
                SELECT x.email, x.label, a.email AS account_email
                FROM aliases x
                JOIN accounts a ON a.id = x.account_id
                ORDER BY x.created_at ASC
                """
            ).fetchall()
        return {
            "version": 1,
            "accounts": [
                {
                    "email": str(row["email"]),
                    "password": self.cipher.decrypt(bytes(row["password_encrypted"])),
                }
                for row in account_rows
            ],
            "aliases": [
                {
                    "email": str(row["email"]),
                    "accountEmail": str(row["account_email"]),
                    "label": str(row["label"] or ""),
                }
                for row in alias_rows
            ],
        }

    def import_alias(self, account_id: str, email: str, label: str = "") -> str:
        normalized = email.strip().casefold()
        now = utc_now_text()
        with self._lock, self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone() is None:
                return "missing_account"
            if connection.execute(
                "SELECT 1 FROM accounts WHERE email_normalized = ?", (normalized,)
            ).fetchone() is not None:
                return "duplicate"
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO aliases (
                    id, account_id, email, email_normalized, label,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    account_id,
                    email.strip(),
                    normalized,
                    label.strip()[:200],
                    now,
                    now,
                ),
            )
            return "inserted" if cursor.rowcount == 1 else "duplicate"

    def list_aliases(self, account_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM aliases
                WHERE account_id = ?
                ORDER BY created_at DESC
                """,
                (account_id,),
            ).fetchall()
        return [self._public_alias(row) for row in rows]

    def delete_alias(self, alias_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM aliases WHERE id = ?", (alias_id,)
            )
            return cursor.rowcount == 1

    def update_check(
        self,
        account_id: str,
        *,
        status: str,
        message_count: int | None,
        error: str | None,
    ) -> None:
        now = utc_now_text()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE accounts
                SET status = ?, message_count = ?, last_checked_at = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, message_count, now, error, now, account_id),
            )

    def delete_account(self, account_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM accounts WHERE id = ?", (account_id,)
            )
            return cursor.rowcount == 1

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM accounts GROUP BY status"
            ).fetchall()
            total = int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            aliases = int(connection.execute("SELECT COUNT(*) FROM aliases").fetchone()[0])
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "total": total,
            "online": counts.get("online", 0),
            "failed": counts.get("failed", 0),
            "unknown": counts.get("unknown", 0),
            "aliases": aliases,
        }
