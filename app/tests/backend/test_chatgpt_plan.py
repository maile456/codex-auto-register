import base64
import json

import pytest

from backend.chatgpt_plan import (
    PlanCheckError,
    check_account_plan_curl,
    normalize_access_token,
    parse_accounts_check,
)
from backend.plan_check_service import proxy_url
from backend.probe_store import ProxyLease


def test_normalize_access_token_accepts_supported_prefixes() -> None:
    assert normalize_access_token(" token ") == "token"
    assert normalize_access_token("Bearer token") == "token"
    assert normalize_access_token("Authorization: Bearer token") == "token"


def _jwt(account_id: str) -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "free",
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


@pytest.mark.parametrize(
    ("account", "eligible", "plan", "active"),
    [
        (
            {
                "account": {"account_id": "target", "plan_type": "free"},
                "entitlement": {
                    "subscription_plan": "chatgptfreeplan",
                    "has_active_subscription": False,
                },
                "eligible_promo_campaigns": {"plus": {"id": "campaign"}},
            },
            True,
            "free",
            False,
        ),
        (
            {
                "account": {"account_id": "target", "plan_type": "free"},
                "entitlement": {
                    "subscription_plan": "chatgptfreeplan",
                    "has_active_subscription": False,
                },
                "eligible_promo_campaigns": {},
            },
            False,
            "free",
            False,
        ),
        (
            {
                "account": {"account_id": "target", "plan_type": "plus"},
                "entitlement": {
                    "subscription_plan": "chatgptplusplan",
                    "has_active_subscription": True,
                },
                "eligible_promo_campaigns": {"plus": {"id": "ignored"}},
            },
            False,
            "plus",
            True,
        ),
    ],
)
def test_parse_accounts_check_plan_and_trial_rules(
    account: dict,
    eligible: bool,
    plan: str,
    active: bool,
) -> None:
    result = parse_accounts_check(
        {
            "accounts": {
                "other": {
                    "account": {"account_id": "other", "plan_type": "plus"}
                },
                "target": account,
            }
        },
        access_token=_jwt("target"),
    )
    assert result.account_id == "target"
    assert result.current_plan_type == plan
    assert result.plus_trial_eligible is eligible
    assert result.has_active_subscription is active


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()
        self.headers: dict[str, str] = {}


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.proxies: dict[str, str] = {}
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append((url, kwargs))
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_curl_check_uses_proxy_retries_and_never_returns_secrets() -> None:
    sessions = [
        FakeSession([FakeResponse(503)]),
        FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "accounts": {
                            "default": {
                                "account": {"plan_type": "free"},
                                "entitlement": {
                                    "subscription_plan": "chatgptfreeplan"
                                },
                                "eligible_promo_campaigns": {
                                    "plus": {"id": "trial"}
                                },
                            }
                        }
                    },
                )
            ]
        ),
    ]

    result = check_account_plan_curl(
        "Authorization: Bearer TEST_AT_DO_NOT_LOG",
        proxy_url="http://user:PRIVATE_PROXY_PASSWORD@proxy.test:10000",
        retry_delay_seconds=0,
        session_factory=lambda: sessions.pop(0),
    )

    assert result.plus_trial_eligible is True
    serialized = repr(result)
    assert "TEST_AT_DO_NOT_LOG" not in serialized
    assert "PRIVATE_PROXY_PASSWORD" not in serialized


def test_curl_401_marks_token_unauthorized_without_response_body() -> None:
    session = FakeSession([FakeResponse(401, {"private": "PRIVATE_RESPONSE_BODY"})])
    with pytest.raises(PlanCheckError) as exc_info:
        check_account_plan_curl(
            "TEST_AT_DO_NOT_LOG",
            proxy_url="http://proxy.test:10000",
            session_factory=lambda: session,
        )
    assert exc_info.value.code == "access_token_unauthorized"
    assert exc_info.value.http_status == 401
    assert "TEST_AT_DO_NOT_LOG" not in repr(exc_info.value)
    assert "PRIVATE_RESPONSE_BODY" not in repr(exc_info.value)


def test_proxy_url_encodes_credentials() -> None:
    value = proxy_url(
        ProxyLease("proxy", "proxy.test", 10000, "user:name", "p@ss/word")
    )
    assert value == "http://user%3Aname:p%40ss%2Fword@proxy.test:10000"
