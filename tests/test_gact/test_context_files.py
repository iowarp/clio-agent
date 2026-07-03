"""CLIO-BBBBBBBBBB22: session context files."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _sid(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "t"}).json()["id"]


def test_empty_context_files_list(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    resp = client.get(f"/v1/sessions/{sid}/context/files")
    assert resp.status_code == 200
    assert resp.json() == {"files": []}


def test_add_then_list_then_remove(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    target = tmp_path / "a.py"
    target.write_text("print('hello')\n")
    row = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(target), "mode": "edit", "size": 120},
    ).json()
    assert row["path"] == str(target)
    assert row["mode"] == "edit"
    assert row["size"] == 120
    assert row["added_at"]

    body = client.get(f"/v1/sessions/{sid}/context/files").json()
    assert len(body["files"]) == 1

    # Upsert with a new mode keeps the row but flips mode.
    row2 = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(target), "mode": "pin"},
    ).json()
    assert row2["mode"] == "pin"
    body = client.get(f"/v1/sessions/{sid}/context/files").json()
    assert len(body["files"]) == 1  # still 1 row — upserted

    # Detach.
    resp = client.request(
        "DELETE",
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(target)},
    )
    assert resp.status_code == 204
    body = client.get(f"/v1/sessions/{sid}/context/files").json()
    assert body["files"] == []


def test_add_workspace_relative_context_file_records_provenance(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    sid = _sid(client)
    target = tmp_path / "src" / "notes.md"
    target.parent.mkdir()
    target.write_text("relative context\n")

    row = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "@src/notes.md", "mode": "read"},
    ).json()

    assert row["path"] == "src/notes.md"
    assert row["display_path"] == "src/notes.md"
    assert row["resolved_path"] == str(target.resolve())
    assert row["workspace_id"] == "ws_default"
    assert row["source"] == "mention"


def test_add_context_file_can_use_explicit_workspace_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    default_root = tmp_path / "default"
    project_root = tmp_path / "project"
    default_root.mkdir()
    project_root.mkdir()
    client.app.state.workspaces.update("ws_default", root_path=str(default_root))
    workspace_id = client.post(
        "/v1/workspaces",
        json={"name": "project", "root_path": str(project_root)},
    ).json()["id"]
    sid = _sid(client)
    target = project_root / "src" / "notes.md"
    target.parent.mkdir()
    target.write_text("project context\n")

    row = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "@src/notes.md", "workspace_id": workspace_id, "mode": "read"},
    ).json()

    assert row["path"] == "src/notes.md"
    assert row["display_path"] == "src/notes.md"
    assert row["resolved_path"] == str(target.resolve())
    assert row["workspace_id"] == workspace_id
    assert row["source"] == "mention"


def test_workspace_relative_context_file_rejects_traversal(tmp_path: Path) -> None:
    client = _client(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client.app.state.workspaces.update("ws_default", root_path=str(workspace))
    sid = _sid(client)

    resp = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "../secret.txt", "mode": "edit"},
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["error"] == "path_outside_workspace"


def test_remove_context_file_accepts_display_and_mention_paths(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    sid = _sid(client)
    target = tmp_path / "notes.md"
    target.write_text("context\n")
    client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "notes.md", "mode": "read"},
    )

    resp = client.request(
        "DELETE",
        f"/v1/sessions/{sid}/context/files",
        json={"path": "@notes.md"},
    )

    assert resp.status_code == 204
    assert client.get(f"/v1/sessions/{sid}/context/files").json() == {"files": []}


def test_context_files_persist_across_app_rebuild(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    target = tmp_path / "persisted.md"
    target.write_text("persistent context\n")
    client.post(
        f"/v1/sessions/{sid}/context/files",
        json={
            "path": str(target),
            "mode": "read",
            "language": "markdown",
            "size": target.stat().st_size,
        },
    )

    rebuilt = _client(tmp_path)
    body = rebuilt.get(f"/v1/sessions/{sid}/context/files").json()
    file_row = body["files"][0]

    assert file_row["path"] == str(target)
    assert file_row["mode"] == "read"
    assert file_row["added_at"]
    assert file_row["last_modified"] == ""
    assert file_row["size"] == target.stat().st_size
    assert file_row["language"] == "markdown"


def test_context_files_removed_from_persistence_on_session_delete(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    target = tmp_path / "attached.txt"
    target.write_text("attached\n")
    client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(target), "mode": "read"},
    )

    assert client.delete(f"/v1/sessions/{sid}").status_code == 204
    rebuilt = _client(tmp_path)

    assert sid not in rebuilt.app.state.context_files


def test_add_missing_path_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    resp = client.post(f"/v1/sessions/{sid}/context/files", json={})
    assert resp.status_code == 422
    assert "path" in resp.json()["error"]["message"]


def test_unknown_session_404s(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/v1/sessions/sess_nope/context/files")
    assert resp.status_code == 404


def test_invalid_mode_is_structured_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    resp = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(tmp_path / "b.py"), "mode": "nonsense"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["error"] == "bad_request"
    assert "mode" in body["error"]["message"]


def test_read_context_file_must_exist(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    missing = tmp_path / "missing.txt"
    resp = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(missing), "mode": "read"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "context_file_error"
    assert str(missing) in body["error"]["message"]
    assert body["error"]["details"]["operation"] == "exists"
    assert body["error"]["details"]["recovery_actions"] == [
        "choose_existing_file",
        "remove_context_file",
        "retry",
        "exit",
    ]


def test_pin_context_file_must_be_a_file(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    resp = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(tmp_path), "mode": "pin"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["error"] == "context_file_error"
    assert "not a file" in body["error"]["message"]
    assert body["error"]["details"]["operation"] == "is_file"
