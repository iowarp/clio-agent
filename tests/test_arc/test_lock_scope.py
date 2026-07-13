"""Tests for ARC's scoped ``_lock`` (Slice B of #771).

The invariant under test: :class:`ARCMemory._lock` guards ONLY the hot in-memory
structures (the invocation B-tree, the disk-read/write counters, cache-composite
ops). It is NEVER held across ``_store`` I/O (CTE RPCs / LocalFS reads) or
``_lsm`` writes. Two consequences are asserted here:

1. Overlap: two threads writing to DIFFERENT sessions through a store whose
   ``put`` blocks for 50 ms must run concurrently -- their combined wall time is
   well under the serial (lock-held) baseline. Before the fix, ``store_invocation``
   held ``_lock`` across ``store.put`` and the two writers serialized.
2. Consistency: concurrent ``store_invocation`` / ``get_session_invocations`` /
   ``release_session`` across three sessions leaves cache, index, and disk in
   agreement (write-through: a released session keeps its durable disk copy while
   its hot cache/index footprint is evicted).
"""

from __future__ import annotations

import threading
import time
from typing import Iterator, Optional

import msgspec
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Conversation, Invocation


class _DelayedStore:
    """In-memory :class:`ARCStore` whose ``put`` sleeps ``delay`` seconds.

    Records live in a plain dict keyed by ``(kind, name)``. The delay models a
    slow CTE RPC so a lock held across ``put`` is directly observable as
    serialized wall time.
    """

    def __init__(self, delay: float = 0.05) -> None:
        self._delay = delay
        self._data: dict[tuple[str, str], bytes] = {}
        self._d_lock = threading.Lock()

    def put(
        self,
        kind: str,
        name: str,
        data: bytes,
        *,
        tier: str = "warm",
        search_text: Optional[str] = None,
    ) -> None:
        time.sleep(self._delay)
        with self._d_lock:
            self._data[(kind, name)] = data

    def get(self, kind: str, name: str) -> Optional[bytes]:
        with self._d_lock:
            return self._data.get((kind, name))

    def exists(self, kind: str, name: str) -> bool:
        with self._d_lock:
            return (kind, name) in self._data

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        with self._d_lock:
            rows = [
                (name, data)
                for (k, name), data in self._data.items()
                if k == kind and name.startswith(prefix)
            ]
        yield from rows

    def delete(self, kind: str, name: str) -> None:
        with self._d_lock:
            self._data.pop((kind, name), None)

    def clear(self) -> None:
        with self._d_lock:
            self._data.clear()

    def supports_search(self) -> bool:
        return False

    def search(
        self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        return []


def _inv(trace_id: str, session_id: str) -> Invocation:
    now = time.time()
    return Invocation(
        trace_id=trace_id,
        session_id=session_id,
        parent_trace_id=None,
        agent_id="data",
        tier=2,
        source="native",
        started_at=now,
        completed_at=now,
        duration_ms=1.0,
        status="success",
        input={},
        output={},
        tools_called=[],
        nanoagents_spawned=[],
        performance={},
    )


def test_store_invocation_does_not_serialize_across_store_put(tmp_path) -> None:
    """Two writers on different sessions overlap: concurrent wall < 0.8x serial."""
    delay = 0.05
    store = _DelayedStore(delay=delay)
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)

    # Serial baseline: two sequential writes each pay the full store.put delay.
    t0 = time.perf_counter()
    arc.store_invocation(_inv("serial-a", "sess-serial-a"))
    arc.store_invocation(_inv("serial-b", "sess-serial-b"))
    serial_wall = time.perf_counter() - t0

    # Concurrent: two writers on DIFFERENT sessions started together. With _lock
    # scoped off store.put, the two 50 ms puts overlap.
    start = threading.Barrier(2)

    def writer(idx: int) -> None:
        start.wait()
        arc.store_invocation(_inv(f"conc-{idx}", f"sess-conc-{idx}"))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    t0 = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    concurrent_wall = time.perf_counter() - t0

    assert concurrent_wall < 0.8 * serial_wall, (
        f"writers serialized: concurrent={concurrent_wall:.3f}s "
        f"serial={serial_wall:.3f}s (expected overlap under a scoped lock)"
    )
    # Sanity: all four invocations persisted and indexed.
    assert store.get("invocations", "conc-0") is not None
    assert store.get("invocations", "conc-1") is not None


