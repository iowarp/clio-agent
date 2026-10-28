"""Deferred obligation closes: the index build's settlement deletes its records in batches."""

from __future__ import annotations

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact.task_index_records import OPEN_KIND, TaskIndex


def _row(name: str) -> tuple[str, dict[str, str]]:
    return name, {"handle": name, "session_id": "s", "kind": "Session"}


def test_closes_held_in_the_block_are_deleted_when_it_ends() -> None:
    store = _MemoryStore()
    index = TaskIndex(store)
    for name in ("t~a", "t~b", "t~c"):
        index.put_open(_row(name))
    assert [name for name, _ in index.open_records()] == ["t~a", "t~b", "t~c"]

    with index.deferred_closes():
        index.delete_open("t~a")
        index.delete_open("t~b")
        # Held: still stored, already gone from what this process reads as open.
        assert store.exists(OPEN_KIND, "t~a") and store.exists(OPEN_KIND, "t~b")
        assert [name for name, _ in index.open_records()] == ["t~c"]
        index.put_open(_row("t~b"))  # reopened before its close was deleted: it stays

    assert not store.exists(OPEN_KIND, "t~a")
    assert store.exists(OPEN_KIND, "t~b") and store.exists(OPEN_KIND, "t~c")
    assert [name for name, _ in index.open_records()] == ["t~b", "t~c"]


def test_closes_outside_the_block_delete_at_once() -> None:
    store = _MemoryStore()
    index = TaskIndex(store)
    index.put_open(_row("t~a"))
    index.delete_open("t~a")
    assert not store.exists(OPEN_KIND, "t~a")
