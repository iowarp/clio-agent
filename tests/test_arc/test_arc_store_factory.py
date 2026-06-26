"""Tests for the ARC store factory + the clio-core CTE backend (Thread B).

Unit tests (binding-free) cover factory selection and graceful degradation.
The CTE round-trip tests are marked ``integration`` (connect-or-spawn the shared
iowarp-core runtime) so the default unit lane (``-m "not integration"``) stays
binding-free.
"""

from __future__ import annotations

import os
import socket

import msgspec
import pytest

from clio_agent.arc import storage
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import LocalFSStore, make_arc_store

# ---- unit: factory selection + graceful degradation (no binding needed) ----


def test_factory_local(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    assert isinstance(store, LocalFSStore)


def test_factory_env_selects_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ARC_STORE", "local")
    assert isinstance(make_arc_store(data_dir=str(tmp_path)), LocalFSStore)


def test_factory_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown CLIO_ARC_STORE"):
        make_arc_store(backend="bogus")


def test_factory_cte_raises_on_init_failure(tmp_path, monkeypatch):
    """CTE binding/runtime unavailable -> RAISE (fail-loud), NEVER silently degrade to
    LocalFS ([[deliberate-config-fail-loud]]). A silent fallback would mask a broken
    clio-core deploy and obscure that ARC dropped off clio-core. LocalFS is opt-in only
    (backend="local" / CLIO_ARC_STORE=local)."""

    def boom(*a, **k):
        raise ImportError("clio_cte_core_ext not built")

    monkeypatch.setattr(storage, "CTEStore", boom)
    with pytest.raises(RuntimeError, match="clio-core CTE backend"):
        make_arc_store(backend="cte", data_dir=str(tmp_path))


# ---- unit: shared clio-core runtime lifecycle (connect-or-spawn, binding-free) ----


def test_resolve_runtime_port_default(monkeypatch, tmp_path):
    """No override, no config networking.port -> the documented default 9413."""
    monkeypatch.delenv("CLIO_CORE_PORT", raising=False)
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)
    monkeypatch.delenv("CHI_SERVER_CONF", raising=False)
    monkeypatch.setattr(storage.Path, "home", classmethod(lambda cls: tmp_path))
    assert storage._resolve_runtime_port("") == storage._DEFAULT_RUNTIME_PORT


def test_resolve_runtime_port_env_override(monkeypatch):
    monkeypatch.setenv("CLIO_CORE_PORT", "9999")
    assert storage._resolve_runtime_port("") == 9999


def test_resolve_runtime_port_bad_override_falls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIO_CORE_PORT", "not-an-int")
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)
    monkeypatch.delenv("CHI_SERVER_CONF", raising=False)
    monkeypatch.setattr(storage.Path, "home", classmethod(lambda cls: tmp_path))
    assert storage._resolve_runtime_port("") == storage._DEFAULT_RUNTIME_PORT


def test_resolve_runtime_port_reads_config(monkeypatch, tmp_path):
    monkeypatch.delenv("CLIO_CORE_PORT", raising=False)
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)
    monkeypatch.delenv("CHI_SERVER_CONF", raising=False)
    cfg = tmp_path / "clio.yaml"
    cfg.write_text("networking:\n  port: 9421\n", encoding="utf-8")
    assert storage._resolve_runtime_port(str(cfg)) == 9421


def test_read_yaml_port_variants(tmp_path):
    missing = tmp_path / "nope.yaml"
    assert storage._read_yaml_port(str(missing)) is None
    no_net = tmp_path / "a.yaml"
    no_net.write_text("runtime:\n  num_threads: 4\n", encoding="utf-8")
    assert storage._read_yaml_port(str(no_net)) is None
    good = tmp_path / "b.yaml"
    good.write_text("networking:\n  port: 9500\n", encoding="utf-8")
    assert storage._read_yaml_port(str(good)) == 9500


