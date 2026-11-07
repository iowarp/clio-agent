"""Per-child host allowlists on the chokepoint (LaTeX ledger L6).

A child registered with ``restrict_child_hosts`` reaches only those hosts, whatever the
workspace gate decides; other children are unaffected.
"""

from __future__ import annotations

import socket
import threading

from clio_agent.runtime import net_chokepoint as nc


def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)

    def _serve() -> None:
        for _ in range(2):
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.sendall(conn.recv(64) or b"")
            except OSError:
                return

    threading.Thread(target=_serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def _connect(port: int, target: str) -> bytes:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        return client.recv(128)
    finally:
        client.close()


def test_a_restricted_child_reaches_only_its_hosts() -> None:
    upstream, up_port = _echo_server()
    cp = nc.Chokepoint().start()
    try:
        build = cp.open_child_channel("child_build")
        other = cp.open_child_channel("child_other")
        nc.restrict_child_hosts("child_build", ["LOCALHOST"])
        # The build may reach only "localhost"; the same server by address is refused.
        assert b"403" in _connect(build, f"127.0.0.1:{up_port}")
        assert b"200" in _connect(build, f"localhost:{up_port}")
        # Another child is not limited by the build's list.
        assert b"200" in _connect(other, f"127.0.0.1:{up_port}")
    finally:
        nc.release_child_hosts("child_build")
        cp.stop()
        upstream.close()


def test_releasing_a_child_drops_its_allowlist() -> None:
    nc.restrict_child_hosts("child_x", ["relay.fullyjustified.net"])
    assert not nc._child_host_allowed("child_x", "example.org")
    nc.release_child_hosts("child_x")
    assert nc._child_host_allowed("child_x", "example.org")
    nc.release_child_hosts("child_x")  # idempotent
