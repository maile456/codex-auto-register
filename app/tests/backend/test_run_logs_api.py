from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.main import create_app
from backend.run_log_store import (
    RunLogAppendInput,
    RunLogCreateInput,
    RunLogEntryInput,
    RunLogStore,
)


def client_for(tmp_path: Path) -> tuple[TestClient, Path, RunLogStore]:
    log_dir = tmp_path / "logs"
    app = create_app(tmp_path / "settings.json", log_dir)
    return TestClient(app), log_dir, app.state.run_log_store


def append_event(
    store: RunLogStore,
    run_id: UUID,
    event: str,
    *,
    level: str = "success",
    details: dict[str, object] | None = None,
) -> None:
    store.append_entries(
        run_id,
        RunLogAppendInput(
            entries=[
                RunLogEntryInput(
                    timestamp=datetime.now(timezone.utc),
                    level=level,
                    event=event,
                    message=f"event:{event}",
                    details=details or {},
                )
            ]
        ),
    )


def create_run(
    store: RunLogStore,
    requested: int = 4,
    *,
    terminal: bool = False,
) -> UUID:
    run_id = uuid4()
    store.create_run(
        run_id,
        RunLogCreateInput(requestedCount=requested, concurrency=2),
    )
    if terminal:
        append_event(
            store,
            run_id,
            "run_completed",
            details={
                "requested": requested,
                "pending": 0,
                "processed": requested,
                "succeeded": requested,
                "failed": 0,
            },
        )
    return run_id


def test_missing_log_directory_returns_empty_history(tmp_path: Path) -> None:
    client, log_dir, _ = client_for(tmp_path)
    response = client.get("/api/run-logs/runs")
    assert response.status_code == 200
    assert response.json() == []
    assert not log_dir.exists()


def test_internal_append_and_public_read_use_exact_jsonl_contract(tmp_path: Path) -> None:
    client, log_dir, store = client_for(tmp_path)
    run_id = create_run(store)
    append_event(
        store,
        run_id,
        "email_succeeded",
        details={"accountType": "free", "promotionEligible": True},
    )

    assert client.get("/api/run-logs/runs").json() == []
    active_log = client.get(f"/api/run-logs/runs/{run_id}")
    assert active_log.status_code == 200
    assert [entry["event"] for entry in active_log.json()["entries"]] == [
        "run_created",
        "email_succeeded",
    ]

    append_event(store, run_id, "run_completed")
    history = client.get("/api/run-logs/runs")
    assert history.status_code == 200
    assert history.json()[0]["runId"] == str(run_id)
    assert history.json()[0]["filename"] == f"run-{run_id}.jsonl"

    log_path = log_dir / f"run-{run_id}.jsonl"
    raw_log = log_path.read_text(encoding="utf-8")
    assert raw_log.count("\n") == 3
    for forbidden in [
        "chatgptPassword",
        "totpSecret",
        "accessUrl",
        "proxyPassword",
        "cookie",
        "token",
    ]:
        assert forbidden not in raw_log


def test_sensitive_detail_key_is_rejected_without_appending(tmp_path: Path) -> None:
    client, _, store = client_for(tmp_path)
    run_id = create_run(store)

    with pytest.raises(ValidationError):
        RunLogEntryInput(
            timestamp=datetime.now(timezone.utc),
            level="info",
            event="unsafe_entry",
            message="不安全字段测试",
            details={"totpSecret": "must-not-be-written"},
        )

    assert client.get(f"/api/run-logs/runs/{run_id}").json()["entryCount"] == 1


def test_retention_keeps_ten_terminal_runs_and_never_deletes_active_or_unknown(
    tmp_path: Path,
) -> None:
    client, log_dir, store = client_for(tmp_path)
    terminal_ids = [create_run(store, index + 1, terminal=True) for index in range(12)]
    active_id = create_run(store, 1)
    corrupt_id = uuid4()
    corrupt_path = log_dir / f"run-{corrupt_id}.jsonl"
    corrupt_path.write_bytes(b"{not-json\n")
    unknown_path = log_dir / "operator-notes.jsonl"
    unknown_path.write_text("keep me\n", encoding="utf-8")

    assert store.prune_terminal_runs() == 0
    history = client.get("/api/run-logs/runs")
    assert history.status_code == 200
    assert len(history.json()) == 10
    assert client.get(f"/api/run-logs/runs/{terminal_ids[0]}").status_code == 404
    assert client.get(f"/api/run-logs/runs/{terminal_ids[-1]}").status_code == 200
    assert client.get(f"/api/run-logs/runs/{active_id}").status_code == 200
    assert client.get(f"/api/run-logs/runs/{corrupt_id}").status_code == 500
    assert corrupt_path.read_bytes() == b"{not-json\n"
    assert unknown_path.read_text(encoding="utf-8") == "keep me\n"
    assert len(list(log_dir.glob("run-*.jsonl"))) == 12


def test_log_write_endpoints_are_not_public(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)
    run_id = uuid4()
    assert client.post(
        "/api/run-logs/runs",
        json={"requestedCount": 1, "concurrency": 2},
    ).status_code == 405
    assert client.post(
        f"/api/run-logs/runs/{run_id}/entries",
        json={"entries": []},
    ).status_code in {404, 405}
    assert client.get("/api/run-logs/runs/not-a-uuid").status_code == 422
