"""The ARC scope cache is write-through, reports to one residency owner, and lists by prefix.

R6 of the install-and-startup plan: the cache holds nothing clio-core has not
accepted, every cached scope is counted by the residency owner, a transient read leaves
nothing behind, and scope listing asks the store by name pattern.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.segment_cache import INVOCATION_INDEX_SCOPE, transient_reads
from clio_agent.arc.segments import ArcPersistError, SegmentStore
from clio_agent.arc.working_set_fold import make_segment_store


class _Residency:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.touched: list[str] = []

    def scope_resident(self, session_id: str, scope: str, nbytes: int) -> None:
        self.events.append(("resident", session_id, scope, nbytes))

    def scope_dropped(self, session_id: str, scope: str) -> None:
        self.events.append(("dropped", session_id, scope))

    def session_released(self, session_id: str) -> None:
        self.events.append(("released", session_id))

    def scope_touched(self, session_id: str) -> None:
        self.touched.append(session_id)


class _RefusingStore(_MemoryStore):
    """Refuses puts while ``refuse`` is set (a clio-core write that did not land)."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = False

    def put(self, kind: str, name: str, data: bytes, **kwargs: Any) -> None:
        if self.refuse:
            raise OSError("clio-core refused the put")
        super().put(kind, name, data, **kwargs)


