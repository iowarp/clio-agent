"""Tests for the ARC store factory + the clio-core CTE backend (Thread B).

Unit tests (binding-free) cover factory selection and graceful degradation.
The CTE round-trip tests are marked ``integration`` (need iowarp-core's in-process
runtime) so the default unit lane (``-m "not integration"``) stays binding-free.
"""

from __future__ import annotations

import msgspec
import pytest

from clio_agent.arc import storage
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import LocalFSStore, make_arc_store

# ---- unit: factory selection + graceful degradation (no binding needed) ----


def test_factory_local(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    assert isinstance(store, LocalFSStore)


def test_attach_mode_fails_fast_without_daemon(monkeypatch):
    """CLIO_CTE_WITH_RUNTIME=0 with no daemon must fail FAST with an actionable error,
    not hang ~30s in chimaera_init (which make_arc_store's fallback can't catch)."""
    from clio_agent.arc.storage import CTEStore

    monkeypatch.setenv("CLIO_CTE_DAEMON_PORT", "59999")  # nothing listening here
    with pytest.raises(RuntimeError, match="no daemon is reachable"):
        CTEStore._require_daemon_reachable()


def test_attach_mode_does_not_silently_fall_back_to_localfs(tmp_path, monkeypatch):
    """An explicit attach (CLIO_CTE_WITH_RUNTIME=0) that fails must SURFACE, not
    silently drop to LocalFS -- that would give each process its own store and break
    the cross-process sharing the operator asked for. Embedded mode still degrades."""

    def _boom(**kwargs):
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(storage, "CTEStore", _boom)

    monkeypatch.setenv("CLIO_CTE_WITH_RUNTIME", "0")  # attach explicitly requested
    with pytest.raises(RuntimeError, match="daemon unreachable"):
        make_arc_store(backend="cte", data_dir=str(tmp_path))

    monkeypatch.setenv("CLIO_CTE_WITH_RUNTIME", "1")  # embedded -> graceful fallback OK
    store = make_arc_store(backend="cte", data_dir=str(tmp_path))
    assert isinstance(store, LocalFSStore)


def test_factory_env_selects_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ARC_STORE", "local")
    assert isinstance(make_arc_store(data_dir=str(tmp_path)), LocalFSStore)


def test_factory_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown CLIO_ARC_STORE"):
        make_arc_store(backend="bogus")


def test_factory_cte_falls_back_on_init_failure(tmp_path, monkeypatch):
    """CTE binding/runtime unavailable -> LocalFSStore + RuntimeWarning, never crash
    (CLAUDE.md graceful-degradation chain)."""
    def boom(*a, **k):
        raise ImportError("clio_cte_core_ext not built")

    monkeypatch.setattr(storage, "CTEStore", boom)
    with pytest.warns(RuntimeWarning, match="CTE store unavailable"):
        store = make_arc_store(backend="cte", data_dir=str(tmp_path))
    assert isinstance(store, LocalFSStore)


# ---- integration: real in-process CTE runtime ----


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


def test_put_if_absent_is_atomic_under_thread_race(tmp_path):
    """O_EXCL: many threads racing to create the SAME record yield exactly one winner —
    the basis for an exactly-once claim. Serialized single-process calls can't show this;
    real OS threads race the open() syscall (the GIL is released around it)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    n = 64
    barrier = threading.Barrier(n)

    def attempt(i: int) -> bool:
        barrier.wait()  # release all threads at once to maximize the race
        return store.put_if_absent("context", "claimx", f"w{i}".encode())

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(attempt, range(n)))

    assert sum(results) == 1  # exactly one creator won
    winner = results.index(True)
    assert store.get("context", "claimx") == f"w{winner}".encode()  # winner's bytes intact
    assert store.put_if_absent("context", "claimx", b"late") is False  # existing -> no overwrite
    assert store.get("context", "claimx") == f"w{winner}".encode()


def test_put_if_absent_cleans_up_on_write_failure(tmp_path, monkeypatch):
    """A write failure must NOT leave a 0-byte husk — an empty record would read back as
    'no holder' and permanently poison a claim. The file is removed so a retry can claim."""

    store = make_arc_store(backend="local", data_dir=str(tmp_path))

    def boom(_fd, _data):
        raise OSError(28, "ENOSPC")

    monkeypatch.setattr("clio_agent.arc.storage.os.write", boom)
    with pytest.raises(OSError):
        store.put_if_absent("context", "rid_a.claim", b"tok|123.0")
    monkeypatch.undo()

    assert store.get("context", "rid_a.claim") is None  # no husk left behind
    assert store.put_if_absent("context", "rid_a.claim", b"tok|456.0") is True  # retry works
