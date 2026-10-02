"""The chokepoint never cuts a slow response silently (#1577 section 4).

The upstream dial is bounded (``_CONNECT_READ_TIMEOUT_S``), but once the tunnel is up the
pump waits on the peers: a server that takes longer than that bound to answer is relayed
in full. A tunnel that does break mid-flight is logged with a typed reason instead of
being swallowed. Driven against the REAL chokepoint over loopback sockets.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

import pytest

from clio_agent.runtime import net_chokepoint as nc


def _origin(reply_after_s: float, *, reset: bool = False) -> tuple[socket.socket, int]:
    """One-shot upstream: read the request, wait, then answer (or reset the connection)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def _serve() -> None:
        try:
            conn, _ = srv.accept()
            conn.recv(64)
            time.sleep(reply_after_s)
            if reset:
                # SO_LINGER 0 -> close sends RST: the tunnel breaks mid-flight.
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                conn.close()
                return
            conn.sendall(b"late reply")
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def _tunnel(port: int, up_port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=10)
    client.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
    assert b"200" in client.recv(128)
    return client


def _read_all(client: socket.socket) -> bytes:
    data = b""
    try:
        while chunk := client.recv(64):
            data += chunk
    except OSError:
        pass
    return data


def test_a_reply_slower_than_the_dial_bound_is_relayed_in_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upstream answers 1.5 s after the request with a 0.5 s dial bound: relayed.

    SABOTAGE: drop ``upstream.settimeout(None)`` (the upstream keeps the dial timeout
    for the tunnel) -> its recv times out at 0.5 s, the client reads EOF and no reply
    -> red.
    """
    monkeypatch.setattr(nc, "_CONNECT_READ_TIMEOUT_S", 0.5)
    upstream, up_port = _origin(1.5)
    cp = nc.Chokepoint().start()
    try:
        client = _tunnel(cp.port, up_port)
        client.sendall(b"request")
        assert _read_all(client) == b"late reply"
        client.close()
    finally:
        cp.stop()
        upstream.close()


def test_a_tunnel_broken_mid_flight_is_logged_with_a_typed_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The upstream resets the connection: a WARNING naming the cut, never silence.

    SABOTAGE: restore ``except OSError: pass`` in ``_copy`` -> no record -> red.
    """
    upstream, up_port = _origin(0.2, reset=True)
    cp = nc.Chokepoint().start()
    try:
        with caplog.at_level(logging.WARNING, logger=nc.__name__):
            client = _tunnel(cp.port, up_port)
            client.sendall(b"request")
            _read_all(client)
            client.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(
                "reason=tunnel_transport_error" in r.getMessage() for r in caplog.records
            ):
                time.sleep(0.05)
    finally:
        cp.stop()
        upstream.close()
    cut = [r for r in caplog.records if "reason=tunnel_transport_error" in r.getMessage()]
    assert cut, [r.getMessage() for r in caplog.records]
    assert "upstream->client" in cut[0].getMessage()
    assert "127.0.0.1" in cut[0].getMessage()
