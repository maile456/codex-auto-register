from __future__ import annotations

import os
import base64
import select
import socket
import socketserver
import struct
import threading
import time
import urllib.request
from pathlib import Path

from dotenv import dotenv_values


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
_FILE_ENV = dotenv_values(_ENV_FILE) if _ENV_FILE.is_file() else {}


def configured_value(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        value = _FILE_ENV.get(name)
    return str(value) if value not in (None, "") else default


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(configured_value("IPROCKET_BRIDGE_PORT", "18796"))
LOCAL_SOCKS_HOST = configured_value("IPROCKET_PRE_PROXY_HOST", "127.0.0.1")
LOCAL_SOCKS_PORT = int(configured_value("IPROCKET_PRE_PROXY_PORT", "3251"))
SOURCE_URL = configured_value(
    "OPLL_PROXY_SOURCE_URL",
    "",
)

_credential_lock = threading.Lock()
_credential: tuple[str, int, str, str] | None = None
_credential_time = 0.0
_server_lock = threading.Lock()
_background_server: "Server | None" = None
_background_thread: threading.Thread | None = None


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed during SOCKS handshake")
        data += chunk
    return data


def socks_connect(sock: socket.socket, host: str, port: int, username: str = "", password: str = "") -> None:
    if username:
        sock.sendall(b"\x05\x01\x02")
        if recv_exact(sock, 2) != b"\x05\x02":
            raise ConnectionError("SOCKS authentication method rejected")
        user = username.encode("utf-8")
        secret = password.encode("utf-8")
        if len(user) > 255 or len(secret) > 255:
            raise ValueError("SOCKS credentials are too long")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        if recv_exact(sock, 2) != b"\x01\x00":
            raise ConnectionError("SOCKS authentication failed")
    else:
        sock.sendall(b"\x05\x01\x00")
        if recv_exact(sock, 2) != b"\x05\x00":
            raise ConnectionError("local SOCKS proxy rejected connection")

    encoded_host = host.encode("idna")
    if len(encoded_host) > 255:
        raise ValueError("destination hostname is too long")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(encoded_host)]) + encoded_host + struct.pack("!H", port))
    header = recv_exact(sock, 4)
    if header[1] != 0:
        raise ConnectionError(f"SOCKS connect failed ({header[1]})")
    address_type = header[3]
    if address_type == 1:
        address_size = 4
    elif address_type == 4:
        address_size = 16
    elif address_type == 3:
        address_size = recv_exact(sock, 1)[0]
    else:
        raise ConnectionError("invalid SOCKS response")
    recv_exact(sock, address_size + 2)


def http_proxy_connect(sock: socket.socket, host: str, port: int, username: str, password: str) -> None:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Proxy-Authorization: Basic {token}\r\n"
        "Proxy-Connection: Keep-Alive\r\n\r\n"
    ).encode("latin-1")
    sock.sendall(request)
    response = b""
    while b"\r\n\r\n" not in response and len(response) < 65536:
        response += sock.recv(4096)
    status_line = response.split(b"\r\n", 1)[0]
    if b" 200 " not in status_line:
        raise ConnectionError("HTTP upstream proxy CONNECT rejected")


def load_credential(force: bool = False) -> tuple[str, int, str, str]:
    global _credential, _credential_time
    with _credential_lock:
        if _credential and not force and time.time() - _credential_time < 60:
            return _credential
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            lines = [line.strip() for line in response.read(1024 * 1024).decode("utf-8").splitlines() if line.strip()]
        if not lines:
            raise ConnectionError("IPRocket subscription returned no proxy")
        host, port, username, password = lines[0].split(":", 3)
        _credential = host, int(port), username, password
        _credential_time = time.time()
        return _credential