def _disk_trace_ids(arc: ARCMemory, session_id: str) -> set[str]:
    ids: set[str] = set()
    for _name, encoded in arc._store.scan("invocations"):
        inv = msgspec.msgpack.decode(encoded, type=Invocation)
        if inv.session_id == session_id:
            ids.add(inv.trace_id)
    return ids


def _index_trace_ids(arc: ARCMemory, session_id: str) -> set[str]:
    with arc._lock:
        entries = list(arc._inv_index.get_session_range(session_id))
    return {e["trace_id"] for e in entries}


def test_concurrent_store_get_release_keep_index_cache_disk_consistent(tmp_path) -> None:
    """Concurrent writes/reads/release across 3 sessions leave state consistent."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sessions = ["s0", "s1", "s2"]
    per_session = 12

    # Phase 1: populate all three sessions concurrently.
    def populate(sess: str) -> None:
        for i in range(per_session):
            arc.store_invocation(_inv(f"{sess}-{i}", sess))

    threads = [threading.Thread(target=populate, args=(s,)) for s in sessions]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    for sess in sessions:
        expected = {f"{sess}-{i}" for i in range(per_session)}
        assert _index_trace_ids(arc, sess) == expected
        assert _disk_trace_ids(arc, sess) == expected

    # Phase 2: concurrently keep writing s0, read s0 repeatedly, release s1.
    errors: list[Exception] = []

    def more_writes() -> None:
        try:
            for i in range(per_session, per_session + per_session):
                arc.store_invocation(_inv(f"s0-{i}", "s0"))
        except Exception as exc:  # noqa: BLE001 - surface to assert below
            errors.append(exc)

    def readers() -> None:
        try:
            for _ in range(40):
                arc.get_session_invocations("s0")
        except Exception as exc:  # noqa: BLE001 - surface to assert below
            errors.append(exc)

    def release() -> None:
        try:
            arc.release_session("s1")
        except Exception as exc:  # noqa: BLE001 - surface to assert below
            errors.append(exc)

    phase2 = [
        threading.Thread(target=more_writes),
        threading.Thread(target=readers),
        threading.Thread(target=release),
    ]
    for th in phase2:
        th.start()
    for th in phase2:
        th.join()

    assert not errors, f"concurrent access raised: {errors!r}"

    # s0: all 2*per_session writes agree across index and disk.
    s0_expected = {f"s0-{i}" for i in range(2 * per_session)}
    assert _index_trace_ids(arc, "s0") == s0_expected
    assert _disk_trace_ids(arc, "s0") == s0_expected
    # s0 invocations are all retrievable and unique.
    got = arc.get_session_invocations("s0", limit=1000)
    assert {inv.trace_id for inv in got} == s0_expected

    # s1: released -> hot index/cache evicted, but write-through disk copy kept.
    assert _index_trace_ids(arc, "s1") == set()
    assert arc._cache.get("inv:s1-0") is None
    assert _disk_trace_ids(arc, "s1") == {f"s1-{i}" for i in range(per_session)}

    # s2: untouched by phase 2 -> index and disk still agree.
    s2_expected = {f"s2-{i}" for i in range(per_session)}
    assert _index_trace_ids(arc, "s2") == s2_expected
    assert _disk_trace_ids(arc, "s2") == s2_expected


def test_same_timestamp_invocations_both_retained(tmp_path) -> None:
    """#804 regression: two invocations in one session that share an identical
    coarse timestamp must BOTH survive -- the composite index key includes
    ``trace_id`` so neither silently overwrites the other.

    Slice B moved ``store_invocation``'s cache/index writes off the store-RPC
    path but MUST NOT regress PR #819's ``(session_id, timestamp, trace_id)`` key.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    ts = 1_700_000_000.0
    a = _inv("dup-a", "sess-dup")
    b = _inv("dup-b", "sess-dup")
    a.started_at = ts
    b.started_at = ts  # identical tick -> would collide without trace_id in the key

    arc.store_invocation(a)
    arc.store_invocation(b)

    got = {inv.trace_id for inv in arc.get_session_invocations("sess-dup", limit=100)}
    assert got == {"dup-a", "dup-b"}
    assert _index_trace_ids(arc, "sess-dup") == {"dup-a", "dup-b"}


