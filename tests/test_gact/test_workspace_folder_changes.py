"""Moved workspace recovery and additional folder permissions (#1766)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.runtime import grants
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.gact.types import Message, Part
from clio_agent.gact.workspaces import WorkspaceStore
from clio_agent.runtime import sandbox_roots
from clio_agent.tools.execution import get_active_tool_workspace_root, tool_workspace_context
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError


@pytest.fixture(autouse=True)
def clean_root_grants() -> Iterator[None]:
    """Keep the process-wide grant projection isolated from other tests."""
    sandbox_roots.clear_write_root_grants()
    yield
    sandbox_roots.clear_write_root_grants()


def test_moved_primary_preserves_sessions_grants_and_next_turn_context(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    original = tmp_path / "Classes" / "Research"
    original.mkdir(parents=True)
    (original / "notes.txt").write_text("research", encoding="utf-8")
    wid = client.post(
        "/v1/workspaces", json={"name": "Research", "root_path": str(original)}
    ).json()["id"]
    sid = client.post(
        "/v1/sessions", json={"workspace_id": wid, "title": "Keep this conversation"}
    ).json()["id"]
    message = Message(
        id="retained-note",
        session_id=sid,
        role="user",
        created_at="2026-10-15T00:00:00Z",
        updated_at="2026-10-15T00:00:00Z",
        parts=[Part(id="retained-text", type="text", text="Keep these notes")],
    )
    app.state.messages[sid] = [message]
    app.state.message_store.replace_session(sid, [message])
    session_before = client.get(f"/v1/sessions/{sid}").json()
    messages_before = client.get(f"/v1/sessions/{sid}/messages").json()
    assert messages_before["messages"][0]["parts"][0]["text"] == "Keep these notes"
    additional = tmp_path / "Shared sources"
    additional.mkdir()
    (additional / "source.txt").write_text("source", encoding="utf-8")
    granted = client.post(
        f"/v1/workspaces/{wid}/grants", json={"kind": "fs_root", "pattern": str(additional)}
    )
    assert granted.status_code == 200
    moved = tmp_path / "Research"
    original.rename(moved)

    response = client.patch(f"/v1/workspaces/{wid}", json={"root_path": str(moved)})

    assert response.status_code == 200
    assert not original.exists()
    assert client.get(f"/v1/sessions/{sid}").json() == session_before
    assert client.get(f"/v1/sessions/{sid}/messages").json() == messages_before
    assert (
        client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "notes.txt"}).text
        == "research"
    )
    with _gact_app_context(app), _tool_session_context(sid):
        assert Path(get_active_tool_workspace_root()) == moved
        policy = FileAccessPolicy.from_mapping({"CLIO_ALLOWED_ROOTS": str(tmp_path / "unrelated")})
        assert policy.validate_read(str(additional / "source.txt")) == additional / "source.txt"
    assert sandbox_roots.granted_write_roots(str(original)) == ()
    assert sandbox_roots.granted_write_roots(str(moved)) == (additional.resolve(),)
    persisted = WorkspaceStore(path=app.state.workspaces._path).get(wid)
    assert persisted is not None
    assert persisted.root_path == str(moved)
    assert persisted.config[grants.GRANTED_ROOTS_CONFIG_KEY] == [str(additional.resolve())]


def test_root_change_rejects_busy_workspace_before_creating_or_renaming(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    root = tmp_path / "original"
    wid = client.post("/v1/workspaces", json={"name": "Original", "root_path": str(root)}).json()[
        "id"
    ]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    app.state.in_flight_turns[sid] = Mock(done=Mock(return_value=False))
    destination = tmp_path / "must-not-create"
    try:
        response = client.patch(
            f"/v1/workspaces/{wid}", json={"name": "Changed", "root_path": str(destination)}
        )
        assert response.status_code == 409
        assert response.json()["error"]["error"] == "workspace_busy"
        assert not destination.exists()
        stored = app.state.workspaces.get(wid)
        assert stored.name == "Original"
        assert stored.root_path == str(root)
    finally:
        app.state.in_flight_turns.pop(sid)


def test_additional_folder_survives_reload_and_revocation_denies_access(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    root = tmp_path / "workspace"
    additional = tmp_path / "additional"
    additional.mkdir()
    target = additional / "source.txt"
    target.write_text("source", encoding="utf-8")
    wid = client.post("/v1/workspaces", json={"name": "Research", "root_path": str(root)}).json()[
        "id"
    ]
    response = client.post(
        f"/v1/workspaces/{wid}/grants", json={"kind": "fs_root", "pattern": str(additional)}
    )
    assert response.status_code == 200
    sandbox_roots.clear_write_root_grants()
    app.state.workspaces = WorkspaceStore(path=app.state.workspaces._path)
    grants.replay_persisted_root_grants(app)
    with tool_workspace_context(str(root)):
        policy = FileAccessPolicy.from_mapping({"CLIO_ALLOWED_ROOTS": str(tmp_path / "unrelated")})
        assert policy.validate_read(str(target)) == target
        assert policy.validate_write(str(additional / "new.txt")) == additional / "new.txt"
        roots = sandbox_roots.effective_write_roots(
            sandbox_roots.PROFILE_SHELL, policy=policy, workspace_root=str(root)
        )
        assert additional.resolve() in roots
    removed = client.delete(
        f"/v1/workspaces/{wid}/grants", params={"kind": "fs_root", "pattern": str(additional)}
    )
    assert removed.status_code == 200
    assert removed.json()["grant"]["revoked"] is True
    with tool_workspace_context(str(root)):
        policy = FileAccessPolicy.from_mapping({"CLIO_ALLOWED_ROOTS": str(tmp_path / "unrelated")})
        with pytest.raises(FilePolicyError, match="outside"):
            policy.validate_read(str(target))
        with pytest.raises(FilePolicyError, match="outside"):
            policy.validate_write(str(additional / "new.txt"))
    assert target.read_text(encoding="utf-8") == "source"
    assert (
        WorkspaceStore(path=app.state.workspaces._path)
        .get(wid)
        .config[grants.GRANTED_ROOTS_CONFIG_KEY]
        == []
    )


def test_repointing_does_not_revoke_another_workspace_grant(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    root = tmp_path / "shared-primary"
    first = app.state.workspaces.create(name="first", root_path=str(root))
    second = app.state.workspaces.create(name="second", root_path=str(root))
    shared = tmp_path / "shared-extra"
    only_first = tmp_path / "first-extra"
    grants.apply_root_grant(app, first.id, str(shared))
    grants.apply_root_grant(app, second.id, str(shared))
    grants.apply_root_grant(app, first.id, str(only_first))
    destination = tmp_path / "moved"
    assert (
        client.patch(f"/v1/workspaces/{first.id}", json={"root_path": str(destination)}).status_code
        == 200
    )
    assert sandbox_roots.granted_write_roots(str(root)) == (shared.resolve(),)
    assert set(sandbox_roots.granted_write_roots(str(destination))) == {
        shared.resolve(),
        only_first.resolve(),
    }


def test_unknown_workspace_patch_does_not_create_a_directory(tmp_path: Path) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    destination = tmp_path / "must-not-create"
    response = client.patch("/v1/workspaces/ws_missing", json={"root_path": str(destination)})
    assert response.status_code == 404
    assert response.json()["error"]["error"] == "not_found"
    assert not destination.exists()


def test_primary_change_retires_old_and_destination_fleets(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    original = tmp_path / "old"
    destination = tmp_path / "new"
    workspace = app.state.workspaces.create(name="research", root_path=str(original))
    restart = Mock(return_value="restart_no_resident")
    app.state.agent = Mock(request_fleet_restart=restart)
    response = TestClient(app).patch(
        f"/v1/workspaces/{workspace.id}", json={"root_path": str(destination)}
    )
    assert response.status_code == 200
    assert [call.args for call in restart.call_args_list] == [(str(original),), (str(destination),)]


@pytest.mark.parametrize("kind_dispatch", [True, False])
def test_workspace_folder_changes_publish_real_boundary_records(
    tmp_path: Path, kind_dispatch: bool
) -> None:
    """HTTP folder mutations must finish their real ARC-to-UI boundary writes."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    original, destination, additional = (tmp_path / name for name in ("old", "new", "extra"))
    wid = client.post(
        "/v1/workspaces", json={"name": "research", "root_path": str(original)}
    ).json()["id"]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    grant_body = (
        {"kind": "fs_root", "pattern": str(additional)}
        if kind_dispatch
        else {"root": str(additional)}
    )
    assert client.post(f"/v1/workspaces/{wid}/grants", json=grant_body).status_code == 200
    assert (
        client.patch(f"/v1/workspaces/{wid}", json={"root_path": str(destination)}).status_code
        == 200
    )
    assert (
        client.delete(
            f"/v1/workspaces/{wid}/grants", params={"kind": "fs_root", "pattern": str(additional)}
        ).status_code
        == 200
    )
    recorded = [
        (row.payload["event_type"], row.payload["payload"]["pattern"])
        for row in app.state.bus._history.get(sid, [])
        if row.type == "semantic.event" and row.payload["event_type"].startswith("boundary.")
    ]
    assert recorded == [
        ("boundary.granted", str(additional.resolve())),
        ("boundary.revoked", str(original)),
        ("boundary.granted", str(destination)),
        ("boundary.revoked", str(additional.resolve())),
    ]


