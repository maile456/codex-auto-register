"""Lifecycle manager for the vendored PayPal agreement sidecar.

The reference project owns its task registry and browser-facing UI, so it is
kept in a separate local process.  This module only starts that process on
demand and reports a small, stable status object to the existing FastAPI app.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from dotenv import dotenv_values


SOURCE_COMMIT = "4719066ec6fd56b57a5bd9599758366836c9dc0a"
SERVICE_NAME = "paypal-agreement-protocol"
PROXY_ENV_KEYS = (
    "IPROCKET_CHAIN_PROXY",
    "IPROCKET_BRIDGE_PORT",
    "IPROCKET_PRE_PROXY_HOST",
    "IPROCKET_PRE_PROXY_PORT",
)


class PaypalAgreementServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


Probe = Callable[[], tuple[bool, dict[str, Any] | None, str]]
PopenFactory = Callable[..., subprocess.Popen]


class PaypalAgreementService:
    """Start and stop the isolated reference Web UI on demand."""

    source_commit = SOURCE_COMMIT

    def __init__(
        self,
        *,
        vendor_dir: Path | None = None,
        host: str | None = None,
        port: int | None = None,
        startup_timeout: float | None = None,
        python_executable: str | None = None,
        log_path: Path | None = None,
        env_path: Path | None = None,
        probe: Probe | None = None,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        app_root = Path(__file__).resolve().parents[1]
        self.env_path = (env_path or app_root / ".env").resolve()
        self.vendor_dir = (vendor_dir or app_root / "backend" / "paypal_agreement_protocol").resolve()
        self.host = (host or os.getenv("PAP_HOST", "127.0.0.1")).strip() or "127.0.0.1"
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("PAP_HOST must be a loopback host")
        try:
            self.port = int(port or os.getenv("PAP_PORT", "18098"))
        except ValueError as exc:
            raise ValueError("PAP_PORT must be an integer") from exc
        if not 1024 <= self.port <= 65535:
            raise ValueError("PAP_PORT must be between 1024 and 65535")
        try:
            self.startup_timeout = float(
                startup_timeout or os.getenv("PAP_STARTUP_TIMEOUT_SECONDS", "20")
            )
        except ValueError as exc:
            raise ValueError("PAP_STARTUP_TIMEOUT_SECONDS must be numeric") from exc
        self.python_executable = (
            python_executable
            or os.getenv("PAP_PYTHON_EXECUTABLE", "").strip()
            or sys.executable
        )
        data_root = app_root.parent / "data" / "paypal-agreement"
        configured_log_path = os.getenv("PAP_LOG_PATH", "").strip()
        self.log_path = (log_path or (Path(configured_log_path) if configured_log_path else data_root / "sidecar.log")).resolve()
        self._probe_callback = probe
        self._popen_factory = popen_factory or subprocess.Popen
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_handle: Any | None = None
        self._last_error = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ui_path(self) -> str:
        return "/paypal-pay/"

    def _probe(self) -> tuple[bool, dict[str, Any] | None, str]:
        if self._probe_callback is not None:
            return self._probe_callback()
        try:
            response = httpx.get(
                f"{self.base_url}/api/health",
                timeout=0.8,
                follow_redirects=False,
                trust_env=False,
            )
            payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
            if response.status_code != 200:
                return False, payload if isinstance(payload, dict) else None, f"HTTP {response.status_code}"
            if not isinstance(payload, dict):
                return False, None, "health response is not an object"
            if payload.get("service") != SERVICE_NAME:
                return False, payload, "port is occupied by another service"
            return True, payload, ""
        except Exception as exc:
            return False, None, str(exc)

    def _process_exit(self) -> int | None:
        return self._process.poll() if self._process is not None else None

    def _status_locked(self) -> dict[str, Any]:
        running_process = self._process is not None and self._process_exit() is None
        ok, payload, probe_error = self._probe()
        if ok:
            return {
                "ok": True,
                "status": "online",
                "service": SERVICE_NAME,
                "sourceCommit": SOURCE_COMMIT,
                "host": self.host,
                "port": self.port,
                "uiPath": self.ui_path,
                "uiUrl": f"{self.ui_path}",
                "managed": bool(running_process),
                "pid": self._process.pid if running_process and self._process else None,
            }
        if running_process:
            return {
                "ok": False,
                "status": "starting",
                "service": SERVICE_NAME,
                "sourceCommit": SOURCE_COMMIT,
                "host": self.host,
                "port": self.port,
                "uiPath": self.ui_path,
                "uiUrl": self.ui_path,
                "managed": True,
                "pid": self._process.pid if self._process else None,
                "error": self._last_error or probe_error,
            }
        if self._process is not None:
            exit_code = self._process_exit()
            self._close_log_locked()
            self._process = None
            if exit_code not in (None, 0):
                self._last_error = f"sidecar exited with code {exit_code}"
        conflict = bool(payload and payload.get("service") != SERVICE_NAME)
        return {
            "ok": False,
            "status": "conflict" if conflict else ("failed" if self._last_error else "stopped"),
            "service": SERVICE_NAME,
            "sourceCommit": SOURCE_COMMIT,
            "host": self.host,
            "port": self.port,
            "uiPath": self.ui_path,
            "uiUrl": self.ui_path,
            "managed": False,
            "pid": None,
            "error": self._last_error or (probe_error if conflict else ""),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def _close_log_locked(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except Exception:
                    pass
        self._close_log_locked()

    def start(self) -> dict[str, Any]:
        with self._lock:
            current = self._status_locked()
            if current["status"] == "online":
                return current
            if current["status"] == "conflict":
                raise PaypalAgreementServiceError(
                    "port_in_use",
                    f"协议服务端口 {self.port} 已被其他服务占用",
                    409,
                )
            entry = self.vendor_dir / "web.py"
            if not entry.is_file():
                raise PaypalAgreementServiceError("vendor_missing", f"协议模块入口不存在：{entry}", 503)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            if self.env_path.is_file():
                configured_env = dotenv_values(self.env_path)
                for key in PROXY_ENV_KEYS:
                    value = configured_env.get(key)
                    if key not in os.environ and value is not None:
                        env[key] = str(value)
            env.setdefault("PAYPAL_WEB_PRODUCTION", "0")
            env.setdefault("PAYPAL_WEB_COOKIE_SECURE", "0")
            env.setdefault("PAYPAL_WEB_FULL_LOGS", os.getenv("PAP_FULL_LOGS", "0"))
            env.setdefault("PAYPAL_WEB_FULL_LOGS_UI", "0")
            env.setdefault("PAYPAL_WEB_ENABLE_PAY153_BRIDGE", "0")
            env.setdefault("PAYPAL_WEB_METRICS_PATH", str(self.log_path.parent / "protocol_metrics.json"))
            env.setdefault("PAYPAL_WEB_PAYMENT_AUDIT_PATH", str(self.log_path.parent / "payment_audit.jsonl"))
            env.setdefault("PAYPAL_WEB_PAYMENT_AUDIT_KEY_PATH", str(self.log_path.parent / ".payment_audit_hmac_key"))
            env.setdefault("PAYPAL_WEB_FULL_LOG_PATH", str(self.log_path.parent / "protocol_full.log"))
            browser_executable = os.getenv("PAP_BROWSER_EXECUTABLE", "").strip()
            if browser_executable:
                env["PAYPAL_BROWSER_EXECUTABLE"] = browser_executable
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            self._log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                self._process = self._popen_factory(
                    [self.python_executable, "-u", str(entry), "--host", self.host, "--port", str(self.port)],
                    cwd=str(self.vendor_dir),
                    env=env,
                    stdout=self._log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            except Exception as exc:
                self._close_log_locked()
                raise PaypalAgreementServiceError("start_failed", f"协议服务启动失败：{exc}", 503) from exc
            deadline = time.monotonic() + max(1.0, self.startup_timeout)
            while time.monotonic() < deadline:
                current = self._status_locked()
                if current["status"] == "online":
                    self._last_error = ""
                    return current
                if self._process_exit() is not None:
                    break
                time.sleep(0.1)
            exit_code = self._process_exit()
            self._last_error = (
                f"协议服务未在 {self.startup_timeout:g} 秒内就绪"
                if exit_code is None
                else f"协议服务启动后退出（code={exit_code}）"
            )
            self._stop_locked()
            raise PaypalAgreementServiceError("start_timeout", self._last_error, 503)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_locked()
            self._last_error = ""
            return self._status_locked()

    def close(self) -> None:
        self.stop()
