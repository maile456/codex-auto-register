from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from backend.account_mfa import AccountMfaResult, PaidAccountMfaBackfill


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.limit_value = 0

    def sort(self, *_args: Any) -> "_Cursor":
        return self

    def limit(self, value: int) -> "_Cursor":
        self.limit_value = value
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        limit = self.limit_value or length or len(self.rows)
        return [dict(row) for row in self.rows[:limit]]


class _Collection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.last_query: dict[str, Any] | None = None

    def find(
        self,
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> _Cursor:
        self.last_query = query
        return _Cursor(self.rows)


def test_candidates_only_include_current_paid_accounts() -> None:
    pipeline = _Collection([{"accountId": "paid-1"}, {"accountId": "paid-2"}])
    accounts = _Collection(
        [
            {"_id": "paid-1", "email": "one@example.test"},
            {"_id": "paid-2", "email": "two@example.test"},
        ]
    )
    service = object.__new__(PaidAccountMfaBackfill)
    service.mongo = SimpleNamespace(database={"account_pipeline": pipeline})
    service.resources = SimpleNamespace(accounts=accounts)

    result = asyncio.run(service.candidates(limit=1))

    assert [row["_id"] for row in result] == ["paid-1"]
    assert accounts.last_query == {
        "_id": {"$in": ["paid-1", "paid-2"]},
        "$or": [
            {"totpSecret": {"$in": ["", None]}},
            {"chatgptPassword": {"$in": ["", None]}},
        ],
        "securityBackfillError": {"$nin": ["account_deactivated"]},
        "email": {"$type": "string", "$ne": ""},
        "emailAccessUrl": {"$type": "string", "$ne": ""},
    }


def test_roxy_failures_are_retried_before_account_succeeds() -> None:
    service = object.__new__(PaidAccountMfaBackfill)
    service._run_one = AsyncMock(
        side_effect=[
            AccountMfaResult("account-1", "failed", "RoxyApiError"),
            AccountMfaResult("account-1", "success", "account_security_configured"),
        ]
    )

    result = asyncio.run(service._run_one_with_retries({"_id": "account-1"}))

    assert result.status == "success"
    assert service._run_one.await_count == 2
