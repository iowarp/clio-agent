"""The ARC scope cache is write-through, reports to one residency owner, and lists by prefix.

R6 of the install-and-startup plan: the cache holds nothing clio-core has not
accepted, every cached scope is counted by the residency owner, a transient read leaves
nothing behind, and scope listing asks the store by name pattern.
"""

from __future__ import annotations

import threading
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

    def put_at(self, kind: str, name: str, data: bytes, offset: int, **kwargs: Any) -> None:
        if self.refuse:
            raise OSError("clio-core refused the put")
        super().put_at(kind, name, data, offset, **kwargs)


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
    assert {e[2] for e in resident} <= dropped  # each cached scope reported as it goes


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


def test_a_release_reports_the_invocation_index_dropped() -> None:
    store = SegmentStore(_MemoryStore())
    residency = _Residency()
    store.set_residency(residency)
    store.note_invocation("s")
    store.release("s")
    assert residency.events[-1] == ("dropped", "s", INVOCATION_INDEX_SCOPE)
    assert store._invocation_counts == {}


# ---- the lock registry is bounded by what is cached or in use ----------------------


def _session_footprint(store: Any, session_id: str) -> dict[str, list[Any]]:
    """Every per-session/per-scope key the store (and its lock registry) still keeps."""
    held: dict[str, list[Any]] = {}
    attrs = dict(vars(store))
    attrs["_scope_locks._entries"] = store._scope_locks._entries
    for name, value in attrs.items():
        if isinstance(value, (dict, set)):
            keys = [
                k
                for k in value
                if k == session_id or (isinstance(k, tuple) and k and k[0] == session_id)
            ]
            if keys:
                held[name] = keys
    return held


@pytest.mark.parametrize("fold", [False, True])
def test_release_drops_the_locks_of_the_released_scopes(fold: bool) -> None:
    store = make_segment_store(_MemoryStore(), working_set_fold=fold)
    for index in range(20):
        store.append("s", f"agent/{index}", "thought", {"text": str(index)})
        store.context_view("s", f"agent/{index}")
    store.append("s", "_events", "semantic_event", {"text": "log"})
    store.context_view("s", "_events")
    store.append("t", "agent/0", "thought", {"text": "kept"})
    assert any(key[0] == "s" for key in store._scope_locks.keys())
    store.release("s")
    assert all(key[0] == "t" for key in store._scope_locks.keys())
    assert _session_footprint(store, "s") == {}  # no lock, generation or writer left
    assert [seg.content["text"] for seg in store.render("s", "agent/3")] == ["3"]
    store.release("s")
    store.release("t")
    assert len(store._scope_locks) == 0
    assert _session_footprint(store, "t") == {}


@pytest.mark.parametrize("fold", [False, True])
def test_clear_leaves_no_lock_or_generation(fold: bool) -> None:
    store = make_segment_store(_MemoryStore(), working_set_fold=fold)
    for sid in ("a", "b"):
        store.append(sid, "agent", "thought", {"text": sid})
        store.append(sid, "_events", "semantic_event", {"text": sid})
        store.context_view(sid, "_events")
    store.clear()
    assert len(store._scope_locks) == 0
    assert store._scopes == {} and store._loaded == set() and store._generations == {}
    assert [seg.content["text"] for seg in store.render("a", "agent")] == ["a"]


def test_a_read_of_an_uncached_scope_leaves_no_lock() -> None:
    backing = _MemoryStore()
    SegmentStore(backing).append("s", "_events", "semantic_event", {"text": "stored"})
    store = SegmentStore(backing)
    with transient_reads():
        store.render("s", "_events")
        store.context_view("s", "_events")
    assert store.has_segments("s", "never-written") is False
    assert ("s", "_events") not in store._scope_locks.keys()
    store.release("s")
    assert len(store._scope_locks) == 0
    assert store._generations == {}


def test_a_lock_held_across_a_release_stays_one_lock() -> None:
    """A holder pins the scope's lock: the release waits for it, a second thread still
    contends on the same lock, and the lock is gone once nobody holds it."""
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
    assert ("s", "scope") in store._loaded  # the release waits for the holder
    finish.set()
    holder.join(5.0)
    releaser.join(5.0)
    assert not releaser.is_alive()
    assert ("s", "scope") not in store._loaded
    assert len(store._scope_locks) == 0


def test_a_lock_waiter_keeps_its_entry_until_it_is_done() -> None:
    """A thread WAITING on a scope lock pins it too: when the holder leaves, the waiter
    gets that same lock, and the entry goes only after the waiter releases it."""
    store = SegmentStore(_MemoryStore())
    lock = store._lock_for("s", "pseudo")  # never cached: only pins keep it
    lock.acquire()
    acquired = threading.Event()
    finish = threading.Event()

    def wait_then_hold() -> None:
        with store._lock_for("s", "pseudo"):
            acquired.set()
            finish.wait(5.0)

    waiter = threading.Thread(target=wait_then_hold)
    waiter.start()
    deadline = threading.Event()
    while store._scope_locks._entries[("s", "pseudo")].pins < 2 and not deadline.wait(0.01):
        pass
    lock.release()  # the holder leaves while the waiter still waits: the entry stays
    assert acquired.wait(5.0)
    assert store._scope_locks.keys() == [("s", "pseudo")]
    contended: list[bool] = []
    probe = threading.Thread(target=lambda: contended.append(lock.acquire(blocking=False)))
    probe.start()
    probe.join(5.0)
    assert contended == [False]  # the waiter holds the one lock of the scope
    finish.set()
    waiter.join(5.0)
    assert len(store._scope_locks) == 0


def test_a_failed_or_interrupted_acquire_leaves_no_pin() -> None:
    store = SegmentStore(_MemoryStore())
    lock = store._lock_for("s", "scope")
    taken = threading.Event()
    finish = threading.Event()

    def hold() -> None:
        with store._lock_for("s", "scope"):
            taken.set()
            finish.wait(5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    assert taken.wait(5.0)
    assert lock.acquire(blocking=False) is False
    assert lock.acquire(timeout=0.01) is False
    finish.set()
    holder.join(5.0)
    assert len(store._scope_locks) == 0
    with pytest.raises(RuntimeError):
        lock.release()  # nothing held: refused, and no entry is created or left


def test_a_reloaded_scope_gets_a_new_view_generation() -> None:
    store = SegmentStore(_MemoryStore())
    store.append("s", "scope", "thought", {"text": "a"})
    before = store.context_view("s", "scope").generation
    assert store.context_view("s", "scope").generation == before  # stable while cached
    store.append("s", "scope", "thought", {"text": "b"})
    assert store.context_view("s", "scope").generation == before  # appends keep it
    store.release("s")
    assert store._generations == {}
    after = store.context_view("s", "scope").generation
    assert after != before
    assert store.context_view("s", "scope").generation == after


def test_drop_scope_renews_the_view_generation() -> None:
    store = SegmentStore(_MemoryStore())
    store.append("s", "scope", "thought", {"text": "a"})
    before = store.context_view("s", "scope").generation
    store.drop_scope("s", "scope")
    store.append("s", "scope", "thought", {"text": "b"})
    assert store.context_view("s", "scope").generation != before
