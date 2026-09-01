"""CLIO-WS: workspaces CRUD + ws_default invariants."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from clio_agent.gact.app import build_app
from clio_agent.gact.routes import workspaces as workspace_routes


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_default_workspace_exists_on_fresh_install(tmp_path: Path) -> None:
    c = _client(tmp_path)
    body = c.get("/v1/workspaces").json()
    assert len(body["workspaces"]) == 1
    assert body["workspaces"][0]["id"] == "ws_default"
    assert body["workspaces"][0]["name"] == "default"
    # root_path is auto-pinned to whatever the server's CWD was at boot
    # — non-empty.
    assert body["workspaces"][0]["root_path"]


def test_create_workspace_persists(tmp_path: Path) -> None:
    c = _client(tmp_path)
    new = c.post(
        "/v1/workspaces",
        json={"name": "iowarp", "root_path": "/tmp/iowarp"},
    ).json()
    assert new["name"] == "iowarp"
    assert new["root_path"] == "/tmp/iowarp"
    assert new["id"].startswith("ws_")
    assert new["created_at"]

    # GET single + list both reflect the new row.
    fetched = c.get(f"/v1/workspaces/{new['id']}").json()
    assert fetched["id"] == new["id"]
    body = c.get("/v1/workspaces").json()
    assert {w["name"] for w in body["workspaces"]} >= {"default", "iowarp"}


def test_create_workspace_materializes_missing_root(tmp_path: Path) -> None:
    """A newly registered workspace is immediately usable as a tool cwd."""
    c = _client(tmp_path)
    root = tmp_path / "new-workspace"

    response = c.post(
        "/v1/workspaces",
        json={"name": "new workspace", "root_path": str(root)},
    )

    assert response.status_code == 201
    assert root.is_dir()


def test_workspace_exposes_default_and_configured_storage_root(tmp_path: Path) -> None:
    c = _client(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    custom = tmp_path / "custom-clio-store"

    default_ws = c.post(
        "/v1/workspaces",
        json={"name": "default-storage", "root_path": str(project)},
    ).json()
    custom_ws = c.post(
        "/v1/workspaces",
        json={
            "name": "custom-storage",
            "root_path": str(project),
            "storage_root": str(custom),
        },
    ).json()

    assert default_ws["storage_root"] == str(project / ".clio")
    assert custom_ws["storage_root"] == str(custom)


# The per-workspace session/message mirror was DELETED in #771 (reader-less,
# write-only). The inverse guarantee — a workspace-owned session writes NOTHING
# under its storage root — is covered by
# tests/test_gact/test_workspace_mirror_removed.py.


def test_create_session_validates_workspace(tmp_path: Path) -> None:
    c = _client(tmp_path)
    # Unknown workspace → 404.
    resp = c.post("/v1/sessions", json={"workspace_id": "ws_nope"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "not_found"
    assert "ws_nope" in body["error"]["message"]

    # ws_default exists at boot, so a no-arg POST works.
    resp = c.post("/v1/sessions", json={})
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws_default"


def test_delete_workspace_refuses_default(tmp_path: Path) -> None:
    c = _client(tmp_path)
    resp = c.delete("/v1/workspaces/ws_default")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["error"] == "permission_error"
    # Default still listed.
    body = c.get("/v1/workspaces").json()
    assert any(w["id"] == "ws_default" for w in body["workspaces"])


def test_delete_workspace_unknown_404s(tmp_path: Path) -> None:
    c = _client(tmp_path)
    assert c.delete("/v1/workspaces/ws_nope").status_code == 404


def test_delete_user_workspace_works(tmp_path: Path) -> None:
    c = _client(tmp_path)
    new = c.post("/v1/workspaces", json={"name": "scratch"}).json()
    assert c.delete(f"/v1/workspaces/{new['id']}").status_code == 204
    body = c.get("/v1/workspaces").json()
    assert all(w["id"] != new["id"] for w in body["workspaces"])


def test_persistence_round_trip(tmp_path: Path) -> None:
    """First app instance creates a workspace; a second instance
    pointed at the same file sees it."""

    c1 = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    c1.post("/v1/workspaces", json={"name": "persistent"})
    # Drop c1, build a fresh app at the same path.
    c2 = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    body = c2.get("/v1/workspaces").json()
    assert any(w["name"] == "persistent" for w in body["workspaces"])


def test_workspace_file_read_returns_plain_text_not_json(tmp_path: Path) -> None:
    c = _client(tmp_path)
    c.app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    target = tmp_path / "notes.md"
    target.write_bytes(b"hello picker\n")

    resp = c.get("/v1/workspaces/ws_default/files/read", params={"path": "notes.md"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "hello picker\n"
    assert resp.content == b"hello picker\n"


def test_workspace_file_listing_marks_service_storage_without_spending_visible_cap(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    c = _client(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    c.app.state.workspaces.update("ws_default", root_path=str(project))
    for internal_root in (project / ".clio", project / ".clio-child-cache"):
        internal_root.mkdir()
        for index in range(8):
            (internal_root / f"internal-{index}.json").write_text("{}", encoding="utf-8")
    (project / "report.md").write_text("visible", encoding="utf-8")
    monkeypatch.setattr(workspace_routes, "_FILE_PICKER_LIMIT", 3)

    response = c.get("/v1/workspaces/ws_default/files")

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [
        (entry["path"], entry["type"], entry["internal"], entry.get("size")) for entry in entries
    ] == [
        (".clio", "dir", True, None),
        (".clio-child-cache", "dir", True, None),
        ("report.md", "file", False, 7),
    ]

    repo_map = c.get("/v1/workspaces/ws_default/repo_map")

    assert repo_map.status_code == 200
    assert [child["path"] for child in repo_map.json()["tree"]["children"]] == ["report.md"]


def test_workspace_file_read_serves_png_as_raw_bytes(tmp_path: Path) -> None:
    # iowarp/clio-agent#673, #676: binary files (PNG) must be served as RAW
    # bytes with their real content type, not UTF-8-decoded into text/plain
    # (which corrupts the bytes with replacement characters).
    c = _client(tmp_path)
    c.app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    # PNG signature + bytes that are invalid UTF-8 (0xFF 0xFE) — must survive.
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x01\x02"
    target = tmp_path / "validation_plot.png"
    target.write_bytes(png_bytes)

    resp = c.get("/v1/workspaces/ws_default/files/read", params={"path": "validation_plot.png"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == png_bytes
    assert int(resp.headers["content-length"]) == len(png_bytes)


def test_workspace_file_read_sniffs_unknown_binary_as_octet_stream(tmp_path: Path) -> None:
    # Unknown extension + binary content (NUL byte) -> raw octet-stream, not text.
    c = _client(tmp_path)
    c.app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    blob = b"\x00\x01\x02\xff\xfedata"
    target = tmp_path / "artifact.unknownext"
    target.write_bytes(blob)

    resp = c.get("/v1/workspaces/ws_default/files/read", params={"path": "artifact.unknownext"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.content == blob


def test_create_workspace_rejects_unmakeable_root(tmp_path: Path) -> None:
    """An unmakeable root fails with a typed 400 and creates no workspace."""
    c = _client(tmp_path)
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")
    before = {w["id"] for w in c.get("/v1/workspaces").json()["workspaces"]}

    response = c.post(
        "/v1/workspaces",
        json={"name": "blocked", "root_path": str(blocker / "sub")},
    )

    assert response.status_code == 400
    envelope = response.json()["error"]
    assert envelope["error"] == "invalid_request"
    assert envelope["details"]["root_path"] == str(blocker / "sub")
    assert envelope["details"]["reason"]
    after = {w["id"] for w in c.get("/v1/workspaces").json()["workspaces"]}
    assert after == before


def test_patch_workspace_materializes_new_root(tmp_path: Path) -> None:
    """Repointing a workspace root makes it usable as a tool cwd, like create does."""
    c = _client(tmp_path)
    wid = c.post(
        "/v1/workspaces",
        json={"name": "movable", "root_path": str(tmp_path / "first")},
    ).json()["id"]
    moved = tmp_path / "second" / "nested"

    response = c.patch(f"/v1/workspaces/{wid}", json={"root_path": str(moved)})

    assert response.status_code == 200
    assert response.json()["root_path"] == str(moved)
    assert moved.is_dir()


def test_patch_workspace_rejects_unmakeable_root(tmp_path: Path) -> None:
    """An unmakeable PATCH root is refused with a typed 400 and does not repoint."""
    c = _client(tmp_path)
    original = tmp_path / "original"
    wid = c.post(
        "/v1/workspaces",
        json={"name": "pinned", "root_path": str(original)},
    ).json()["id"]
    blocker = tmp_path / "bfile"
    blocker.write_text("not a directory", encoding="utf-8")

    response = c.patch(f"/v1/workspaces/{wid}", json={"root_path": str(blocker / "sub")})

    assert response.status_code == 400
    envelope = response.json()["error"]
    assert envelope["error"] == "invalid_request"
    assert envelope["details"]["root_path"] == str(blocker / "sub")
    assert c.get(f"/v1/workspaces/{wid}").json()["root_path"] == str(original)


def test_persisted_workspace_rename_still_moves_derived_display_name(tmp_path: Path) -> None:
    """A reload must not freeze a DERIVED label into the configured display name."""
    from clio_agent.gact.workspaces import WorkspaceStore

    store_path = tmp_path / "workspaces.json"
    project = tmp_path / "proj"
    first = WorkspaceStore(path=store_path)
    ws = first.create(name=str(project), root_path=str(project))
    assert ws.to_wire()["display_name"] == "proj"

    reloaded = WorkspaceStore(path=store_path)
    renamed = reloaded.update(ws.id, name="My Project")

    assert renamed is not None
    assert renamed.to_wire()["display_name"] == "My Project"


def test_persisted_workspace_keeps_configured_display_name(tmp_path: Path) -> None:
    """A deliberately configured label survives the reload and outranks a rename."""
    from clio_agent.gact.workspaces import WorkspaceStore

    store_path = tmp_path / "workspaces.json"
    first = WorkspaceStore(path=store_path)
    ws = first.create(name="raw", root_path=str(tmp_path / "raw"))
    first.update(ws.id, display_name="Chosen Label")

    reloaded = WorkspaceStore(path=store_path)
    renamed = reloaded.update(ws.id, name="Renamed")

    assert renamed is not None
    assert renamed.display_name == "Chosen Label"
    assert renamed.to_wire()["display_name"] == "Chosen Label"
