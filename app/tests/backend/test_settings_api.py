from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


DEFAULT_PATH = r"D:\RoxyBrowser\RoxyBrowser.exe"
LEGACY_PATH = r"D:\ImRun Browser\ImRun Browser.exe"
TEST_API_KEY = "TEST_ROXY_KEY_DO_NOT_LOG"


def client_for(path: Path) -> TestClient:
    return TestClient(create_app(path))


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "browserExecutablePath": DEFAULT_PATH,
        "roxyApiKey": "",
        "roxyApiPort": 50000,
        "headless": False,
        "proxyRetryCount": 1,
        "concurrency": 2,
        "taskTimeoutSeconds": 0,
    }
    payload.update(overrides)
    return payload


def public_defaults() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "browserExecutablePath": DEFAULT_PATH,
        "roxyApiKey": "",
        "roxyApiPort": 50000,
        "headless": False,
        "proxyRetryCount": 1,
        "concurrency": 2,
        "taskTimeoutSeconds": 0,
        "updatedAt": None,
    }


def test_get_missing_file_returns_schema_two_defaults_without_writing(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"

    response = client_for(settings_path).get("/api/settings/execution")

    assert response.status_code == 200
    assert response.json() == public_defaults()
    assert not settings_path.exists()


def test_schema_one_is_migrated_in_memory_and_written_as_schema_two(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    legacy = {
        "schemaVersion": 1,
        "browserExecutablePath": LEGACY_PATH,
        "concurrency": 4,
        "taskTimeoutSeconds": 480,
        "updatedAt": None,
    }
    settings_path.write_text(json.dumps(legacy), encoding="utf-8")
    client = client_for(settings_path)

    loaded = client.get("/api/settings/execution")

    assert loaded.status_code == 200
    assert loaded.json()["schemaVersion"] == 2
    assert loaded.json()["browserExecutablePath"] == DEFAULT_PATH
    assert loaded.json()["concurrency"] == 4
    assert json.loads(settings_path.read_text(encoding="utf-8"))["schemaVersion"] == 1

    saved = client.put(
        "/api/settings/execution",
        json=valid_payload(concurrency=4, taskTimeoutSeconds=480),
    )
    assert saved.status_code == 200
    assert json.loads(settings_path.read_text(encoding="utf-8"))["schemaVersion"] == 2
    assert json.loads(
        settings_path.with_name("settings.json.bak").read_text(encoding="utf-8")
    )["schemaVersion"] == 1


def test_schema_one_custom_browser_path_is_preserved(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    custom_path = r"E:\Browsers\Custom.exe"
    settings_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "browserExecutablePath": custom_path,
                "concurrency": 2,
                "taskTimeoutSeconds": 300,
                "updatedAt": None,
            }
        ),
        encoding="utf-8",
    )

    response = client_for(settings_path).get("/api/settings/execution")

    assert response.status_code == 200
    assert response.json()["browserExecutablePath"] == custom_path


@pytest.mark.parametrize("concurrency", [1, 12])
def test_concurrency_boundaries_are_persisted(tmp_path: Path, concurrency: int) -> None:
    settings_path = tmp_path / "settings.json"
    response = client_for(settings_path).put(
        "/api/settings/execution", json=valid_payload(concurrency=concurrency)
    )

    assert response.status_code == 200
    assert response.json()["concurrency"] == concurrency
    assert json.loads(settings_path.read_text(encoding="utf-8"))["concurrency"] == concurrency


@pytest.mark.parametrize("concurrency", [0, 13, 1.5, "2", None])
def test_invalid_concurrency_is_rejected(tmp_path: Path, concurrency: object) -> None:
    response = client_for(tmp_path / "settings.json").put(
        "/api/settings/execution", json=valid_payload(concurrency=concurrency)
    )
    assert response.status_code == 422


@pytest.mark.parametrize("timeout", [-1, 1.5, "300", None])
def test_invalid_timeout_is_rejected(tmp_path: Path, timeout: object) -> None:
    response = client_for(tmp_path / "settings.json").put(
        "/api/settings/execution", json=valid_payload(taskTimeoutSeconds=timeout)
    )
    assert response.status_code == 422


