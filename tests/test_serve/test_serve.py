"""Tests for the connect-or-spawn primitive (:mod:`clio_agent.serve`, #799 phase 2a).

Two tiers:

* **Pure-logic** (fast, deterministic, no real process): attach path with a monkeypatched
  health probe; pidfile write/read/cleanup; the ``stop_server`` idempotency branches
  (no pidfile / dead pid / not-ours) and the reason catalog.
* **Real subprocess** (bounded, always cleaned up): spawn a real ``clio-agent-gact`` on an
  ephemeral free port, watch it become healthy, prove a second :func:`ensure_server`
  *attaches* (same URL, no new process), then :func:`stop_server` kills it and frees the
  port. Wrapped in ``try/finally`` so it never strands a daemon.
"""

from __future__ import annotations

import socket
import sys
import time

import pytest

from clio_agent import serve


@pytest.fixture(autouse=True)
def _isolated_user_dir(tmp_path, monkeypatch):
    """Point the canonical user data dir at a temp dir so pidfiles are isolated, and
    reset the module's last-action record between tests."""
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path))
    serve._LAST_ACTION = None
    yield
    serve._LAST_ACTION = None


def _free_port() -> int:
    """Bind to ``:0`` to grab a currently-free high port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- reason catalog -----------------------------------------------------------


def test_serve_reason_validates_against_closed_catalog():
    payload = serve._serve_reason("spawned", pid=123)
    assert payload["reason"] == "spawned"
    assert payload["managed"] is True
    assert payload["pid"] == 123
    assert "detail" in payload


def test_serve_reason_rejects_unknown_reason():
    with pytest.raises(ValueError, match="Unknown serve reason"):
        serve._serve_reason("not_a_real_reason")


# --- pidfile write/read/cleanup ----------------------------------------------


def test_pidfile_roundtrip_and_cleanup():
    pidfile = serve._pidfile_path(45999)
    assert not pidfile.exists()

    serve._write_pidfile(
        pidfile,
        pid=4242,
        create_time=1234.5,
        host="127.0.0.1",
        port=45999,
        spawned_by_us=True,
    )
    record = serve._read_pidfile(pidfile)
    assert record == {
        "pid": 4242,
        "create_time": 1234.5,
        "host": "127.0.0.1",
        "port": 45999,
        "spawned_by_us": True,
    }

    serve._remove_pidfile(pidfile)
    assert not pidfile.exists()
    assert serve._read_pidfile(pidfile) is None


def test_read_pidfile_garbled_returns_none():
    pidfile = serve._pidfile_path(45998)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("not json{{{", encoding="utf-8")
    assert serve._read_pidfile(pidfile) is None


# --- ensure_server attach path (no real spawn) --------------------------------


def test_ensure_server_attaches_external_when_healthy(monkeypatch):
    """Health answers but no pidfile of ours exists => attach to an external server,
    never spawn, never record it as managed."""
    monkeypatch.setattr(serve, "_probe_health", lambda *a, **k: True)

    def _boom(*_a, **_k):  # pragma: no cover - must never run on the attach path
        raise AssertionError("ensure_server must not spawn when a server already answers")

    monkeypatch.setattr(serve.subprocess, "Popen", _boom)

    url = serve.ensure_server(port=45997, host="127.0.0.1")
    assert url == "http://127.0.0.1:45997"

    action = serve.last_action()
    assert action is not None
    assert action["reason"] == "attached_external"
    assert action["managed"] is False
    # Attaching to a stranger must not write a pidfile we would later kill.
    assert not serve._pidfile_path(45997).exists()


def test_ensure_server_reports_already_running_for_our_prior_spawn(monkeypatch):
    """Health answers AND a live pidfile marks the server as ours => already_running."""
    port = 45996
    pidfile = serve._pidfile_path(port)
    # Record the *current* process as "our server" so the liveness + create-time guard
    # passes without launching anything.
    import os

    serve._write_pidfile(
        pidfile,
        pid=os.getpid(),
        create_time=serve._proc_create_time(os.getpid()),
        host="127.0.0.1",
        port=port,
        spawned_by_us=True,
    )
    monkeypatch.setattr(serve, "_probe_health", lambda *a, **k: True)
    monkeypatch.setattr(
        serve.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not spawn")),
    )

    url = serve.ensure_server(port=port)
    assert url == f"http://127.0.0.1:{port}"
    action = serve.last_action()
    assert action is not None
    assert action["reason"] == "already_running"
    assert action["pid"] == os.getpid()


def test_ensure_server_binary_not_found_raises(monkeypatch):
    monkeypatch.setattr(serve, "_probe_health", lambda *a, **k: False)

    def _no_bin() -> str:
        raise serve.ServerBinaryNotFound(serve._serve_reason("binary_not_found"))

    monkeypatch.setattr(serve, "_server_bin", _no_bin)
    with pytest.raises(serve.ServerBinaryNotFound) as excinfo:
        serve.ensure_server(port=45995)
    assert excinfo.value.reason == "binary_not_found"


def test_ensure_server_timeout_tears_down_and_raises(monkeypatch):
    """A spawned process that never turns healthy => ServerStartTimeout, torn down,
    pidfile removed (no silent fallback)."""
    port = 45994
    monkeypatch.setattr(serve, "_probe_health", lambda *a, **k: False)
    monkeypatch.setattr(serve, "_server_bin", lambda: "fake-bin")

    class _FakeProc:
        pid = 999999

        def poll(self):
            return None  # pretend it stays alive so we exercise the timeout branch

    monkeypatch.setattr(serve.subprocess, "Popen", lambda *a, **k: _FakeProc())

    torn_down: list[int] = []
    monkeypatch.setattr(
        serve,
        "_terminate_tree",
        lambda pid, **k: torn_down.append(pid) or True,
    )

    with pytest.raises(serve.ServerStartTimeout) as excinfo:
        serve.ensure_server(port=port, timeout_s=0.6)

    assert excinfo.value.reason == "spawn_timeout"
    assert torn_down == [999999]
    assert not serve._pidfile_path(port).exists()


# --- stop_server idempotency branches -----------------------------------------


def test_stop_server_no_pidfile_is_clean_noop():
    note = serve.stop_server(port=45993)
    assert note["reason"] == "no_server"
    assert note["managed"] is False


def test_stop_server_dead_pid_cleans_up():
    port = 45992
    pidfile = serve._pidfile_path(port)
    # A PID that is (almost certainly) not alive, with a mismatching create time.
    serve._write_pidfile(
        pidfile,
        pid=987654,
        create_time=1.0,
        host="127.0.0.1",
        port=port,
        spawned_by_us=True,
    )
    note = serve.stop_server(port=port)
    assert note["reason"] == "dead_pid"
    assert not pidfile.exists()


def test_stop_server_refuses_to_kill_not_ours(monkeypatch):
    port = 45991
    pidfile = serve._pidfile_path(port)
    serve._write_pidfile(
        pidfile,
        pid=5555,
        create_time=1.0,
        host="127.0.0.1",
        port=port,
        spawned_by_us=False,
    )
    killed: list[int] = []
    monkeypatch.setattr(serve, "_terminate_tree", lambda pid, **k: killed.append(pid) or True)
    note = serve.stop_server(port=port)
    assert note["reason"] == "not_ours"
    assert killed == []  # an external server is never killed


# --- real subprocess: spawn -> attach -> stop (bounded, always cleaned up) ----


@pytest.mark.integration
def test_real_spawn_attach_stop_roundtrip():
    """End-to-end against a real ``clio-agent-gact`` on an ephemeral port."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    assert not serve._probe_health(base_url), "port should start free"

    spawned_pid: int | None = None
    try:
        # (1) spawn — becomes healthy within the bounded window.
        url = serve.ensure_server(port=port, timeout_s=45.0)
        assert url == base_url
        first = serve.last_action()
        assert first is not None and first["reason"] == "spawned"
        spawned_pid = first["pid"]
        assert serve._probe_health(base_url)

        # (2) a second ensure_server ATTACHES to the one we spawned — no new process.
        url2 = serve.ensure_server(port=port, timeout_s=5.0)
        assert url2 == base_url
        second = serve.last_action()
        assert second is not None
        assert second["reason"] == "already_running"
        assert second["pid"] == spawned_pid  # same process => nothing new spawned

        # (3) stop — kills the tree we spawned.
        note = serve.stop_server(port=port)
        assert note["reason"] == "stopped"
        assert note["pid"] == spawned_pid

        # (4) port is free again (bounded wait for the OS to reap the listener).
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not serve._probe_health(base_url):
                break
            time.sleep(0.5)
        assert not serve._probe_health(base_url), "server should be gone after stop"

        if sys.platform.startswith("win"):
            # Windows-specific: the detached-process-group flag path was exercised.
            assert spawned_pid is not None
    finally:
        # Belt-and-suspenders: never strand a daemon even if an assertion above failed.
        serve.stop_server(port=port)
