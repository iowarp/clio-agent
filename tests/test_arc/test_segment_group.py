"""Group commit of segment appends (``arc.segment_group``) on clio-core.

Staged appends are visible to the staging thread only, are put once per scope, survive
a concurrent writer on the same scope, and on a failed put leave nothing in the cache
that clio-core does not hold.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from clio_agent.arc.batch_put import BatchPutError
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.segment_errors import ArcPersistError
from clio_agent.arc.segment_group import cache_absent, segment_group


def _contents(arc: ARCMemory, sid: str, scope: str) -> list[Any]:
    return [segment.content for segment in arc._segments.render(sid, scope)]


def _reloaded(arc: ARCMemory, sid: str, scope: str) -> list[Any]:
    arc._segments.release(sid)  # drop the cache: the next read is clio-core's record
    return _contents(arc, sid, scope)


def _counting(arc: ARCMemory, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    store = arc._segments._store
    for method in ("put", "put_at", "put_many"):
        original = getattr(store, method)

        def wrapped(kind: str, *args: Any, _m: str = method, _f: Any = original, **kw: Any) -> Any:
            calls.append((_m, kind))
            return _f(kind, *args, **kw)

        monkeypatch.setattr(store, method, wrapped)
    return calls


def test_staged_appends_are_put_once_per_scope_after_a_concurrent_writer(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another thread appends to a staged scope: its segment keeps its place, the staged
    ones follow, and clio-core holds exactly what the cache shows.

    SABOTAGE: make ``SegmentGroup._rebased`` return ``entry.staged`` and the group's put
    lands at the stale end, over the other thread's record (the reload loses it).
    """
    store = arc._segments
    sid = "sess_group_rebase"
    store.append(sid, "_events", "semantic_event", {"n": 0})
    store.append(sid, "_events/t/content/a", "task_content", {"c": "seed"})
    calls = _counting(arc, monkeypatch)
    seen: list[list[Any]] = []

    def other() -> None:
        seen.append(_contents(arc, sid, "_events"))  # committed only: staged is not visible
        store.append(sid, "_events", "semantic_event", {"n": "other"})

    with segment_group(store, lambda scope: (1,) if scope == "_events" else (0,)) as group:
        store.append(sid, "_events", "semantic_event", {"n": 1})
        store.append(sid, "_events/t/content/b", "task_content", {"c": "new"})
        store.append(sid, "_events", "semantic_event", {"n": 2})
        assert _contents(arc, sid, "_events") == [{"n": 0}, {"n": 1}, {"n": 2}]  # read own
        worker = threading.Thread(target=other)
        worker.start()
        worker.join(10)
        assert calls == [("put_at", "segments")]  # only the other thread's append
        group.flush()

    assert seen == [[{"n": 0}]]
    expected = [{"n": 0}, {"n": "other"}, {"n": 1}, {"n": 2}]
    assert _contents(arc, sid, "_events") == expected
    orders = [segment.order for segment in store.render(sid, "_events")]
    assert orders == sorted(orders) and len(set(orders)) == 4
    # One put_many per rank: the content first, then the log.
    assert calls == [("put_at", "segments"), ("put_many", "segments"), ("put_many", "segments")]
    assert _reloaded(arc, sid, "_events") == expected
    assert _reloaded(arc, sid, "_events/t/content/b") == [{"c": "new"}]


def test_a_lost_group_put_leaves_only_what_clio_core_holds(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost put raises typed; the cache drops the staged scope, and its next read
    (clio-core's record) has the committed segments only.

    SABOTAGE: install the plan in ``_flush_rank`` even for lost names and the cache
    keeps a segment clio-core never stored.
    """
    store = arc._segments
    sid = "sess_group_lost"
    store.append(sid, "_events", "semantic_event", {"n": 0})
    original = store._store.put_many

    def lose(kind: str, records: Any) -> None:
        raise BatchPutError(kind, [], {record.name: RuntimeError("lost") for record in records})

    with segment_group(store, lambda _scope: (0,)) as group:
        store.append(sid, "_events", "semantic_event", {"n": 1})
        monkeypatch.setattr(store._store, "put_many", lose)
        with pytest.raises(ArcPersistError):
            group.flush()
    monkeypatch.setattr(store._store, "put_many", original)
    assert _contents(arc, sid, "_events") == [{"n": 0}]
    assert _reloaded(arc, sid, "_events") == [{"n": 0}]


def test_absent_scopes_are_probed_together_and_cached_empty(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One probe for several scopes; absent ones read as empty with no further call,
    present ones still load their record.

    SABOTAGE: cache every probed scope empty and the present record reads as empty.
    """
    store = arc._segments
    sid = "sess_group_probe"
    store.append(sid, "_events/t/content/held", "task_content", {"c": "held"})
    store.release(sid)
    probes: list[list[str]] = []
    original = store._store.sizes

    def sizes(kind: str, names: list[str]) -> dict[str, int]:
        probes.append(list(names))
        return original(kind, names)

    monkeypatch.setattr(store._store, "sizes", sizes)
    keys = [(sid, "_events/t/content/held"), (sid, "_events/t/content/new")]
    cache_absent(store, keys, minted=[(sid, "_events/t/content/minted")])
    assert len(probes) == 1 and len(probes[0]) == 2
    reads: list[str] = []
    get_raw = store._store.get_raw

    def counted(kind: str, name: str, **kwargs: Any) -> Any:
        reads.append(name)
        return get_raw(kind, name, **kwargs)

    monkeypatch.setattr(store._store, "get_raw", counted)
    assert _contents(arc, sid, "_events/t/content/new") == []
    assert _contents(arc, sid, "_events/t/content/minted") == []
    assert reads == []
    assert _contents(arc, sid, "_events/t/content/held") == [{"c": "held"}]
    assert len(reads) == 1


def test_ranks_put_cited_records_first_then_lane_chunks_then_the_log(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task-history ranks: content before the lane, lane chunks in chunk order (10
    after 2), the ``_events`` log last -- the order one operation writes them in.

    SABOTAGE: sort the ranks by ``repr`` in ``SegmentGroup.flush`` and chunk 10 goes first.
    """
    from clio_agent.gact.task_history_group import flush_rank

    store = arc._segments
    sid = "sess_group_ranks"
    batches: list[list[str]] = []
    original = store._store.put_many

    def put_many(kind: str, records: Any) -> None:
        batches.append([record.name.split("__", 1)[1] for record in records])
        original(kind, records)

    monkeypatch.setattr(store._store, "put_many", put_many)
    with segment_group(store, flush_rank) as group:
        for scope in ("_events", "_events/t/log/10", "_events/t/log/2", "_events/t/content/x"):
            store.append(sid, scope, "semantic_event", {"scope": scope})
        group.flush()
    assert batches == [
        ["_events~t~content~x"],
        ["_events~t~log~2"],
        ["_events~t~log~10"],
        ["_events"],
    ]