def test_zero_timeout_is_persisted_as_unlimited(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    response = client_for(settings_path).put(
        "/api/settings/execution",
        json=valid_payload(taskTimeoutSeconds=0),
    )

    assert response.status_code == 200
    assert response.json()["taskTimeoutSeconds"] == 0
    assert json.loads(settings_path.read_text(encoding="utf-8"))[
        "taskTimeoutSeconds"
    ] == 0


@pytest.mark.parametrize("port", [0, 65536, 1.5, "50000", None])
def test_invalid_roxy_port_is_rejected_without_echoing_key(
    tmp_path: Path,
    port: object,
) -> None:
    response = client_for(tmp_path / "settings.json").put(
        "/api/settings/execution",
        json=valid_payload(roxyApiPort=port, roxyApiKey=TEST_API_KEY),
    )
    assert response.status_code == 422
    assert TEST_API_KEY not in response.text


@pytest.mark.parametrize("retry_count", [-1, 6, 1.5, "1", None])
def test_invalid_proxy_retry_count_is_rejected(
    tmp_path: Path,
    retry_count: object,
) -> None:
    response = client_for(tmp_path / "settings.json").put(
        "/api/settings/execution",
        json=valid_payload(proxyRetryCount=retry_count),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("headless", [0, 1, "true", None])
def test_headless_requires_a_real_boolean(tmp_path: Path, headless: object) -> None:
    response = client_for(tmp_path / "settings.json").put(
        "/api/settings/execution", json=valid_payload(headless=headless)
    )
    assert response.status_code == 422


def test_api_key_is_persisted_and_returned_in_plaintext(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    client = client_for(settings_path)

    saved = client.put(
        "/api/settings/execution",
        json=valid_payload(roxyApiKey=TEST_API_KEY),
    )
    loaded = client.get("/api/settings/execution")

    assert saved.status_code == 200
    assert loaded.status_code == 200
    assert saved.json()["roxyApiKey"] == TEST_API_KEY
    assert loaded.json()["roxyApiKey"] == TEST_API_KEY
    assert "roxyApiKeyConfigured" not in saved.json()
    assert "roxyApiKeyConfigured" not in loaded.json()
    assert json.loads(settings_path.read_text(encoding="utf-8"))["roxyApiKey"] == TEST_API_KEY


def test_blank_api_key_explicitly_clears_existing_key(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    client = client_for(settings_path)
    client.put(
        "/api/settings/execution",
        json=valid_payload(roxyApiKey=TEST_API_KEY),
    ).raise_for_status()

    cleared = client.put(
        "/api/settings/execution",
        json=valid_payload(concurrency=4, roxyApiKey="   "),
    )

    assert cleared.status_code == 200
    assert cleared.json()["roxyApiKey"] == ""
    assert client.get("/api/settings/execution").json()["roxyApiKey"] == ""
    assert json.loads(settings_path.read_text(encoding="utf-8"))["roxyApiKey"] == ""


def test_api_key_is_required_in_update_payload(tmp_path: Path) -> None:
    payload = valid_payload()
    payload.pop("roxyApiKey")

    response = client_for(tmp_path / "settings.json").put(
        "/api/settings/execution",
        json=payload,
    )

    assert response.status_code == 422


def test_saved_settings_survive_new_app_instance(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    first_client = client_for(settings_path)
    first_client.put(
        "/api/settings/execution",
        json=valid_payload(
            concurrency=7,
            taskTimeoutSeconds=480,
            headless=True,
            proxyRetryCount=5,
        ),
    ).raise_for_status()

    response = client_for(settings_path).get("/api/settings/execution")

    assert response.status_code == 200
    assert response.json()["concurrency"] == 7
    assert response.json()["taskTimeoutSeconds"] == 480
    assert response.json()["headless"] is True
    assert response.json()["proxyRetryCount"] == 5
    assert response.json()["updatedAt"] is not None


def test_second_write_creates_backup_and_removes_temp(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    client = client_for(settings_path)
    client.put("/api/settings/execution", json=valid_payload(concurrency=3)).raise_for_status()
    first_version = settings_path.read_bytes()

    client.put("/api/settings/execution", json=valid_payload(concurrency=6)).raise_for_status()

    assert settings_path.with_name("settings.json.bak").read_bytes() == first_version
    assert json.loads(settings_path.read_text(encoding="utf-8"))["concurrency"] == 6
    assert not settings_path.with_name("settings.json.tmp").exists()


def test_corrupted_file_is_reported_and_not_overwritten(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    corrupted = b"{not-json"
    settings_path.write_bytes(corrupted)
    client = client_for(settings_path)

    get_response = client.get("/api/settings/execution")
    put_response = client.put("/api/settings/execution", json=valid_payload())

    assert get_response.status_code == 500
    assert get_response.json()["detail"]["code"] == "settings_corrupted"
    assert put_response.status_code == 500
    assert settings_path.read_bytes() == corrupted
    assert not settings_path.with_name("settings.json.bak").exists()


def test_unsupported_schema_is_reported_and_not_overwritten(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    unsupported = '{"schemaVersion":99}'
    settings_path.write_text(unsupported, encoding="utf-8")
    client = client_for(settings_path)

    get_response = client.get("/api/settings/execution")
    put_response = client.put("/api/settings/execution", json=valid_payload())

    assert get_response.status_code == 500
    assert get_response.json()["detail"]["code"] == "settings_corrupted"
    assert put_response.status_code == 500
    assert settings_path.read_text(encoding="utf-8") == unsupported


def test_browser_path_must_not_be_blank(tmp_path: Path) -> None:
    response = client_for(tmp_path / "settings.json").put(
        "/api/settings/execution", json=valid_payload(browserExecutablePath="   ")
    )
    assert response.status_code == 422
