"""session registry tests.

Covers Create/Get/List/Delete/Update, disk persistence roundtrip,
and resilience to corrupted on-disk state.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clio_agent.gact.sessions import SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    """File-backed store; tmp_path isolates each test."""

    return SessionStore(path=tmp_path / "sessions.json")


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
    rows = mem_store.list()
    assert [s.id for s in rows[:3]] == [s3.id, s2.id, s1.id], (
        f"expected newest-first, got {[s.id for s in rows]}"
    )

    # Filter by workspace.
    ws_a = mem_store.list(workspace_id="ws_a")
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


def test_legacy_session_recovers_last_interaction_from_message_ledger(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    messages_path = tmp_path / "messages"
    messages_path.mkdir()
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
    ledger = messages_path / "sess_legacy.json"
    ledger.write_text("[]")
    observed = datetime(2026, 8, 22, 18, 21, tzinfo=timezone.utc).timestamp()
    os.utime(ledger, (observed, observed))

    session = SessionStore(path=sessions_path).get("sess_legacy")

    assert session is not None
    assert session.last_interaction_at == "2026-08-22T18:21:00+00:00"


def test_update_unknown_returns_none(mem_store: SessionStore) -> None:
    assert mem_store.update("sess_nope", title="x") is None


def test_delete_returns_true_then_false(mem_store: SessionStore) -> None:
    sess = mem_store.create(workspace_id="ws", title="gone")
    assert mem_store.delete(sess.id) is True
    assert mem_store.get(sess.id) is None
    assert mem_store.delete(sess.id) is False


def test_persistence_roundtrip(tmp_path: Path) -> None:
    """A store created with path=X, mutated, then discarded — and a
    fresh store with the same path — must see the same records."""

    path = tmp_path / "sessions.json"

    a = SessionStore(path=path)
    s1 = a.create(workspace_id="ws", title="keep")
    s2 = a.create(workspace_id="ws", title="also")
    _ = a  # drop reference

    b = SessionStore(path=path)
    got = b.list()
    assert {s.id for s in got} == {s1.id, s2.id}


def test_persistence_file_is_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path=path)
    store.create(workspace_id="ws", title="x")
    # File exists after the first write.
    assert path.exists()
    # Valid JSON, keyed by session id.
    raw = json.loads(path.read_text())
    assert isinstance(raw, dict)
    assert len(raw) == 1


def test_corrupted_store_file_starts_empty(tmp_path: Path) -> None:
    """Resilience: a half-written / malformed JSON file must not
    crash the server on boot. The store just starts empty so new
    sessions can be created."""

    path = tmp_path / "sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    store = SessionStore(path=path)
    assert store.list() == []

    # And future creates still work + overwrite the corrupted file.
    sess = store.create(workspace_id="ws", title="recovered")
    assert store.get(sess.id) is not None


def test_count_tracks_create_delete(mem_store: SessionStore) -> None:
    assert mem_store.count() == 0
    s1 = mem_store.create(workspace_id="ws", title="a")
    mem_store.create(workspace_id="ws", title="b")
    assert mem_store.count() == 2
    mem_store.delete(s1.id)
    assert mem_store.count() == 1


def test_flush_fsyncs_before_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#771 truth pass: the docstring's durability claim is now real.

    ``_flush`` must ``os.fsync`` the temp file's descriptor before the atomic
    rename publishes it, so a crash can't leave a half-written blob on disk.
    """

    import os as _os

    fsynced: list[int] = []
    real_fsync = _os.fsync

    def _spy_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("clio_agent.gact.sessions.os.fsync", _spy_fsync)

    store = SessionStore(path=tmp_path / "sessions.json")
    store.create(workspace_id="ws", title="durable")

    assert fsynced, "os.fsync was never called on the persisted session file"
    # And the record actually landed on disk.
    reloaded = SessionStore(path=tmp_path / "sessions.json")
    assert reloaded.count() == 1
