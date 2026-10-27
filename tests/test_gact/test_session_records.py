"""R3: session metadata is one row per session, and the conversation list is paged.

* a change to one session writes that one row, never every session's record;
* ``SessionStore.page`` reads a page by range, newest interaction first, with an
  opaque keyset cursor and the total;
* the legacy ``sessions.json`` is imported once;
* ``GET /v1/sessions`` keeps its unpaged shape and pages with ``limit``/``cursor``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.session_list import register_session_list_route
from clio_agent.gact.session_records import InvalidCursorError, PageQuery, encode_cursor
from clio_agent.gact.sessions import SessionStore


def _store(tmp_path: Path, count: int, **create: object) -> tuple[SessionStore, list[str]]:
    store = SessionStore(path=tmp_path / "sessions.json")
    ids = [store.create(workspace_id="ws", title=f"t{i}", **create).id for i in range(count)]
    return store, ids


def test_one_update_writes_one_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, ids = _store(tmp_path, 25)
    writes: list[str] = []
    real_put = store._records.put

    def put(columns, body):  # type: ignore[no-untyped-def]
        writes.append(columns.id)
        real_put(columns, body)

    monkeypatch.setattr(store._records, "put", put)
    store.update(ids[3], title="renamed")
    assert writes == [ids[3]]
    assert not (tmp_path / "sessions.json").exists()
    assert SessionStore(path=tmp_path / "sessions.json").get(ids[3]).title == "renamed"


def test_pages_walk_every_row_newest_interaction_first(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, 7)
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


def test_page_filters_kinds_archive_and_workspace(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, 3)
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


def test_rows_are_read_on_demand_and_identity_is_kept(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, 3)
    reopened = SessionStore(path=tmp_path / "sessions.json")
    assert reopened.resident_count() == 0  # boot reads no row
    first = reopened.get(ids[1])
    assert first is reopened.get(ids[1])
    reopened.update(ids[1], title="same object")
    assert first is not None and first.title == "same object"
    assert reopened.ids() and set(reopened.ids()) == set(ids)
    assert reopened.child_ids_by_parent() == {}


def test_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_SESSIONS_CACHE_RECORDS", "2")
    store, ids = _store(tmp_path, 5)
    for sid in ids:
        store.get(sid)
    assert store.resident_count() == 2


def test_legacy_json_is_imported_once(tmp_path: Path) -> None:
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
    store = SessionStore(path=legacy)
    assert [row.id for row in store.list()] == ["sess_old"]
    assert [row.id for row in store.sessions_without_interaction_time()] == ["sess_old"]
    store.delete("sess_old")
    # The marker keeps a later start from importing the (unchanged) legacy file again.
    assert SessionStore(path=legacy).count() == 0
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT value FROM meta").fetchone() == ("1",)


def test_foreign_cursor_is_refused(tmp_path: Path) -> None:
    store, _ = _store(tmp_path, 1)
    with pytest.raises(InvalidCursorError):
        store.page(PageQuery(limit=1, cursor="not-a-cursor"))
    rows, _, _ = store.page(PageQuery(limit=1, cursor=encode_cursor("9999", "sess_z")))
    assert len(rows) == 1


def _client(store: SessionStore) -> TestClient:
    app = FastAPI()
    app.state.sessions = store
    register_session_list_route(app)
    return TestClient(app)


def test_route_unpaged_shape_is_unchanged(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, 3)
    body = _client(store).get("/v1/sessions?workspace_id=ws").json()
    assert [row["id"] for row in body["sessions"]] == list(reversed(ids))
    assert body["next_cursor"] is None and body["total"] is None


def test_route_pages_with_cursor(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, 5)
    client = _client(store)
    first = client.get("/v1/sessions?workspace_id=ws&limit=2&kinds=conversation,branch").json()
    assert [row["id"] for row in first["sessions"]] == [ids[4], ids[3]]
    assert first["total"] == 5 and first["next_cursor"]
    second = client.get(
        f"/v1/sessions?workspace_id=ws&limit=2&kinds=conversation&cursor={first['next_cursor']}"
    ).json()
    assert [row["id"] for row in second["sessions"]] == [ids[2], ids[1]]
    assert client.get("/v1/sessions?limit=0").status_code == 422
    assert client.get("/v1/sessions?limit=2&cursor=garbage").status_code == 422
    assert client.get("/v1/sessions?limit=2&kinds=nonsense").status_code == 422
