"""Session metadata as clio-core records, and the paged conversation list.

* a change to one session writes that one record (plus its index bucket only when an
  indexed column moved), never every session's record;
* the in-memory index answers pages newest interaction first with an opaque keyset
  cursor and the total, kinds/workspace/archive filters, a conversation's tree, the
  sessions waiting on a person, named ids and a title search -- no record is read for
  a session a view does not return;
* a restart (a fresh store on the same record store) loads the index from its buckets;
* the legacy ``sessions.json`` is imported by the ``session_records_import`` store
  migration, never by opening the store;
* ``GET /v1/sessions`` keeps its unpaged shape and pages with ``limit``/``cursor``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact import store_migrations
from clio_agent.gact.routes.session_list import register_session_list_route
from clio_agent.gact.session_records import (
    KIND,
    InvalidCursorError,
    PageQuery,
    SessionRecords,
    SessionRecordsNotReadyError,
    bucket_of,
    encode_cursor,
)
from clio_agent.gact.sessions import SessionStore


class _CountingStore(_MemoryStore):
    """A record store that counts what each change reads and writes."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []
        self.gets: list[str] = []

    def put_many(self, kind: str, records: Any) -> None:
        self.puts.extend(record.name for record in records)
        super().put_many(kind, records)

    def get(self, kind: str, name: str) -> bytes | None:
        self.gets.append(name)
        return super().get(kind, name)


def _store(backing: Any, count: int, **create: Any) -> tuple[SessionStore, list[str]]:
    store = SessionStore(store=backing)
    ids = [store.create(workspace_id="ws", title=f"t{i}", **create).id for i in range(count)]
    store.flush()
    return store, ids


def test_one_update_writes_one_record_and_moves_one_bucket() -> None:
    backing = _CountingStore()
    store, ids = _store(backing, 25)
    backing.puts.clear()
    store.update(ids[3], metadata_patch={"note": "x"})  # no indexed column moved
    store.flush()
    assert backing.puts == [f"row/{ids[3]}"]
    backing.puts.clear()
    store.update(ids[3], title="renamed")
    store.flush()
    assert sorted(backing.puts) == sorted([f"row/{ids[3]}", bucket_of(ids[3])])
    assert SessionStore(store=backing).get(ids[3]).title == "renamed"


def test_pages_walk_every_session_newest_interaction_first() -> None:
    store, ids = _store(_MemoryStore(), 7)
    store.update(ids[0], message_count=2)  # the oldest becomes the most recent
    seen: list[str] = []
    cursor = None
    while True:
        rows, cursor, total = store.page(PageQuery(workspace_id="ws", limit=3, cursor=cursor))
        assert total == 7
        seen.extend(row.id for row in rows)
        if cursor is None:
            break
    assert seen == [ids[0], *reversed(ids[1:])]


def test_a_page_reads_only_its_own_records() -> None:
    backing = _CountingStore()
    _store(backing, 40)
    restarted = SessionStore(store=backing)
    backing.gets.clear()
    rows, _, total = restarted.page(PageQuery(workspace_id="ws", limit=5))
    assert total == 40 and len(rows) == 5
    assert sorted(backing.gets) == sorted(f"row/{row.id}" for row in rows)


def test_page_filters_kinds_archive_workspace_and_title() -> None:
    store, ids = _store(_MemoryStore(), 3)
    child = store.create(
        workspace_id="ws", title="child", parent_session_id=ids[0], agent={"id": "worker"}
    )
    store.create(workspace_id="other", title="elsewhere")
    store.update(ids[1], archived=True)
    rows, cursor, total = store.page(
        PageQuery(workspace_id="ws", limit=10, kinds=frozenset({"conversation", "branch"}))
    )
    assert {row.id for row in rows} == {ids[0], ids[2]}
    assert (cursor, total) == (None, 2)
    rows, _, _ = store.page(PageQuery(workspace_id="ws", limit=10, kinds=frozenset({"agent"})))
    assert [row.id for row in rows] == [child.id]
    rows, _, _ = store.page(PageQuery(limit=10, search="ELSE"))
    assert [row.title for row in rows] == ["elsewhere"]


def test_family_waiting_and_named_sessions_come_from_the_index() -> None:
    store, ids = _store(_MemoryStore(), 2)
    child = store.create(
        workspace_id="other", title="child", parent_session_id=ids[0], agent={"id": "w"}
    )
    grandchild = store.create(
        workspace_id="other", title="grand", parent_session_id=child.id, agent={"id": "w"}
    )
    assert [row.id for row in store.related(family_of=grandchild.id)] == [
        ids[0],
        child.id,
        grandchild.id,
    ]
    assert store.related(family_of="sess_unknown") == []
    store.update(grandchild.id, status="waiting_permission")
    assert {row.id for row in store.related()} == {ids[0], child.id, grandchild.id}
    rows = store.select(PageQuery(archived=None, ids=frozenset({ids[1], "missing"})))
    assert [row.id for row in rows] == [ids[1]]
    assert store.child_ids_by_parent() == {ids[0]: [child.id], child.id: [grandchild.id]}


