"""Appended artifact-index lists are written a page at a time.

A session's transform list was rewritten whole on every tool call: on P7 the
2,000th call re-encoded and put 2,000 ids (2026-11-02). Each append now writes only
the list's last page of at most ``artifacts.index_page_items`` ids (default 256).
"""

from __future__ import annotations

import json
from typing import Any

from clio_agent.gact.artifacts import registry_store
from clio_agent.gact.artifacts.registry_store import StoreBackend, index_key


class _Store:
    def __init__(self) -> None:
        self.records: dict[str, bytes] = {}
        self.put_sizes: list[int] = []

    def get(self, _kind: str, name: str) -> bytes | None:
        return self.records.get(name)

    def put(self, _kind: str, name: str, data: bytes) -> None:
        self.records[name] = data
        self.put_sizes.append(len(json.loads(data)) if data.startswith(b"[") else 0)


def _backend(store: _Store, **kwargs: Any) -> StoreBackend:
    return StoreBackend(store, event_check=lambda _event_id: True, **kwargs)  # type: ignore[arg-type]


def test_appends_write_one_bounded_page_and_read_back_in_order() -> None:
    """SABOTAGE: rewrite the whole list in ``_append`` and a put carries 600 ids."""
    registry_store._page_items.cache_clear()
    store = _Store()
    backend = _backend(store)
    key = index_key("s", "sess_1")
    ids = [f"call_{n:04d}" for n in range(600)]
    for call_id in ids:
        backend._append(key, call_id)
    backend._append(key, ids[-1])  # a repeat is not appended again

    assert max(store.put_sizes) <= 256  # one page, never the whole 600-id list
    assert backend._members(key) == ids
    # Read back cold, from the records alone.
    assert _backend(store)._members(key) == ids


def test_a_whole_list_written_by_the_migration_still_reads_and_grows() -> None:
    store = _Store()
    key = index_key("s", "sess_1")
    legacy = [f"call_{n:04d}" for n in range(300)]
    store.records[key] = registry_store.encode(legacy)
    backend = _backend(store)
    backend._append(key, "call_new")
    assert backend._members(key) == [*legacy, "call_new"]


def test_a_live_transform_neither_reads_clio_core_nor_puts_under_the_registry_lock() -> None:
    """A transform of a call id minted here: no read, and its puts are queued in order.

    Every concurrent tool call takes the registry lock; on a P7 burst it was held
    across a read (always a miss for a fresh id) and two puts, and waiting for it was
    most of a call's own time (2026-11-03).

    SABOTAGE: ignore ``cached_only`` in ``StoreBackend.transform`` and the fresh id
    is read from the store; or call ``put()`` in ``_write`` regardless of ``_behind``
    and the puts happen inside the lock.
    """
    from clio_agent.gact.artifacts.registry import ArtifactRegistry
    from clio_agent.gact.artifacts.transform_types import Instrument
    from clio_agent.gact.artifacts.transforms import TransformRecord, TransformStatus

    reads: list[str] = []
    store = _Store()
    original_get = store.get

    def get(kind: str, name: str) -> bytes | None:
        reads.append(name)
        return original_get(kind, name)

    store.get = get  # type: ignore[method-assign]
    backend = _backend(store)
    registry = ArtifactRegistry(backend=backend)
    queued: list[Any] = []
    record = TransformRecord(
        call_id="call_fresh",
        event_id="evt_1",
        session_id="sess_1",
        workspace_id="ws",
        status=TransformStatus.SUCCESS,
        instrument=Instrument(tool="records_get_record", args={}),
        started_at="2026-11-03T00:00:00Z",
        ended_at="2026-11-03T00:00:01Z",
    )
    assert registry.record_transform(record, fresh=True, write_behind=queued.append) is True
    assert index_key("t", "call_fresh") not in reads  # no read of the fresh id
    assert store.records == {} and len(queued) == 2  # nothing put yet: queued in order
    for put in queued:  # the writer runs them later, outside the lock
        put()
    assert set(store.records) == {index_key("s", "sess_1"), index_key("t", "call_fresh")}
    assert registry.get_transform("call_fresh") is record  # the cache answers meanwhile
