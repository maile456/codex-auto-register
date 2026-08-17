from __future__ import annotations

from backend.oai_payment_extractor import logging_utils


def test_logging_queue_can_be_disabled_for_restricted_windows_sessions(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("OPLL_LOG_ENQUEUE", "false")
    monkeypatch.setattr(
        logging_utils.logger,
        "configure",
        lambda **kwargs: captured.update(kwargs),
    )

    logging_utils.configure_logging(force=True)

    handlers = captured["handlers"]
    assert isinstance(handlers, list)
    assert handlers[0]["enqueue"] is False
