"""B5 (#979): grants on the record — the runtime-layer pieces.

Host-agnostic unit coverage (the deny-mode real-egress + fenced proofs run in the WSL live
gate). Covers the write-root grant registry + its live ``effective_write_roots`` propagation,
the deny-mode CONNECT gate on the chokepoint, and the fleet namespace→serving-child map that
completes the deferred B4 WRITER (#978 pt 5).
"""

from __future__ import annotations

import socket
import threading

import pytest

from clio_agent.runtime import net_chokepoint as nc
from clio_agent.runtime import sandbox_net, sandbox_roots


@pytest.fixture(autouse=True)
def _clean_registries():
    sandbox_roots.clear_write_root_grants()
    sandbox_net.clear_namespace_children()
    yield
    sandbox_roots.clear_write_root_grants()
    sandbox_net.clear_namespace_children()


# --------------------------------------------------------------------------- #
# Mid-session root grants → live ``effective_write_roots`` propagation (#979.3)
# --------------------------------------------------------------------------- #


def test_root_grant_widens_effective_write_roots_live(tmp_path) -> None:
    """A registered root grant appears in the next ``effective_write_roots`` — no restart."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    before = sandbox_roots.effective_write_roots(
        sandbox_roots.PROFILE_SHELL, workspace_root=str(ws)
    )
    assert outside.resolve() not in {r.resolve() for r in before}

    sandbox_roots.register_write_root_grant(str(ws), str(outside))

    after = sandbox_roots.effective_write_roots(sandbox_roots.PROFILE_SHELL, workspace_root=str(ws))
    assert outside.resolve() in {r.resolve() for r in after}
    # Scoped by workspace root: a DIFFERENT workspace does not inherit the grant.
    other = sandbox_roots.effective_write_roots(
        sandbox_roots.PROFILE_SHELL, workspace_root=str(tmp_path / "other")
    )
    assert outside.resolve() not in {r.resolve() for r in other}


def test_root_grant_is_idempotent_and_clearable(tmp_path) -> None:
    ws = tmp_path / "ws"
    granted = tmp_path / "g"
    sandbox_roots.register_write_root_grant(str(ws), str(granted))
    sandbox_roots.register_write_root_grant(str(ws), str(granted))  # idempotent
    assert list(sandbox_roots.granted_write_roots(str(ws))).count(granted.resolve()) == 1
    sandbox_roots.clear_write_root_grants()
    assert sandbox_roots.granted_write_roots(str(ws)) == ()


# --------------------------------------------------------------------------- #
# Deny-mode CONNECT gate on the chokepoint (#979.5)
# --------------------------------------------------------------------------- #


def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def _serve() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                conn.sendall(conn.recv(64) or b"")
        except OSError:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_no_gate_wired_is_allow_all_passthrough() -> None:
    """The default (no gate) is ALLOW + RECORD — a CONNECT tunnels through (B4 default)."""
    upstream, up_port = _echo_server()
    cp = nc.Chokepoint().start()
    try:
        client = socket.create_connection(("127.0.0.1", cp.port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in client.recv(128)
        client.close()
    finally:
        cp.stop()
        upstream.close()


def test_deny_gate_blocks_connect_with_403() -> None:
    """A gate returning ``deny`` refuses the CONNECT (403) and never dials upstream."""
    upstream, up_port = _echo_server()
    nc.set_egress_gate(lambda rec: "deny")
    cp = nc.Chokepoint().start()
    try:
        client = socket.create_connection(("127.0.0.1", cp.port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"403" in client.recv(128)
        client.close()
    finally:
        cp.stop()
        nc.set_egress_gate(None)
        upstream.close()


def test_allow_gate_lets_connect_through() -> None:
    upstream, up_port = _echo_server()
    seen: list[str] = []

    def _gate(rec: nc.EgressRecord) -> str:
        seen.append(rec.host)
        return "allow"

    nc.set_egress_gate(_gate)
    cp = nc.Chokepoint().start()
    try:
        client = socket.create_connection(("127.0.0.1", cp.port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in client.recv(128)
        client.close()
    finally:
        cp.stop()
        nc.set_egress_gate(None)
        upstream.close()
    assert seen == ["127.0.0.1"]  # the gate was consulted with the requested host


def test_gate_that_raises_fails_open() -> None:
    """A gate wiring BUG (raising) must never sever egress — fail open (B4 default)."""
    upstream, up_port = _echo_server()

    def _boom(rec: nc.EgressRecord) -> str:
        raise RuntimeError("wiring bug")

    nc.set_egress_gate(_boom)
    cp = nc.Chokepoint().start()
    try:
        client = socket.create_connection(("127.0.0.1", cp.port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in client.recv(128)
        client.close()
    finally:
        cp.stop()
        nc.set_egress_gate(None)
        upstream.close()


# --------------------------------------------------------------------------- #
# Fleet namespace → serving child map (the deferred B4 WRITER, #979.7)
# --------------------------------------------------------------------------- #


def test_namespace_child_register_and_resolve(tmp_path) -> None:
    root = str(tmp_path / "ws")
    sandbox_net.register_namespace_child(root, "geo", "child_geo")
    assert sandbox_net.resolve_namespace_child(root, "geo") == "child_geo"
    # Workspace-scoped: another workspace's identically-named namespace does not collide.
    assert sandbox_net.resolve_namespace_child(str(tmp_path / "other"), "geo") == ""
    # Empty child / namespace are no-ops (floor / built-in namespace).
    sandbox_net.register_namespace_child(root, "ndp", "")
    assert sandbox_net.resolve_namespace_child(root, "ndp") == ""
    assert sandbox_net.resolve_namespace_child(root, "") == ""