def test_a_restart_loads_the_index_and_reads_no_record_up_front() -> None:
    backing = _CountingStore()
    _, ids = _store(backing, 3)
    backing.gets.clear()
    restarted = SessionStore(store=backing)
    assert all(name.startswith("index/") for name in backing.gets)
    assert restarted.resident_count() == 0 and restarted.count() == 3
    first = restarted.get(ids[1])
    assert first is restarted.get(ids[1])
    restarted.update(ids[1], title="same object")
    assert first is not None and first.title == "same object"
    restarted.delete(ids[2])
    assert sorted(SessionStore(store=backing).ids()) == sorted(ids[:2])


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_SESSIONS_CACHE_RECORDS", "2")
    store, ids = _store(_MemoryStore(), 5)
    for sid in ids:
        store.get(sid)
    assert store.resident_count() == 2


def test_records_made_before_the_store_is_bound_move_into_it() -> None:
    store = SessionStore()
    early = store.create(workspace_id="ws", title="early")
    backing = _MemoryStore()
    store.bind_store(backing)
    store.flush()
    assert (KIND, f"row/{early.id}") in backing._data
    assert SessionStore(store=backing).get(early.id).title == "early"


def test_an_announced_store_refuses_reads_until_bound_on_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = SessionRecords()
    records.expect_store()
    monkeypatch.setattr("clio_agent.gact.session_records._on_server_loop", lambda: True)
    with pytest.raises(SessionRecordsNotReadyError):
        records.count()
    records.bind(_MemoryStore())
    assert records.count() == 0


def _migration_app(store: SessionStore) -> Any:
    class _Index:
        def __init__(self) -> None:
            self.markers: dict[str, Any] = {}

        def done_marker(self, name: str) -> Any:
            return self.markers.get(name)

        def put_done_marker(self, name: str, value: Any) -> None:
            self.markers[name] = value

    history = SimpleNamespace(index=_Index(), confirm_write=lambda write: write())
    return SimpleNamespace(state=SimpleNamespace(sessions=store, task_history=history))


def test_legacy_json_is_imported_once_by_the_store_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "sessions.json"
    legacy.write_text(
        json.dumps(
            {
                "sess_old": {
                    "id": "sess_old",
                    "workspace_id": "ws",
                    "title": "old",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
                "bad": {"unknown_field": 1},
            }
        )
    )
    store = SessionStore(path=legacy, store=_MemoryStore())
    assert store.count() == 0  # opening the store never imports
    app = _migration_app(store)
    item = store_migrations.migration("session_records_import")
    first = store_migrations.run_migration(app, item)
    assert (first.status, first.details) == ("done", {"sessions": 1})
    assert [row.id for row in store.list()] == ["sess_old"]
    assert [row.id for row in store.sessions_without_interaction_time()] == ["sess_old"]
    assert store_migrations.run_migration(app, item).status == "already_done"
    # A rerun after an interruption keeps what is there and adds nothing twice.
    store.update("sess_old", title="kept")
    assert store.import_legacy() == {"sessions": 0}
    assert store.get("sess_old").title == "kept"


def test_foreign_cursor_is_refused() -> None:
    store, _ = _store(_MemoryStore(), 1)
    with pytest.raises(InvalidCursorError):
        store.page(PageQuery(limit=1, cursor="not-a-cursor"))
    rows, _, _ = store.page(PageQuery(limit=1, cursor=encode_cursor("9999", "sess_z")))
    assert len(rows) == 1


def _client(store: SessionStore) -> TestClient:
    app = FastAPI()
    app.state.sessions = store
    register_session_list_route(app)
    return TestClient(app)


def test_route_unpaged_shape_is_unchanged() -> None:
    store, ids = _store(_MemoryStore(), 3)
    body = _client(store).get("/v1/sessions?workspace_id=ws").json()
    assert [row["id"] for row in body["sessions"]] == list(reversed(ids))
    assert body["next_cursor"] is None and body["total"] is None


def test_route_pages_relations_and_search() -> None:
    store, ids = _store(_MemoryStore(), 5)
    child = store.create(
        workspace_id="ws", title="kid", parent_session_id=ids[0], agent={"id": "w"}
    )
    store.update(child.id, status="waiting_user")
    client = _client(store)
    first = client.get("/v1/sessions?workspace_id=ws&limit=2&kinds=conversation,branch").json()
    assert [row["id"] for row in first["sessions"]] == [ids[4], ids[3]]
    assert first["total"] == 5 and first["next_cursor"]
    second = client.get(
        f"/v1/sessions?workspace_id=ws&limit=2&kinds=conversation&cursor={first['next_cursor']}"
    ).json()
    assert [row["id"] for row in second["sessions"]] == [ids[2], ids[1]]
    family = client.get(f"/v1/sessions?family_of={child.id}").json()["sessions"]
    assert [row["id"] for row in family] == [ids[0], child.id]
    waiting = client.get("/v1/sessions?waiting_on_person=true").json()["sessions"]
    assert {row["id"] for row in waiting} == {ids[0], child.id}
    named = client.get(f"/v1/sessions?ids={ids[1]},{ids[2]}").json()["sessions"]
    assert {row["id"] for row in named} == {ids[1], ids[2]}
    found = client.get("/v1/sessions?workspace_id=ws&q=KI&limit=10").json()["sessions"]
    assert [row["id"] for row in found] == [child.id]
    assert client.get("/v1/sessions?limit=0").status_code == 422
    assert client.get("/v1/sessions?limit=2&cursor=garbage").status_code == 422
    assert client.get("/v1/sessions?limit=2&kinds=nonsense").status_code == 422
