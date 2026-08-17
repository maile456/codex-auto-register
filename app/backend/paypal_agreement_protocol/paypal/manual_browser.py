from __future__ import annotations

import itertools
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from paypal.proxy import ProxyConfig


_DISPLAY_COUNTER = itertools.count(120)
_DISPLAY_LOCK = threading.Lock()
_BROWSER_LIMIT = max(1, min(int(os.getenv("PAYPAL_MANUAL_BROWSER_LIMIT", "2") or 2), 4))
_BROWSER_SEMAPHORE = threading.BoundedSemaphore(_BROWSER_LIMIT)


@dataclass
class BrowserState:
    ready: bool = False
    done: bool = False
    error: str = ""
    message: str = "正在启动临时 Chromium…"
    current_url: str = ""
    http_status: int = 0


class ManualBrowserController:
    """Thread-owned Playwright browser controlled through queued UI actions."""

    def __init__(
        self,
        *,
        proxy_config: ProxyConfig,
        user_agent: str,
        cookies: list[dict[str, Any]],
        start_url: str,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        locale: str = "pt-BR",
        timezone_id: str = "America/Sao_Paulo",
        completion_mode: str = "captcha",
    ) -> None:
        self.proxy_config = proxy_config
        self.user_agent = user_agent
        self.initial_cookies = list(cookies)
        self.start_url = start_url
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.locale = str(locale or "pt-BR")
        self.timezone_id = str(timezone_id or "America/Sao_Paulo")
        self.completion_mode = str(completion_mode or "captcha").lower()
        self.initial_datadome = next(
            (
                str(cookie.get("value") or "")
                for cookie in self.initial_cookies
                if str(cookie.get("name") or "").lower() == "datadome"
            ),
            "",
        )

        self._lock = threading.RLock()
        self._state = BrowserState()
        self._frame = b""
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.result_cookies: list[dict[str, Any]] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="paypal-manual-browser", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._commands.put_nowait({"type": "stop"})
        except queue.Full:
            pass

    def wait(self, timeout: float, cancel_event: threading.Event | None = None) -> list[dict[str, Any]]:
        deadline = time.time() + timeout
        while not self._done_event.wait(0.25):
            if cancel_event is not None and cancel_event.is_set():
                self.stop()
                raise RuntimeError("Task cancelled")
            if time.time() >= deadline:
                self.stop()
                raise TimeoutError("Waiting for manual browser verification timed out")
        state = self.state()
        if state.error:
            raise RuntimeError(state.error)
        if not self.result_cookies:
            raise RuntimeError("Manual browser finished without reusable cookies")
        return list(self.result_cookies)

    def state(self) -> BrowserState:
        with self._lock:
            return BrowserState(**self._state.__dict__)

    def frame(self) -> bytes:
        with self._lock:
            return bytes(self._frame)

    def action(self, payload: dict[str, Any]) -> None:
        action_type = str(payload.get("type") or "").strip().lower()
        if action_type not in {"click", "text", "key", "reload", "scroll", "finish"}:
            raise ValueError("Unsupported browser action")
        command = dict(payload)
        command["type"] = action_type
        try:
            self._commands.put_nowait(command)
        except queue.Full as exc:
            raise ValueError("Browser action queue is full") from exc

    def _set_state(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self._state, key, value)

    @staticmethod
    def _playwright_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in cookies:
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            domain = str(item.get("domain") or "")
            path = str(item.get("path") or "/") or "/"
            if not name or not domain:
                continue
            cookie: dict[str, Any] = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": bool(item.get("secure", True)),
                "httpOnly": bool(item.get("httpOnly", False)),
            }
            expires = item.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                cookie["expires"] = float(expires)
            result.append(cookie)
        return result

    @staticmethod
    def _proxy_settings(config: ProxyConfig) -> dict[str, str] | None:
        entry = config.entry if config and config.enabled else None
        if entry is None:
            return None
        scheme = "socks5" if entry.scheme == "socks5h" else entry.scheme
        settings = {"server": f"{scheme}://{entry.host}:{entry.port}"}
        if entry.username:
            settings["username"] = entry.username
        if entry.password:
            settings["password"] = entry.password
        return settings

    @staticmethod
    def _start_socks5_http_bridge(config: ProxyConfig) -> tuple[dict[str, str] | None, subprocess.Popen | None, str]:
        """Convert authenticated SOCKS5 to a per-task loopback HTTP proxy for Chromium."""
        entry = config.entry if config and config.enabled else None
        if entry is None or entry.scheme not in {"socks5", "socks5h"} or not (entry.username or entry.password):
            return ManualBrowserController._proxy_settings(config), None, ""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            local_port = int(listener.getsockname()[1])

        auth_file = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="paypal-socks-auth-", delete=False
        )
        try:
            auth_file.write(f"{entry.username}:{entry.password}")
            auth_file.flush()
        finally:
            auth_file.close()
        try:
            os.chmod(auth_file.name, 0o600)
        except OSError:
            pass

        process = subprocess.Popen(
            [
                sys.executable, "-m", "pproxy",
                "-l", f"http://127.0.0.1:{local_port}",
                "-r", f"socks5://{entry.host}:{entry.port}##{auth_file.name}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if process.poll() is not None:
                try:
                    os.unlink(auth_file.name)
                except OSError:
                    pass
                raise RuntimeError("SOCKS5 browser proxy bridge failed to start")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.2):
                    return {"server": f"http://127.0.0.1:{local_port}"}, process, auth_file.name
            except OSError:
                time.sleep(0.1)
        process.terminate()
        try:
            os.unlink(auth_file.name)
        except OSError:
            pass
        raise RuntimeError("SOCKS5 browser proxy bridge startup timed out")

    @staticmethod
    def _cookie_value(cookies: list[dict[str, Any]], name: str) -> str:
        target = name.lower()
        for cookie in cookies:
            if str(cookie.get("name") or "").lower() == target:
                return str(cookie.get("value") or "")
        return ""

    def _completion_ready(self, cookies: list[dict[str, Any]], current_url: str, http_status: int) -> bool:
        hostname = (urlparse(current_url).hostname or "").lower()
        if self.completion_mode == "agreement":
            return hostname in {"pm-redirects.stripe.com", "pay.openai.com", "chatgpt.com"}
        current_datadome = self._cookie_value(cookies, "datadome")
        return bool(
            current_datadome
            and current_datadome != self.initial_datadome
            and hostname.endswith("paypal.com")
            and 200 <= int(http_status or 0) < 400
        )

    def _run(self) -> None:
        xvfb: subprocess.Popen | None = None
        browser = None
        proxy_bridge: subprocess.Popen | None = None
        proxy_auth_file = ""
        acquired = False
        try:
            self._set_state(message="等待临时浏览器资源…")
            while not self._stop_event.is_set():
                if _BROWSER_SEMAPHORE.acquire(timeout=0.5):
                    acquired = True
                    break
            if not acquired:
                self._set_state(done=True, error="Task cancelled", message="????????????")
                self._done_event.set()
                return

            display = ""
            if os.name != "nt":
                with _DISPLAY_LOCK:
                    display_number = next(_DISPLAY_COUNTER)
                display = f":{display_number}"
                xvfb = subprocess.Popen(
                    ["Xvfb", display, "-screen", "0", f"{self.viewport_width}x{self.viewport_height}x24", "-nolisten", "tcp", "-ac"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.35)
                if xvfb.poll() is not None:
                    raise RuntimeError("Xvfb failed to start")

            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright

            self._set_state(message="正在通过任务代理打开验证页面…")
            with sync_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {
                    # Windows has no Xvfb; a headless browser still provides
                    # the screenshot/control surface used by the web UI.
                    "headless": os.name == "nt",
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-background-networking",
                        "--window-size=1280,800",
                    ],
                }
                browser_executable = os.getenv("PAYPAL_BROWSER_EXECUTABLE", "").strip()
                if browser_executable:
                    launch_kwargs["executable_path"] = browser_executable
                elif os.name == "nt":
                    # Playwright may install full Chromium without the separate
                    # chromium_headless_shell package.  Point at the available
                    # full browser explicitly so headless verification works.
                    browser_candidates = [
                        playwright.chromium.executable_path,
                        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
                        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
                        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                        os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
                        os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
                    ]
                    available_executable = next(
                        (candidate for candidate in browser_candidates if candidate and os.path.isfile(candidate)),
                        "",
                    )
                    if available_executable:
                        launch_kwargs["executable_path"] = available_executable
                elif os.name != "nt":
                    launch_kwargs["executable_path"] = "/usr/bin/chromium"
                if display:
                    launch_kwargs["env"] = {**os.environ, "DISPLAY": display}
                proxy, proxy_bridge, proxy_auth_file = self._start_socks5_http_bridge(self.proxy_config)
                if proxy:
                    launch_kwargs["proxy"] = proxy
                browser = playwright.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": self.viewport_width, "height": self.viewport_height},
                    locale=self.locale,
                    timezone_id=self.timezone_id,
                    ignore_https_errors=False,
                )
                cookies = self._playwright_cookies(self.initial_cookies)
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                navigation = {"status": 0}

                def remember_navigation(response) -> None:
                    try:
                        if response.request.is_navigation_request() and response.frame == page.main_frame:
                            navigation["status"] = int(response.status)
                            self._set_state(http_status=int(response.status), current_url=page.url)
                    except Exception:
                        pass

                page.on("response", remember_navigation)
                try:
                    response = page.goto(self.start_url, wait_until="commit", timeout=20000)
                    if response is not None:
                        navigation["status"] = int(response.status)
                except PlaywrightTimeoutError:
                    self._set_state(message="验证页面加载较慢，浏览器画面仍可继续操作")
                self._set_state(ready=True, message="临时浏览器已打开，请在画面中完成验证")

                last_frame_at = 0.0
                while not self._stop_event.is_set():
                    self._set_state(current_url=page.url)
                    try:
                        command = self._commands.get(timeout=0.12)
                    except queue.Empty:
                        command = None
                    if command:
                        command_type = command.get("type")
                        if command_type == "stop":
                            break
                        if command_type == "click":
                            x = max(0.0, min(float(command.get("x", 0)), self.viewport_width))
                            y = max(0.0, min(float(command.get("y", 0)), self.viewport_height))
                            page.mouse.click(x, y)
                        elif command_type == "text":
                            value = str(command.get("value") or "")[:500]
                            page.keyboard.type(value, delay=20)
                        elif command_type == "key":
                            key = str(command.get("key") or "")
                            if key in {"Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"}:
                                page.keyboard.press(key)
                        elif command_type == "reload":
                            page.reload(wait_until="domcontentloaded", timeout=45000)
                        elif command_type == "scroll":
                            page.mouse.wheel(0, max(-1200, min(int(command.get("delta_y", 500)), 1200)))
                        elif command_type == "finish":
                            current_cookies = context.cookies()
                            if self._completion_ready(current_cookies, page.url, int(navigation.get("status") or 0)):
                                self.result_cookies = current_cookies
                                self._set_state(done=True, message="验证 Cookie 已取得")
                                self._done_event.set()
                                break
                            self._set_state(
                                message=(
                                    "验证页面仍返回 403，请先在画面中完成验证"
                                    if int(navigation.get("status") or 0) == 403
                                    else "尚未检测到通过验证后的 PayPal 会话，请继续操作"
                                )
                            )

                    now = time.time()
                    if now - last_frame_at >= 0.65:
                        try:
                            frame = page.screenshot(type="jpeg", quality=68, full_page=False, timeout=8000)
                            with self._lock:
                                self._frame = frame
                        except Exception as screenshot_error:
                            self._set_state(message=f"画面生成中：{str(screenshot_error)[:120]}")
                        last_frame_at = now

                    current_cookies = context.cookies()
                    if self._completion_ready(current_cookies, page.url, int(navigation.get("status") or 0)):
                        self.result_cookies = current_cookies
                        self._set_state(done=True, message="验证已完成，正在同步 Cookie")
                        self._done_event.set()
                        break
        except Exception as exc:
            self._set_state(done=True, error=str(exc), message="临时浏览器运行失败")
            self._done_event.set()
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            if xvfb is not None:
                try:
                    xvfb.terminate()
                    xvfb.wait(timeout=3)
                except Exception:
                    try:
                        xvfb.kill()
                    except Exception:
                        pass
            if proxy_bridge is not None:
                try:
                    proxy_bridge.terminate()
                    proxy_bridge.wait(timeout=3)
                except Exception:
                    try:
                        proxy_bridge.kill()
                    except Exception:
                        pass
            if proxy_auth_file:
                try:
                    os.unlink(proxy_auth_file)
                except OSError:
                    pass
            if acquired:
                _BROWSER_SEMAPHORE.release()
