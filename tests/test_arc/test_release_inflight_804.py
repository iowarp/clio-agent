"""#804 regression: ``release_session`` must observe invocations whose durable
write is still in flight.

``store_invocation`` writes the record to the store OUTSIDE ``_lock`` (the #771
narrowing), then inserts the ``_inv_index`` entry AFTER the store RPC returns. So
a ``release_session`` that runs while a same-session ``store_invocation`` is frozen
inside ``store.put`` sees only the already-indexed entries: it under-counts /
under-evicts, and the in-flight invocation's index entry then lands AFTER the
release as a leaked stale entry for an already-released session.

The gate freezes the writer mid-``store.put`` so the interleaving is deterministic
(no timing luck), mirroring ``_GatedConvStore`` in ``test_lock_scope.py``.
"""

from __future__ import annotations

import threading
import time
from typing import Iterator, Optional

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Invocation


class _GatedInvStore:
    """In-memory ARCStore whose ``put`` on the ``invocations`` kind blocks on a
    gate once armed, freezing a writer between its store write and its index
    insert."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}
        self._d_lock = threading.Lock()
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
        if kind == "invocations" and self._armed.is_set():
            self.put_entered.set()
            self._gate.wait(timeout=5.0)
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


def test_release_drains_inflight_invocation_write(tmp_path) -> None:
    """A ``release_session`` racing an in-flight same-session ``store_invocation``
    must count/evict BOTH invocations, and leave no stale index entry behind."""
    store = _GatedInvStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    sess = "s1"

    # t1 lands fully (gate disarmed).
    arc.store_invocation(_inv("t1", sess))

    # Arm: the NEXT invocations put (t2) freezes mid-flight, before its index insert.
    store._armed.set()

    def writer() -> None:
        arc.store_invocation(_inv("t2", sess))

    wt = threading.Thread(target=writer)
    wt.start()
    assert store.put_entered.wait(2.0), "writer never reached the gated store.put"

    # Release runs on its own thread so a correct (draining) implementation can
    # block on the in-flight t2 without deadlocking the test driver.
    result: dict[str, int] = {}

    def releaser() -> None:
        result.update(arc.release_session(sess))

    rt = threading.Thread(target=releaser)
    rt.start()

    # Unfixed: release does NOT wait for the in-flight write, so it returns
    # promptly having counted only t1 (index == 1). Fixed: release drains the
    # in-flight t2 and blocks here until the gate is opened below.
    rt.join(timeout=1.5)
    drained = rt.is_alive()  # still blocked => draining (fixed behavior)

    store._gate.set()  # let the frozen writer finish its put + index insert
    wt.join(5.0)
    rt.join(5.0)
    assert not wt.is_alive() and not rt.is_alive(), "threads did not settle (deadlock?)"

    # The release must have accounted for BOTH invocations.
    assert result.get("index") == 2, (
        f"release under-counted an in-flight invocation: index={result.get('index')} "
        f"(drained={drained})"
    )
    # And no stale index entry may survive for the released session.
    with arc._lock:
        remaining = list(arc._inv_index.get_session_range(sess))
    assert remaining == [], f"stale index entry leaked after release: {remaining!r}"
    # The drain quiesced (gate opened before release finished), so the release is
    # NOT degraded: inflight_pending must be 0.
    assert result.get("inflight_pending") == 0


def test_drain_timeout_reports_pending_count(tmp_path) -> None:
    """When the drain cannot quiesce in time it must REPORT the residual count, not
    silently proceed (no silent fallback): ``_drain_inflight_invocations`` returns the
    still-pending count, which ``release_session`` surfaces as ``inflight_pending``."""
    store = _GatedInvStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    sess = "s1"

    store._armed.set()

    def writer() -> None:
        arc.store_invocation(_inv("t1", sess))

    wt = threading.Thread(target=writer)
    wt.start()
    try:
        assert store.put_entered.wait(2.0), "writer never reached the gated store.put"
        # t1 is frozen mid-put -> in flight. A short-timeout drain cannot quiesce and
        # must report the residual (1), not return a clean 0.
        pending = arc._drain_inflight_invocations(sess, timeout=0.1)
        assert pending == 1, f"drain timeout must report the pending write, got {pending}"
    finally:
        store._gate.set()  # release the frozen writer so the thread can exit
        wt.join(5.0)
    assert not wt.is_alive(), "writer thread did not settle"
    # Once the write completes, a fresh drain quiesces cleanly.
    assert arc._drain_inflight_invocations(sess, timeout=1.0) == 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
    pytest.main([__file__, "-v"])
