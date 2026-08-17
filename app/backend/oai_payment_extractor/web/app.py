from __future__ import annotations

import json
import os
import queue
from typing import Any, Mapping

from flask import Flask, render_template, request
from flask_sock import Sock

from .auth import (
    WEBSOCKET_AUTH_TIMEOUT_SECONDS,
    request_is_authorized,
    unauthorized_response,
    websocket_auth_message,
)
from .events import make_event
from .env import load_configured_env
from .routes import register_routes
from .tasks import TaskManager
from ..logging_utils import configure_logging


def create_app(
    test_config: Mapping[str, Any] | None = None,
    *,
    task_manager: TaskManager | None = None,
) -> Flask:
    load_configured_env()
    app = Flask(__name__)
    app.config.from_mapping(
        TASK_WORKERS=_int_env("OPLL_TASK_WORKERS", 2),
        TASK_TTL_SECONDS=_int_env("OPLL_TASK_TTL_SECONDS", 3600),
        TASK_EVENT_HISTORY_SIZE=_int_env("OPLL_TASK_EVENT_HISTORY_SIZE", 500),
        WEB_PASSWORD=os.getenv("OPLL_WEB_PASSWORD", ""),
        SSL_CERT_FILE=os.getenv("OPLL_SSL_CERT_FILE", ""),
        SSL_KEY_FILE=os.getenv("OPLL_SSL_KEY_FILE", ""),
        LOG_LEVEL=os.getenv("OPLL_LOG_LEVEL", "INFO"),
        LOG_FILE=os.getenv("OPLL_LOG_FILE", ""),
        LOG_JSON=os.getenv("OPLL_LOG_JSON", "false").lower() in {"1", "true", "yes"},
    )
    if test_config:
        app.config.update(test_config)
    if app.config.get("TESTING") and not app.config.get("WEB_PASSWORD"):
        app.config["WEB_PASSWORD"] = "test-password"
    configure_logging(
        level=str(app.config["LOG_LEVEL"]),
        log_file=str(app.config["LOG_FILE"]),
        serialize=bool(app.config["LOG_JSON"]),
    )
    manager = task_manager or TaskManager(
        max_workers=int(app.config["TASK_WORKERS"]),
        ttl_seconds=int(app.config["TASK_TTL_SECONDS"]),
        history_size=int(app.config["TASK_EVENT_HISTORY_SIZE"]),
    )
    app.extensions["payment_task_manager"] = manager
    register_routes(app, manager)

    @app.before_request
    def require_api_password() -> Any:
        if request.path.startswith("/api/") and not request_is_authorized(str(app.config["WEB_PASSWORD"])):
            return unauthorized_response()

    @app.get("/")
    def workbench() -> str:
        return render_template("index.html")

    sock = Sock(app)
    _register_websocket(sock, manager, str(app.config["WEB_PASSWORD"]))
    return app


def _register_websocket(sock: Sock, manager: TaskManager, password: str) -> None:
    @sock.route("/ws/tasks")
    def task_stream(ws: Any) -> None:
        try:
            auth_message = ws.receive(timeout=WEBSOCKET_AUTH_TIMEOUT_SECONDS)
        except Exception:
            return
        if not websocket_auth_message(auth_message, password):
            try:
                ws.send(json.dumps({"type": "auth.failed", "data": {"error": "unauthorized"}}))
                ws.close()
            except Exception:
                pass
            return

        try:
            ws.send(json.dumps({"type": "auth.ok"}))
        except Exception:
            return

        history, subscriber = manager.subscribe()
        try:
            for event in history:
                ws.send(json.dumps(event, ensure_ascii=False))
            while True:
                try:
                    event = subscriber.get(timeout=15)
                except queue.Empty:
                    event = make_event("", "task.ping")
                ws.send(json.dumps(event, ensure_ascii=False))
        except Exception:
            # A closed browser socket must not affect task execution.
            return
        finally:
            manager.unsubscribe(subscriber)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default