class _GatedConvStore(_DelayedStore):
    """Store whose ``put`` on the ``conversations`` kind blocks on a gate once armed,
    so a writer can be frozen mid-write to expose the store_conversation /
    get_conversation interleaving deterministically (no timing luck)."""

    def __init__(self) -> None:
        super().__init__(delay=0.0)
        self._gate = threading.Event()
        self._armed = threading.Event()
        self.put_entered = threading.Event()

    def put(
        self,
        kind: str,
        name: str,
        data: bytes,
        *,
        tier: str = "warm",
        search_text: Optional[str] = None,
    ) -> None:
        if kind == "conversations" and self._armed.is_set():
            self.put_entered.set()
            self._gate.wait(timeout=5.0)
        with self._d_lock:
            self._data[(kind, name)] = data


def _conv(session_id: str, marker: str) -> Conversation:
    now = time.time()
    return Conversation(
        session_id=session_id,
        user_id="user@example.com",
        created_at=now,
        metadata={"marker": marker},
    )


def test_conversation_refill_cannot_clobber_a_concurrent_write(tmp_path) -> None:
    """A cache-miss ``get_conversation`` must never refill the LRU with a disk value
    older than a concurrent ``store_conversation`` on the SAME session.

    Regression for the lost-update race that narrowing ``_lock`` (#771 Slice B)
    introduced: with the per-session cache+store pair no longer atomic, a reader could
    cache a stale ``v0`` after a writer had already cached the fresh ``v2``, pinning
    the hot path to the old conversation forever. The writer is frozen inside
    ``store.put`` via a gate so the interleaving is deterministic.
    """
    store = _GatedConvStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    sess = "sess-rmw"

    arc.store_conversation(_conv(sess, "v0"))  # seed (gate disarmed)
    store._armed.set()  # the next conversations put (v2) will freeze mid-flight

    def writer() -> None:
        arc.store_conversation(_conv(sess, "v2"))

    wt = threading.Thread(target=writer)
    wt.start()
    assert store.put_entered.wait(2.0), "writer never reached the gated store.put"

    # Model a cache eviction (release_session / LRU pressure) so the reader is forced
    # down the slow disk-refill path while the writer is still frozen.
    arc._cache.invalidate(f"conv:{sess}")

    reader_result: dict[str, Optional[str]] = {}

    def reader() -> None:
        c = arc.get_conversation(sess)
        reader_result["marker"] = c.metadata.get("marker") if c else None

    rt = threading.Thread(target=reader)
    rt.start()
    time.sleep(0.2)  # let the reader reach disk (unfixed) or block on the lock (fixed)
    store._gate.set()  # release the frozen writer
    wt.join(5.0)
    rt.join(5.0)
    assert not wt.is_alive() and not rt.is_alive(), "threads did not settle (deadlock?)"

    final = arc.get_conversation(sess)
    assert final is not None and final.metadata.get("marker") == "v2", (
        "stale conversation won the cache after a concurrent write: "
        f"reader saw {reader_result.get('marker')!r}, "
        f"final cache marker={final.metadata.get('marker') if final else None!r}"
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    pytest.main([__file__, "-v"])
