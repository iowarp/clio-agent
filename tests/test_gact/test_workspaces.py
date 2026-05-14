"""CLIO-WS: workspaces CRUD + ws_default invariants."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


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


def test_create_session_validates_workspace(tmp_path: Path) -> None:
    c = _client(tmp_path)
    # Unknown workspace → 404.
    resp = c.post("/v1/sessions", json={"workspace_id": "ws_nope"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "internal_error"
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