def test_revocation_keeps_another_workspace_owner_at_the_same_primary(tmp_path: Path) -> None:
    """A shared-root registry entry stays while any workspace still owns its grant."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    primary, additional = tmp_path / "primary", tmp_path / "additional"
    first = app.state.workspaces.create(name="first", root_path=str(primary))
    second = app.state.workspaces.create(name="second", root_path=str(primary))
    grants.apply_root_grant(app, first.id, str(additional))
    grants.apply_root_grant(app, second.id, str(additional))
    response = TestClient(app).delete(
        f"/v1/workspaces/{first.id}/grants",
        params={"kind": "fs_root", "pattern": str(additional)},
    )
    assert response.status_code == 200
    assert response.json()["grant"]["revoked"] is True
    assert app.state.workspaces.get(first.id).config[grants.GRANTED_ROOTS_CONFIG_KEY] == []
    assert app.state.workspaces.get(second.id).config[grants.GRANTED_ROOTS_CONFIG_KEY] == [
        str(additional.resolve())
    ]
    assert sandbox_roots.granted_write_roots(str(primary)) == (additional.resolve(),)
    repeated = TestClient(app).delete(
        f"/v1/workspaces/{first.id}/grants",
        params={"kind": "fs_root", "pattern": str(additional)},
    )
    assert repeated.status_code == 200 and repeated.json()["grant"]["revoked"] is False
    assert sandbox_roots.granted_write_roots(str(primary)) == (additional.resolve(),)
    last_owner = TestClient(app).delete(
        f"/v1/workspaces/{second.id}/grants",
        params={"kind": "fs_root", "pattern": str(additional)},
    )
    assert last_owner.status_code == 200 and last_owner.json()["grant"]["revoked"] is True
    assert sandbox_roots.granted_write_roots(str(primary)) == ()
