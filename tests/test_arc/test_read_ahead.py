"""Read-ahead: a one-time pass reads its records together and then reads them as before.

Unit tests over fake stores and fake native futures (no daemon), plus one over the real
clio-core store: a prefetched record answers ``get`` without a round trip, a write drops
it first, and the buffer lives for one pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.arc import clio_core_async_ops as ops
from clio_agent.arc import read_ahead
from clio_agent.arc.blob_frame import frame
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.read_ahead import ReadAhead


class _Store:
    """A store with ``get_many`` that counts its reads (what ``prefetch`` calls)."""

    def __init__(self, records: dict[str, bytes]) -> None:
        self.records = records
        self.batches: list[list[str]] = []
        self.read_ahead: ReadAhead | None = None

    def get_many(self, kind: str, names: list[str]) -> dict[str, bytes | None]:
        self.batches.append(list(names))
        return {name: self.records.get(name) for name in names}

    def query_names(self, kind: str, name_re: str = "") -> list[str]:
        return sorted(self.records)


def test_buffer_answers_holds_absence_and_drops_written_names() -> None:
    buffer = ReadAhead()
    buffer.fill("segments", {"a": b"1", "gone": None})
    assert buffer.lookup("segments", "a") == (True, b"1")
    assert buffer.lookup("segments", "gone") == (True, None)  # read ahead as absent
    assert buffer.lookup("segments", "other") == (False, None)  # not read ahead
    assert buffer.lookup("variants", "a") == (False, None)  # kinds are apart
    buffer.forget("segments", ["a"])
    assert buffer.lookup("segments", "a") == (False, None)


def test_held_listing_follows_writes_and_answers_prefixes_in_order() -> None:
    buffer = ReadAhead()
    buffer.hold_listing("segments", ["s1__b", "s1__a", "s2__a"])
    assert buffer.names_with_prefix("segments", "s1__") == ["s1__a", "s1__b"]
    buffer.forget("segments", ["s1__c"])  # a put creates the record
    buffer.forget("segments", ["s1__a"], deleted=True)
    assert buffer.names_with_prefix("segments", "s1__") == ["s1__b", "s1__c"]
    assert buffer.names_with_prefix("segments", "s3__") == []
    assert buffer.names_with_prefix("variants", "") is None  # no listing held: ask the store
    buffer.clear()  # a batch is done: records go, the listing stays
    assert buffer.names_with_prefix("segments", "s2") == ["s2__a"]
    buffer.reset()
    assert buffer.names_with_prefix("segments", "") is None


def test_reading_ahead_installs_one_buffer_for_the_pass_and_nests() -> None:
    store = _Store({})
    with read_ahead.reading_ahead(store) as outer:
        assert outer is not None and store.read_ahead is outer
        with read_ahead.reading_ahead(store) as inner:
            assert inner is outer  # an enclosing pass's buffer is reused, not replaced
        assert store.read_ahead is outer
    assert store.read_ahead is None

    class _Direct:  # an in-process store: nothing to batch
        pass

    with read_ahead.reading_ahead(_Direct()) as none:
        assert none is None


def test_prefetch_reads_only_what_is_not_held_and_records_listed_absences() -> None:
    store = _Store({"a": b"1", "b": b"2"})
    with read_ahead.reading_ahead(store):
        assert read_ahead.prefetch(store, "segments", ["a", "b", "a"], absent=["x"]) == 2
        assert read_ahead.prefetch(store, "segments", ["a", "c"]) == 1
        assert store.batches == [["a", "b"], ["c"]]
        assert read_ahead.held_ahead(store, "segments", "x") == (True, None)
        read_ahead.forget_written(store, "segments", ["a"])
        assert read_ahead.held_ahead(store, "segments", "a") == (False, None)
    assert read_ahead.prefetch(store, "segments", ["a"]) == 0  # outside a pass: nothing


def test_ahead_by_batch_reads_each_batch_before_yielding_it() -> None:
    store = _Store({f"r{i}": b"x" for i in range(5)})
    seen: list[tuple[str, int]] = []
    with read_ahead.reading_ahead(store) as buffer:
        assert buffer is not None
        for item in read_ahead.ahead_by_batch(
            store, "segments", [f"r{i}" for i in range(5)], lambda n: ([n], []), size=2
        ):
            seen.append((item, len(store.batches)))
            assert buffer.holds("segments", item)
    assert [item for item, _ in seen] == [f"r{i}" for i in range(5)]
    assert [batches for _, batches in seen] == [1, 1, 2, 2, 3]  # read before each batch
    assert store.batches == [["r0", "r1"], ["r2", "r3"], ["r4"]]


def test_family_names_follow_the_dense_walk_and_the_erase_lookahead() -> None:
    class _Segments:
        @staticmethod
        def _record_name(session_id: str, scope: str) -> str:
            return f"{session_id}__{scope.replace('/', '~')}"

    listing = {"s__lane", "s__lane~2", "s__lane~5"}
    present, absent = read_ahead.family_names(_Segments(), "s", "lane", listing)
    assert present == ["s__lane", "s__lane~2", "s__lane~5"]  # a chunk past a 2-hole too
    assert absent == ["s__lane~3", "s__lane~4", "s__lane~6", "s__lane~7", "s__lane~8"]


# ---- the batched native reads ----------------------------------------------------------


class _Ready:
    def __init__(self, code: int = 0, value: Any = None, ready_after: int = 0) -> None:
        self.code, self.value, self.polls, self.ready_after = code, value, 0, ready_after

    def done(self) -> bool:
        self.polls += 1
        return self.polls > self.ready_after

    def wait(self, max_sec: float = -1.0) -> int:
        return self.code

    def result(self, max_sec: float = -1.0) -> Any:
        return self.value


class _Client:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, str]] = []

    def AsyncGetBlobSize(self, tag_id: Any, name: str, query: Any) -> _Ready:  # noqa: N802
        self.calls.append(("size", name))
        if name not in self.bodies:
            return _Ready(code=1)  # GetBlobSize's "no such blob"
        return _Ready(value=len(self.bodies[name]), ready_after=2)

    def AsyncGetBlob(self, tag_id: Any, name: str, size: int, off: int = 0) -> _Ready:  # noqa: N802
        self.calls.append(("get", name))
        return _Ready(value=self.bodies[name][:size], ready_after=1)


def _native_store(bodies: dict[str, bytes]) -> Any:
    from types import SimpleNamespace

    cte = SimpleNamespace(PoolQuery=SimpleNamespace(Local=lambda: "local"))
    tag_ids = SimpleNamespace(get=lambda kind: "tag")
    return SimpleNamespace(_cte=cte, _client=_Client(bodies), _tag_ids=tag_ids)


def test_store_get_many_issues_every_probe_then_every_read() -> None:
    store = _native_store({"a": frame(b"alpha"), "b": frame(b"beta")})
    rows, stale = ops.store_get_many(store, "segments", ["a", "missing", "b"])
    assert rows == {"a": b"alpha", "missing": None, "b": b"beta"}
    assert stale == []
    kinds = [kind for kind, _ in store._client.calls]
    assert kinds == ["size", "size", "size", "get", "get"]  # all probes before any read


def test_store_get_many_hands_back_a_body_shorter_than_its_frame() -> None:
    whole = frame(b"x" * 64)
    store = _native_store({"short": whole})
    store._client.bodies = {"short": whole}
    store._client.AsyncGetBlob = lambda tag, name, size, off=0: _Ready(value=whole[:-8])
    rows, stale = ops.store_get_many(store, "segments", ["short"])
    assert stale == ["short"] and rows["short"] is None  # re-read singly by the caller


def test_store_get_many_refuses_a_failed_probe_typed() -> None:
    store = _native_store({})
    store._client.AsyncGetBlobSize = lambda tag, name, query: _Ready(code=7)
    with pytest.raises(RuntimeError, match="GetBlobSize failed"):
        ops.store_get_many(store, "segments", ["x"])


# ---- the real store --------------------------------------------------------------------


def test_clio_core_get_answers_from_the_pass_and_reads_again_after_a_write(
    arc: ARCMemory,
) -> None:
    store: Any = arc._store
    store.put("segments", "ra-a", b"first")
    with read_ahead.reading_ahead(store):
        read_ahead.prefetch(store, "segments", ["ra-a", "ra-missing"])
        calls: list[str] = []
        original = store._get_one
        store._get_one = lambda kind, name: calls.append(name) or original(kind, name)
        assert store.get("segments", "ra-a") == b"first"
        assert store.get("segments", "ra-missing") is None
        assert calls == []  # both answered from the read-ahead
        store.put("segments", "ra-a", b"second")  # the write drops it from the buffer
        assert store.get("segments", "ra-a") == b"second"
        assert calls == ["ra-a"]
    assert store.get_many("segments", ["ra-a", "ra-missing"]) == {
        "ra-a": b"second",
        "ra-missing": None,
    }
