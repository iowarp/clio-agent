"""Tests for clio-core daemon memory surfacing + bounded recycle (#891).

Covers the snapshot gather (fake psutil process), the ok/elevated/critical classifier,
threshold + recycle-switch config resolution, and the recycle policy — in particular
the invariant that a live client ALWAYS blocks a recycle, and that every successful
recycle emits the typed reason record. Two SABOTAGE pins encode the exact defects the
guards exist to prevent.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from clio_agent.arc import clio_core_daemon as ccd
from clio_agent.arc.clio_core_daemon import (
    CLIO_CORE_DAEMON_RECYCLED,
    DaemonMemorySnapshot,
    classify_daemon_rss,
    clio_core_daemon_recycle_snapshot,
    collect_daemon_memory_snapshot,
    maybe_recycle_idle_daemon,
    reset_clio_core_daemon_recycle,
)

_GiB = 1024**3


class _FakeProc:
    """Minimal psutil.Process stand-in exposing memory_info()/num_threads()."""

    def __init__(self, *, pid: int, rss: int, vms: int, threads: int) -> None:
        self.pid = pid
        self._rss = rss
        self._vms = vms
        self._threads = threads

    def memory_info(self):
        return SimpleNamespace(rss=self._rss, vms=self._vms)

    def num_threads(self):
        return self._threads


def _snapshot(rss: int, *, live: int = 0, stale: int = 0, pid: int = 4321) -> DaemonMemorySnapshot:
    return DaemonMemorySnapshot(
        pid=pid,
        pid_source="injected",
        rss_bytes=rss,
        committed_bytes=rss * 2,
        thread_count=12,
        live_client_count=live,
        stale_client_count=stale,
        registered_client_count=live + stale,
        port=9413,
    )


@pytest.fixture(autouse=True)
def _clean_recycle_record():
    reset_clio_core_daemon_recycle()
    yield
    reset_clio_core_daemon_recycle()


# --------------------------------------------------------------------------- #
# snapshot gather (fake psutil process)
# --------------------------------------------------------------------------- #


def test_collect_snapshot_reads_fake_process_ok():
    proc = _FakeProc(pid=999, rss=512 * 1024**2, vms=1 * _GiB, threads=8)
    snap = collect_daemon_memory_snapshot(
        process=proc, pid=999, pid_source="pidfile", live_pids=[111], registered_pids=[111, 222]
    )
    assert snap is not None
    assert snap.pid == 999
    assert snap.pid_source == "pidfile"
    assert snap.rss_bytes == 512 * 1024**2
    assert snap.committed_bytes == 1 * _GiB
    assert snap.thread_count == 8
    assert snap.live_client_count == 1
    assert snap.stale_client_count == 1  # 2 registered - 1 live
    assert snap.registered_client_count == 2
    assert classify_daemon_rss(snap.rss_bytes) == "ok"


def test_collect_snapshot_elevated_and_critical_via_fake_process():
    elevated = collect_daemon_memory_snapshot(
        process=_FakeProc(pid=1, rss=2 * _GiB, vms=3 * _GiB, threads=20),
        pid=1,
        live_pids=[],
        registered_pids=[],
    )
    critical = collect_daemon_memory_snapshot(
        process=_FakeProc(pid=1, rss=5 * _GiB, vms=8 * _GiB, threads=40),
        pid=1,
        live_pids=[],
        registered_pids=[],
    )
    assert elevated is not None and critical is not None
    assert classify_daemon_rss(elevated.rss_bytes) == "elevated"
    assert classify_daemon_rss(critical.rss_bytes) == "critical"


def test_collect_snapshot_none_when_no_daemon(monkeypatch):
    # No injected process and pid resolution finds nothing -> None (down daemon is a
    # #892 liveness concern, not a memory row).
    monkeypatch.setattr(ccd, "_resolve_daemon_pid", lambda *_a, **_k: (None, ""))
    assert collect_daemon_memory_snapshot() is None


# --------------------------------------------------------------------------- #
# classifier + config resolution
# --------------------------------------------------------------------------- #


def test_classify_boundaries_with_explicit_thresholds():
    assert classify_daemon_rss(_GiB - 1, warn=_GiB, critical=4 * _GiB) == "ok"
    assert classify_daemon_rss(_GiB, warn=_GiB, critical=4 * _GiB) == "elevated"
    assert classify_daemon_rss(4 * _GiB, warn=_GiB, critical=4 * _GiB) == "critical"


def test_default_thresholds_are_1_and_4_gib():
    warn, critical = ccd._resolve_daemon_rss_thresholds()
    assert warn == 1 * _GiB
    assert critical == 4 * _GiB


def test_threshold_config_resolution_from_env(monkeypatch):
    from clio_agent import conf

    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN", str(2 * _GiB))
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_DAEMON_RSS_CRITICAL", str(8 * _GiB))
    conf.reload()
    warn, critical = ccd._resolve_daemon_rss_thresholds()
    assert warn == 2 * _GiB
    assert critical == 8 * _GiB


def test_inverted_thresholds_fall_back_to_defaults(monkeypatch):
    from clio_agent import conf

    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN", str(8 * _GiB))
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_DAEMON_RSS_CRITICAL", str(2 * _GiB))
    conf.reload()
    warn, critical = ccd._resolve_daemon_rss_thresholds()
    assert (warn, critical) == (1 * _GiB, 4 * _GiB)  # inverted -> defaults, not silently accepted


def test_recycle_switch_defaults_off_and_env_enables(monkeypatch):
    from clio_agent import conf

    conf.reload()
    assert ccd._resolve_recycle_enabled() is False
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE", "1")
    conf.reload()
    assert ccd._resolve_recycle_enabled() is True


# --------------------------------------------------------------------------- #
# recycle policy
# --------------------------------------------------------------------------- #


def test_recycle_disabled_is_a_noop_typed_outcome():
    stop_called = []
    outcome = maybe_recycle_idle_daemon(
        enabled=False,
        snapshot=_snapshot(5 * _GiB),
        stop_daemon=lambda: stop_called.append(True),
        live_pids_fn=list,
        lock=contextlib.nullcontext(),
    )
    assert outcome.recycled is False
    assert outcome.reason == "disabled"
    assert stop_called == []


def test_recycle_not_critical_refuses():
    stop_called = []
    outcome = maybe_recycle_idle_daemon(
        enabled=True,
        snapshot=_snapshot(2 * _GiB),  # elevated, not critical
        stop_daemon=lambda: stop_called.append(True),
        live_pids_fn=list,
        lock=contextlib.nullcontext(),
    )
    assert outcome.recycled is False
    assert outcome.reason == "not_critical"
    assert stop_called == []


def test_recycle_refuses_with_live_client_and_records_nothing():
    """INVARIANT: a live client ALWAYS blocks a recycle; no daemon stop, no record."""
    stop_called = []
    outcome = maybe_recycle_idle_daemon(
        enabled=True,
        snapshot=_snapshot(6 * _GiB, live=1),
        stop_daemon=lambda: stop_called.append(True),
        live_pids_fn=lambda: [12345],  # a LIVE client attached
        lock=contextlib.nullcontext(),
    )
    assert outcome.recycled is False
    assert outcome.reason == "live_clients_present"
    assert outcome.live_client_count == 1
    # SABOTAGE PIN (a): if the guard ignores live clients, stop_daemon fires -> red.
    assert stop_called == []
    # And no recycle reason is recorded for a refused recycle.
    assert clio_core_daemon_recycle_snapshot() is None


def test_recycle_stale_only_recycles_and_records_reason():
    """Zero live clients + critical -> stop the daemon and emit the typed reason."""
    stop_called = []
    snap = _snapshot(6 * _GiB, stale=2, pid=7777)
    outcome = maybe_recycle_idle_daemon(
        enabled=True,
        snapshot=snap,
        stop_daemon=lambda: stop_called.append(True),
        live_pids_fn=list,  # zero live clients (all stale)
        lock=contextlib.nullcontext(),
    )
    assert outcome.recycled is True
    assert outcome.reason == "recycled"
    assert outcome.before_rss_bytes == 6 * _GiB
    assert stop_called == [True]  # the daemon WAS stopped

    # SABOTAGE PIN (b): the typed reason record MUST be emitted with the before-RSS.
    record = clio_core_daemon_recycle_snapshot()
    assert record is not None
    assert record.reason == CLIO_CORE_DAEMON_RECYCLED
    assert record.before_rss_bytes == 6 * _GiB
    assert record.pid == 7777
    assert record.to_details()["reason"] == CLIO_CORE_DAEMON_RECYCLED
