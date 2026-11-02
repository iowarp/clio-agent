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
