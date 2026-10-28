"""session registry tests.

Covers Create/Get/List/Delete/Update, disk persistence roundtrip,
and resilience to corrupted on-disk state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact.session_records import KIND, PageQuery
from clio_agent.gact.sessions import SessionStore


@pytest.fixture()
def backing() -> _MemoryStore:
    """The clio-core record store a session store binds (in-process for unit tests)."""

    return _MemoryStore()


@pytest.fixture()
def store(tmp_path: Path, backing: _MemoryStore) -> SessionStore:
    """Store bound to a record store; a second store on ``backing`` is a restart."""

    return SessionStore(path=tmp_path / "sessions.json", store=backing)


@pytest.fixture()
def mem_store() -> SessionStore:
    """Purely in-memory store — used when persistence isn't under test."""

    return SessionStore(path=None)


def test_create_returns_session_with_id_and_timestamps(mem_store: SessionStore) -> None:
    sess = mem_store.create(workspace_id="ws_default", title="first")
    assert sess.id.startswith("sess_")
    assert sess.workspace_id == "ws_default"
    assert sess.title == "first"
    assert sess.status == "idle"
    assert sess.created_at  # non-empty
    assert sess.updated_at == sess.created_at  # matches on creation
    assert sess.last_interaction_at == sess.created_at
    assert sess.message_count == 0
    assert sess.metadata == {}
    assert sess.agent == {"id": "main"}
    assert sess.model == {}


def test_create_persists_agent_and_model_refs(mem_store: SessionStore) -> None:
    sess = mem_store.create(
        workspace_id="ws_default",
        title="with refs",
        agent={"id": "code_reviewer", "mode": "review"},
        model={"provider_id": "lm_studio", "model_id": "qwopus3.5-9b-v3"},
    )

    assert sess.agent == {"id": "code_reviewer", "mode": "review"}
    assert sess.model == {
        "provider_id": "lm_studio",
        "model_id": "qwopus3.5-9b-v3",
    }


def test_create_default_title_uses_id_suffix(mem_store: SessionStore) -> None:
    """Calling create() without a title gets a placeholder built from
    the session id — so GET /v1/sessions has something to render in
    the sidebar immediately."""

    sess = mem_store.create(workspace_id="ws_default")
    assert sess.title.startswith("session ")
    assert sess.id[-6:] in sess.title


def test_get_returns_same_record(mem_store: SessionStore) -> None:
    sess = mem_store.create(workspace_id="ws_default", title="x")
    got = mem_store.get(sess.id)
    assert got is not None
    assert got.id == sess.id
    assert got.title == "x"


def test_get_missing_returns_none(mem_store: SessionStore) -> None:
    assert mem_store.get("sess_does_not_exist") is None


def test_list_newest_first_and_filter_by_workspace(mem_store: SessionStore) -> None:
    s1 = mem_store.create(workspace_id="ws_a", title="a1")
    s2 = mem_store.create(workspace_id="ws_b", title="b1")
    s3 = mem_store.create(workspace_id="ws_a", title="a2")

    # Unfiltered: newest first (by created_at).
    rows = mem_store.select(PageQuery(archived=None))
    assert [s.id for s in rows[:3]] == [s3.id, s2.id, s1.id], (
        f"expected newest-first, got {[s.id for s in rows]}"
    )

    # Filter by workspace.
    ws_a = mem_store.select(PageQuery(workspace_id="ws_a", archived=None))
    assert {s.id for s in ws_a} == {s1.id, s3.id}


def test_update_patches_fields_and_bumps_updated_at(
    mem_store: SessionStore,
) -> None:
    sess = mem_store.create(workspace_id="ws", title="old")
    original_updated = sess.updated_at
    original_interaction = sess.last_interaction_at

    patched = mem_store.update(
        sess.id,
        title="new",
        status="running",
        metadata_patch={"k": "v"},
        agent={"id": "data"},
        model={"provider_id": "openai", "model_id": "gpt-4o-mini"},
    )
    assert patched is not None
    assert patched.title == "new"
    assert patched.status == "running"
    assert patched.metadata == {"k": "v"}
    assert patched.agent == {"id": "data"}
    assert patched.model == {"provider_id": "openai", "model_id": "gpt-4o-mini"}
    # updated_at at least not earlier than creation (clock could be
    # equal if called in the same second — allow equality).
    assert patched.updated_at >= original_updated
    assert patched.last_interaction_at == original_interaction

    interacted = mem_store.update(sess.id, message_count=2)
    assert interacted is not None
    assert interacted.last_interaction_at == interacted.updated_at
    assert interacted.last_interaction_at > original_interaction


