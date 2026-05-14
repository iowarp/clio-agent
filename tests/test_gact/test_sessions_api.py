"""CLIO-BBBBBBBBBB8: integration tests for /v1/sessions CRUD.

Uses FastAPI's TestClient against build_app() with a per-test
sessions_path so tests don't share state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def test_post_v1_sessions_returns_created_session(client: TestClient) -> None:
    resp = client.post(
        "/v1/sessions",
        json={"workspace_id": "ws_default", "title": "my session"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"].startswith("sess_")
    assert body["workspace_id"] == "ws_default"
    assert body["title"] == "my session"
    assert body["status"] == "idle"
    assert body["message_count"] == 0


def test_post_v1_sessions_defaults_workspace_and_title(
    client: TestClient,
) -> None:
    """Empty body is allowed — CLIO has an implicit default workspace
    and we synthesise a title from the session id."""

    resp = client.post("/v1/sessions", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == "ws_default"
    # Synthesised titles include the trailing id chars.
    assert body["id"][-6:] in body["title"]


def test_get_v1_sessions_lists_newest_first(client: TestClient) -> None:
    first = client.post("/v1/sessions", json={"title": "first"}).json()
    second = client.post("/v1/sessions", json={"title": "second"}).json()

    resp = client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    ids = [s["id"] for s in body["sessions"]]
    assert ids == [second["id"], first["id"]], (
        f"expected newest-first; got {ids}"
    )


def test_get_v1_sessions_filter_by_workspace(client: TestClient) -> None:
    # Create two ad-hoc workspaces (ws_default already exists so
    # POSTing to it works without a roundtrip).
    ws_a = client.post("/v1/workspaces", json={"name": "alpha"}).json()["id"]
    ws_b = client.post("/v1/workspaces", json={"name": "beta"}).json()["id"]

    client.post("/v1/sessions", json={"workspace_id": ws_a, "title": "a1"})
    client.post("/v1/sessions", json={"workspace_id": ws_b, "title": "b1"})
    client.post("/v1/sessions", json={"workspace_id": ws_a, "title": "a2"})

    resp = client.get(f"/v1/sessions?workspace_id={ws_a}")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["sessions"]]
    assert len(ids) == 2
    assert all(
        s["workspace_id"] == ws_a for s in resp.json()["sessions"]
    )


def test_get_v1_sessions_sid_returns_single(client: TestClient) -> None:
    created = client.post("/v1/sessions", json={"title": "x"}).json()
    resp = client.get(f"/v1/sessions/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert resp.json()["title"] == "x"


def test_get_v1_sessions_sid_not_found_returns_structured_404(
    client: TestClient,
) -> None:
    resp = client.get("/v1/sessions/sess_does_not_exist")
    assert resp.status_code == 404
    body = resp.json()
    # v0.2 envelope shape — the typed taxonomy (§14).
    assert "error" in body
    inner = body["error"]
    assert isinstance(inner, dict)
    assert "message" in inner
    # Machine-readable discriminator present (either v0.1 `code` or
    # v0.2 `error` — our impl uses the latter).
    assert "error" in inner or "code" in inner


def test_delete_v1_sessions_removes_row(client: TestClient) -> None:
    created = client.post("/v1/sessions", json={"title": "gone"}).json()
    resp = client.delete(f"/v1/sessions/{created['id']}")
    assert resp.status_code == 204

    # Gone from the list.
    resp2 = client.get(f"/v1/sessions/{created['id']}")
    assert resp2.status_code == 404


def test_delete_v1_sessions_missing_is_404(client: TestClient) -> None:
    resp = client.delete("/v1/sessions/sess_does_not_exist")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body


def test_sessions_persisted_across_app_instances(tmp_path: Path) -> None:
    """Two TestClients pointing at the same sessions.json see the
    same rows — which is how the store survives
    ``clio-agent-gact`` restarts."""

    path = tmp_path / "sessions.json"

    with TestClient(build_app(sessions_path=path)) as a:
        created = a.post("/v1/sessions", json={"title": "keep"}).json()

    with TestClient(build_app(sessions_path=path)) as b:
        resp = b.get(f"/v1/sessions/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "keep"
