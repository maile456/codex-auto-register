from __future__ import annotations

from backend.oai_iprocket_chain_bridge import Handler


class FakeSocket:
    def __init__(self, request: bytes) -> None:
        self._request = request
        self.responses: list[bytes] = []

    def settimeout(self, _seconds) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        request, self._request = self._request, b""
        return request

    def sendall(self, data: bytes) -> None:
        self.responses.append(data)


def test_bridge_requests_proxy_authentication_before_connecting() -> None:
    request = FakeSocket(
        b"CONNECT example.test:443 HTTP/1.1\r\n"
        b"Host: example.test:443\r\n\r\n"
    )

    Handler(request, ("127.0.0.1", 12345), object())

    response = b"".join(request.responses)
    assert response.startswith(b"HTTP/1.1 407 Proxy Authentication Required\r\n")
    assert b"Proxy-Authenticate: Basic" in response
