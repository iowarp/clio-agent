"""The egress chokepoint chains through the site proxy CLIO itself was given (no direct egress)."""

from __future__ import annotations

import socket
import threading

import pytest

from clio_agent.runtime import net_chokepoint as nc


def _fake_proxy(status: bytes) -> tuple[int, list[bytes]]:
    """One-shot proxy: records the CONNECT head, answers ``status``, then echoes bytes."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    seen: list[bytes] = []

    def serve() -> None:
        conn, _ = server.accept()
        with conn, server:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1024)
            seen.append(head)
            conn.sendall(b"HTTP/1.1 " + status + b"\r\n\r\n")
            if status.startswith(b"200"):
                conn.sendall(conn.recv(1024))

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], seen


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.upper(), raising=False)


def test_connect_is_chained_through_the_site_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    port, seen = _fake_proxy(b"200 Connection established")
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{port}")
    with nc._dial_tunnel("github.com", 443) as tunnel:
        tunnel.sendall(b"ping")
        assert tunnel.recv(4) == b"ping"
    assert seen[0].startswith(b"CONNECT github.com:443 HTTP/1.1\r\n")


def test_a_refused_connect_fails_the_dial(monkeypatch: pytest.MonkeyPatch) -> None:
    port, _seen = _fake_proxy(b"403 Forbidden")
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{port}")
    with pytest.raises(OSError, match="refused CONNECT"):
        nc._dial_tunnel("github.com", 443)


def test_no_proxy_hosts_and_unset_proxies_dial_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    assert nc._upstream_proxy("github.com", "https") is None
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,.internal")
    assert nc._upstream_proxy("github.com", "https") == ("proxy.example", 3128)
    assert nc._upstream_proxy("data.internal", "https") is None
    assert nc._upstream_proxy("127.0.0.1", "http") is None