def test_runtime_alive_detects_listener():
    """A bound socket reads as alive; a free port reads as down."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert storage._runtime_alive(port) is True
    # socket closed -> port free again
    assert storage._runtime_alive(port) is False


def test_ensure_runtime_daemon_connects_when_already_up(monkeypatch, tmp_path):
    """If a runtime is already listening, connect-or-spawn must NOT spawn."""
    _isolate_clio_home(monkeypatch, tmp_path)
    monkeypatch.setattr(storage, "_resolve_runtime_port", lambda _cfg: 4321)
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: True)

    def fail_spawn(*a, **k):  # pragma: no cover - asserts it is never called
        raise AssertionError("spawned a daemon when one was already running")

    monkeypatch.setattr(storage, "_spawn_runtime_daemon", fail_spawn)
    storage._ensure_runtime_daemon(object(), "", "error")  # no raise == connect path
    assert os.getpid() in storage._live_client_pids()  # AND we registered as a client


def test_ensure_runtime_daemon_spawns_when_down(monkeypatch, tmp_path):
    """No runtime -> spawn once, then succeed when it comes up."""
    _isolate_clio_home(monkeypatch, tmp_path)
    calls = {"spawn": 0}
    # down on the under-lock check + first poll, up afterwards.
    alive_seq = iter([False, False, True, True])
    monkeypatch.setattr(storage, "_resolve_runtime_port", lambda _cfg: 4322)
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: next(alive_seq))

    def spawn(*a, **k):
        calls["spawn"] += 1

    monkeypatch.setattr(storage, "_spawn_runtime_daemon", spawn)
    storage._ensure_runtime_daemon(object(), "", "error")
    assert calls["spawn"] == 1


# ---- unit: client refcount / last-one-out daemon release (binding-free) ----


def _isolate_clio_home(monkeypatch, tmp_path):
    monkeypatch.setattr(storage.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(storage, "_client_registered", False)


def test_proc_starttime_and_pid_alive():
    pid = os.getpid()
    st = storage._proc_starttime(pid)
    assert isinstance(st, int)
    assert storage._pid_alive(pid, st) is True
    assert storage._pid_alive(pid, st + 1) is False  # start-time mismatch => PID reused
    assert storage._proc_starttime(2_000_000_000) is None  # no such pid
    assert storage._pid_alive(2_000_000_000, 123) is False


def test_register_then_live_then_deregister(monkeypatch, tmp_path):
    _isolate_clio_home(monkeypatch, tmp_path)
    storage._register_client()
    assert os.getpid() in storage._live_client_pids()
    storage._deregister_client()
    assert os.getpid() not in storage._live_client_pids()


def test_live_pids_prunes_dead_and_reused(monkeypatch, tmp_path):
    _isolate_clio_home(monkeypatch, tmp_path)
    reg = storage._client_registry_dir()
    reg.mkdir(parents=True)
    (reg / "2000000001").write_text("123", encoding="utf-8")  # dead pid
    # our pid but a wrong start-time == PID-reuse impostor
    (reg / str(os.getpid())).write_text(
        str(storage._proc_starttime(os.getpid()) + 7), encoding="utf-8"
    )
    assert storage._live_client_pids() == []
    assert not (reg / "2000000001").exists()  # pruned
    assert not (reg / str(os.getpid())).exists()  # pruned (start-time mismatch)


def test_release_keeps_daemon_when_another_client_alive(monkeypatch, tmp_path):
    _isolate_clio_home(monkeypatch, tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(storage, "_stop_runtime_daemon", lambda *a, **k: calls.append(1))
    storage._register_client()  # us
    ppid = os.getppid()  # a second, genuinely-alive client
    reg = storage._client_registry_dir()
    (reg / str(ppid)).write_text(str(storage._proc_starttime(ppid)), encoding="utf-8")
    storage.release_runtime_client("", "error")
    assert calls == []  # another client attached -> daemon NOT stopped
    assert os.getpid() not in storage._live_client_pids()  # but we deregistered


def test_release_stops_daemon_when_last_and_is_idempotent(monkeypatch, tmp_path):
    _isolate_clio_home(monkeypatch, tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(storage, "_stop_runtime_daemon", lambda *a, **k: calls.append(1))
    storage._register_client()
    storage.release_runtime_client("", "error")
    assert calls == [1]  # we were the last -> stop the shared daemon
    storage.release_runtime_client("", "error")  # second call
    assert calls == [1]  # idempotent (no double-stop)


# ---- integration: real shared clio-core CTE runtime (connect-or-spawn) ----


@pytest.mark.integration
def test_cte_roundtrip_binary():
    """The base64 regression guard: arbitrary msgpack bytes (incl. non-UTF-8) must
    round-trip byte-identically through CTE's UTF-8-decoding GetBlob."""
    store = make_arc_store(backend="cte")
    assert type(store).__name__ == "CTEStore"
    payload = msgspec.msgpack.encode({"n": 1, "raw": b"\x00\x83\xff\x81", "s": "x"})
    store.put("segments", "cte_rt__k1", payload)
    assert store.get("segments", "cte_rt__k1") == payload  # identical bytes
    assert store.exists("segments", "cte_rt__k1") is True
    assert store.exists("segments", "cte_rt__missing") is False
    assert store.get("segments", "cte_rt__missing") is None
    store.delete("segments", "cte_rt__k1")
    assert store.get("segments", "cte_rt__k1") is None


@pytest.mark.integration
def test_cte_scan_prefix():
    store = make_arc_store(backend="cte")
    store.put("segments", "cte_scan__a", b"AAA")
    store.put("segments", "cte_scan__b", b"BBB")
    store.put("segments", "cte_other__c", b"CCC")
    names = sorted(n for n, _ in store.scan("segments", "cte_scan__"))
    assert names == ["cte_scan__a", "cte_scan__b"]


@pytest.mark.integration
def test_cte_backs_the_live_segment_plane():
    """The whole point: the live context plane (SegmentStore) runs on CTE."""
    arc = ARCMemory(store=make_arc_store(backend="cte"))
    assert type(arc._store).__name__ == "CTEStore"
    sid, scope = "cte_live_s1", "agentA"
    arc.append_segment(sid, scope, "thought", {"text": "on CTE"}, step=0)
    arc.append_segment(sid, scope, "observation", {"text": "OBS_CTE"}, step=0)
    assert "OBS_CTE" in str(arc.render_segments_keys(sid, scope))
    # a second ARCMemory over the same runtime sees the persisted segments
    arc2 = ARCMemory(store=make_arc_store(backend="cte"))
    assert len(arc2.render_segments(sid, scope)) == 2
    arc.clear_all()