def open_chain(
    destination_host: str,
    destination_port: int,
    credential: tuple[str, str, int, str, str] | None = None,
) -> socket.socket:
    if credential:
        protocol, proxy_host, proxy_port, username, password = credential
    else:
        proxy_host, proxy_port, username, password = load_credential()
        protocol = "socks5" if proxy_port in {9595, 59999, 619999} else "http"
    upstream: socket.socket | None = None
    try:
        try:
            upstream = socket.create_connection((LOCAL_SOCKS_HOST, LOCAL_SOCKS_PORT), timeout=15)
            upstream.settimeout(30)
            socks_connect(upstream, proxy_host, proxy_port)
        except (ConnectionError, OSError):
            # A pasted 1024proxy URL can be used directly when no local
            # pre-proxy is configured. Keep the chained route when available.
            if upstream is not None:
                upstream.close()
            upstream = socket.create_connection((proxy_host, proxy_port), timeout=15)
            upstream.settimeout(30)
        if protocol == "http":
            http_proxy_connect(upstream, destination_host, destination_port, username, password)
        else:
            socks_connect(upstream, destination_host, destination_port, username, password)
        upstream.settimeout(None)
        return upstream
    except Exception:
        if upstream is not None:
            upstream.close()
        raise


def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], 60)
        if not readable:
            continue
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(15)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            part = self.request.recv(4096)
            if not part:
                return
            data += part
        header_text = data.decode("latin-1", errors="replace")
        first_line = header_text.split("\r\n", 1)[0]
        method, target, _ = (first_line.split(" ", 2) + ["", ""])[:3]
        if method.upper() != "CONNECT":
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            return
        host, separator, port_text = target.rpartition(":")
        if not separator:
            host, port_text = target, "443"
        dynamic_credential = None
        for header_line in header_text.split("\r\n")[1:]:
            if not header_line.lower().startswith("proxy-authorization: basic "):
                continue
            try:
                encoded = header_line.split(None, 2)[2].strip()
                bridge_user, bridge_password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
                if bridge_user.startswith("iprb_"):
                    metadata = bridge_user[5:]
                    metadata += "=" * ((4 - len(metadata) % 4) % 4)
                    decoded = base64.urlsafe_b64decode(metadata).decode("utf-8")
                    protocol, proxy_host, proxy_port, username = decoded.split("|", 3)
                    dynamic_credential = protocol, proxy_host, int(proxy_port), username, bridge_password
            except Exception:
                dynamic_credential = None
            break
        if dynamic_credential is None:
            self.request.sendall(
                b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                b'Proxy-Authenticate: Basic realm="autoregister-bridge"\r\n'
                b'Connection: close\r\n\r\n'
            )
            return
        upstream = open_chain(host.strip("[]"), int(port_text), dynamic_credential)
        try:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.request.settimeout(None)
            relay(self.request, upstream)
        finally:
            upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def ensure_background_server() -> bool:
    """Start the dynamic-credential bridge once without requiring a subscription URL."""
    global _background_server, _background_thread
    with _server_lock:
        if _background_thread is not None and _background_thread.is_alive():
            return False
        try:
            probe = socket.create_connection((LISTEN_HOST, LISTEN_PORT), timeout=0.2)
        except OSError:
            probe = None
        if probe is not None:
            probe.close()
            return False
        server = Server((LISTEN_HOST, LISTEN_PORT), Handler)
        thread = threading.Thread(
            target=server.serve_forever,
            name="oai-iprocket-bridge",
            daemon=True,
        )
        thread.start()
        _background_server = server
        _background_thread = thread
        return True


def stop_background_server() -> None:
    global _background_server, _background_thread
    with _server_lock:
        server = _background_server
        thread = _background_thread
        _background_server = None
        _background_thread = None
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None:
        thread.join(timeout=2)


if __name__ == "__main__":
    # A pasted proxy pool can carry its upstream credentials in the bridge
    # CONNECT authorization header.  In that mode no subscription URL is
    # required; load the default credential only when a source is configured.
    if SOURCE_URL.strip():
        load_credential(force=True)
    with Server((LISTEN_HOST, LISTEN_PORT), Handler) as server:
        server.serve_forever()