def test_legacy_session_interaction_time_waits_for_the_transcript_store(
    tmp_path: Path, backing: _MemoryStore
) -> None:
    """A row older than ``last_interaction_at`` reads "" until the transcript store's
    boot settles it (file mtime or last atom message -- test_transcript_replace_atomic);
    the registry itself never reads a transcript (no ``messages/`` stat here)."""

    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps(
            {
                "sess_legacy": {
                    "id": "sess_legacy",
                    "workspace_id": "ws",
                    "title": "legacy",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-23T03:00:00+00:00",
                }
            }
        )
    )

    store = SessionStore(path=sessions_path, store=backing)
    assert store.get("sess_legacy") is None
    assert store.import_legacy() == {"sessions": 1}  # the session_records_import migration
    session = store.get("sess_legacy")
    assert session is not None
    assert session.last_interaction_at == ""
    assert [row.id for row in store.sessions_without_interaction_time()] == ["sess_legacy"]

    store.settle_interaction_times({"sess_legacy": "2026-08-22T18:21:00+00:00"})
    assert store.get("sess_legacy").last_interaction_at == "2026-08-22T18:21:00+00:00"
    assert store.sessions_without_interaction_time() == []
    reloaded = SessionStore(path=sessions_path, store=backing).get("sess_legacy")
    assert reloaded is not None
    assert reloaded.last_interaction_at == "2026-08-22T18:21:00+00:00"


def test_settle_keeps_an_interaction_time_set_meanwhile(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps(
            {
                "sess_legacy": {
                    "id": "sess_legacy",
                    "workspace_id": "ws",
                    "title": "legacy",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-23T03:00:00+00:00",
                }
            }
        )
    )
    store = SessionStore(path=sessions_path)
    store.import_legacy()
    live = store.update("sess_legacy", message_count=3)
    assert live is not None and live.last_interaction_at
    store.settle_interaction_times({"sess_legacy": "2026-08-22T18:21:00+00:00"})
    assert store.get("sess_legacy").last_interaction_at == live.last_interaction_at


def test_update_unknown_returns_none(mem_store: SessionStore) -> None:
    assert mem_store.update("sess_nope", title="x") is None


def test_delete_returns_true_then_false(mem_store: SessionStore) -> None:
    sess = mem_store.create(workspace_id="ws", title="gone")
    assert mem_store.delete(sess.id) is True
    assert mem_store.get(sess.id) is None
    assert mem_store.delete(sess.id) is False


def test_persistence_roundtrip(tmp_path: Path, backing: _MemoryStore) -> None:
    """A store mutated, then discarded -- and a fresh store on the same record
    store (a restart) -- must see the same records."""

    path = tmp_path / "sessions.json"

    a = SessionStore(path=path, store=backing)
    s1 = a.create(workspace_id="ws", title="keep")
    s2 = a.create(workspace_id="ws", title="also")
    _ = a  # drop reference

    b = SessionStore(path=path, store=backing)
    got = b.select(PageQuery(archived=None))
    assert {s.id for s in got} == {s1.id, s2.id}


def test_persistence_is_one_record_per_session(tmp_path: Path, backing: _MemoryStore) -> None:
    """Each session is one clio-core record; no whole-registry JSON file is written."""
    path = tmp_path / "sessions.json"
    store = SessionStore(path=path, store=backing)
    sess = store.create(workspace_id="ws", title="x")
    rows = [name for kind, name in backing._data if kind == KIND and name.startswith("row/")]
    assert rows == [f"row/{sess.id}"]
    assert not path.exists()
    assert SessionStore(path=path, store=backing).count() == 1


def test_corrupted_store_file_starts_empty(tmp_path: Path, backing: _MemoryStore) -> None:
    """Resilience: a half-written / malformed legacy JSON file must not crash the
    server. The import is refused typed (so the migration stays owed and a repaired
    file is imported later); new sessions can be created."""

    path = tmp_path / "sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    store = SessionStore(path=path, store=backing)
    with pytest.raises(ValueError, match="session_records_legacy_unreadable"):
        store.import_legacy()
    assert store.ids() == []

    sess = store.create(workspace_id="ws", title="recovered")
    assert store.get(sess.id) is not None


def test_count_tracks_create_delete(mem_store: SessionStore) -> None:
    assert mem_store.count() == 0
    s1 = mem_store.create(workspace_id="ws", title="a")
    mem_store.create(workspace_id="ws", title="b")
    assert mem_store.count() == 2
    mem_store.delete(s1.id)
    assert mem_store.count() == 1


def test_each_write_is_committed_before_the_call_returns(
    tmp_path: Path, backing: _MemoryStore
) -> None:
    """Off the server loop, a change lands in the record store before the call returns."""

    store = SessionStore(path=tmp_path / "sessions.json", store=backing)
    sess = store.create(workspace_id="ws", title="durable")
    assert (KIND, f"row/{sess.id}") in backing._data
    store.update(sess.id, title="renamed")
    assert SessionStore(store=backing).get(sess.id).title == "renamed"


class _RecordFailed(RuntimeError):
    pass


def _failing_observer(event_type: str, _session: object) -> None:
    raise _RecordFailed(event_type)


def test_a_session_whose_creation_cannot_be_recorded_is_not_created(store: SessionStore) -> None:
    """The lifecycle record is clio-core's: a failure is the caller's error, never a log
    line, and no session exists that clio-core has no record of."""
    store.set_lifecycle_observer(_failing_observer)

    with pytest.raises(_RecordFailed, match="session.created"):
        store.create(workspace_id="ws_default", title="unrecorded")

    assert store.ids() == []
    assert SessionStore(store=store._records._writer.store).ids() == []


def test_a_deletion_that_cannot_be_recorded_is_the_callers_error(store: SessionStore) -> None:
    sess = store.create(workspace_id="ws_default", title="t")
    store.set_lifecycle_observer(_failing_observer)

    with pytest.raises(_RecordFailed, match="session.deleted"):
        store.delete(sess.id)