class _QueryStore(_MemoryStore):
    """Answers name-pattern queries; listing every name fails the test."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    def query_names(self, kind: str, name_re: str = "") -> list[str]:
        import re

        self.queries.append(name_re)
        return [n for k, n in list(self._data) if k == kind and re.fullmatch(name_re, n)]

    def scan_names(self, kind: str, prefix: str = ""):  # type: ignore[no-untyped-def]
        assert prefix, "a whole-kind listing is never needed to find one scope's sessions"
        return super().scan_names(kind, prefix)


def test_a_refused_write_leaves_the_cache_at_the_committed_record() -> None:
    backing = _RefusingStore()
    store = SegmentStore(backing)
    kept = store.append("s", "_events", "semantic_event", {"text": "kept"})
    backing.refuse = True
    with pytest.raises(ArcPersistError):
        store.append("s", "_events", "semantic_event", {"text": "lost"})
    with pytest.raises(ArcPersistError):
        store.delete("s", "_events", [kept.id])
    # Nothing clio-core refused is visible, and the committed segment was not mutated.
    assert [s.content["text"] for s in store.render("s", "_events")] == ["kept"]
    assert kept.status == "live"


def test_a_tombstone_replaces_the_committed_segment_instead_of_mutating_it() -> None:
    store = SegmentStore(_MemoryStore())
    first = store.append("s", "scope", "thought", {"text": "a"})
    replacement = store.replace("s", "scope", first.id, {"text": "b"})
    assert first.status == "live"  # a reader holding the old render keeps its view
    assert [s.id for s in store.render("s", "scope")] == [replacement.id]
    tombstoned = store.list_segments("s", "scope", include_tombstoned=True)
    assert {s.id: s.status for s in tombstoned}[first.id] == "tombstoned"


@pytest.mark.parametrize("fold", [False, True])
def test_every_cached_scope_is_reported_to_the_residency_owner(fold: bool) -> None:
    store = make_segment_store(_MemoryStore(), working_set_fold=fold)
    residency = _Residency()
    store.set_residency(residency)
    store.append("s", "_events", "semantic_event", {"text": "x"})
    store.append("s", "expert", "thought", {"text": "y"})
    resident = [e for e in residency.events if e[0] == "resident"]
    assert resident and all(e[1] == "s" and e[3] > 0 for e in resident)
    store.render("s", "_events")
    assert "s" in residency.touched
    assert store.release("s") >= 1
    dropped = {e[2] for e in residency.events if e[0] == "dropped" and e[1] == "s"}
    assert {e[2] for e in resident} <= dropped


def test_a_transient_read_caches_and_reports_nothing() -> None:
    backing = _MemoryStore()
    SegmentStore(backing).append("s", "_events", "semantic_event", {"text": "stored"})
    store = SegmentStore(backing)
    residency = _Residency()
    store.set_residency(residency)
    with transient_reads():
        assert [s.content["text"] for s in store.render("s", "_events")] == ["stored"]
    assert store._loaded == set()
    assert residency.events == []
    store.render("s", "_events")  # outside: cached and counted
    assert ("s", "_events") in store._loaded
    assert residency.events and residency.events[0][:3] == ("resident", "s", "_events")


def test_drop_scope_reports_the_dropped_scope() -> None:
    store = SegmentStore(_MemoryStore())
    residency = _Residency()
    store.set_residency(residency)
    store.append("s", "_events", "semantic_event", {"text": "x"})
    assert store.drop_scope("s", "_events") == 1
    assert residency.events[-1] == ("dropped", "s", "_events")
    assert store.render("s", "_events") == []


def test_sessions_with_scope_asks_the_store_by_pattern() -> None:
    backing = _QueryStore()
    store = SegmentStore(backing)
    for sid in ("a", "b"):
        store.append(sid, "_events", "semantic_event", {"text": sid})
    store.append("c", "other", "thought", {"text": "c"})
    assert store.sessions_with_scope("_events") == ["a", "b"]
    assert backing.queries and backing.queries[-1].endswith("_events")


def test_scan_scopes_passes_the_scope_prefix_to_the_store() -> None:
    backing = _QueryStore()
    store = SegmentStore(backing)
    store.append("s", "agent/one", "thought", {"text": "1"})
    store.append("s", "other", "thought", {"text": "2"})
    assert store.scan_scopes("s", "agent/") == ["agent/one"]


def test_invocation_index_entries_are_counted_against_their_session() -> None:
    store = SegmentStore(_MemoryStore())
    residency = _Residency()
    store.set_residency(residency)
    store.note_invocation("s")
    store.note_invocation("s")
    first, second = residency.events
    assert first[:3] == second[:3] == ("resident", "s", INVOCATION_INDEX_SCOPE)
    assert second[3] == 2 * first[3]


@pytest.mark.parametrize("fold", [False, True])
def test_release_drops_the_locks_of_the_released_scopes(fold: bool) -> None:
    store = make_segment_store(_MemoryStore(), working_set_fold=fold)
    for index in range(20):
        store.append("s", f"agent/{index}", "thought", {"text": str(index)})
    store.append("t", "agent/0", "thought", {"text": "kept"})
    assert any(key[0] == "s" for key in store._scope_locks.keys())
    store.release("s")
    assert all(key[0] == "t" for key in store._scope_locks.keys())
    store.release("t")
    assert len(store._scope_locks) == 0


def test_a_read_of_an_uncached_scope_leaves_no_lock() -> None:
    backing = _MemoryStore()
    SegmentStore(backing).append("s", "_events", "semantic_event", {"text": "stored"})
    store = SegmentStore(backing)
    with transient_reads():
        store.render("s", "_events")
    assert store.has_segments("s", "never-written") is False
    store.release("s")
    assert len(store._scope_locks) == 0


def test_a_lock_held_across_a_release_stays_one_lock() -> None:
    """A holder pins the scope's lock: the release waits for it, a second thread still
    contends on the same lock, and the lock is gone once nobody holds it."""
    import threading

    store = SegmentStore(_MemoryStore())
    store.append("s", "scope", "thought", {"text": "a"})
    lock = store._lock_for("s", "scope")
    entered, finish = threading.Event(), threading.Event()
    observed: list[bool] = []

    def hold() -> None:
        with lock:
            entered.set()
            finish.wait(5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(5.0)
    releaser = threading.Thread(target=store.release, args=("s",))
    releaser.start()
    contender = threading.Thread(
        target=lambda: observed.append(store._lock_for("s", "scope").acquire(blocking=False))
    )
    contender.start()
    contender.join(5.0)
    assert observed == [False]  # still the held lock, not a fresh one
    finish.set()
    holder.join(5.0)
    releaser.join(5.0)
    assert ("s", "scope") not in store._loaded
    assert len(store._scope_locks) == 0


def test_a_reloaded_scope_gets_a_new_view_generation() -> None:
    store = SegmentStore(_MemoryStore())
    store.append("s", "scope", "thought", {"text": "a"})
    before = store.context_view("s", "scope").generation
    assert store.context_view("s", "scope").generation == before  # stable while cached
    store.release("s")
    assert store.context_view("s", "scope").generation != before
