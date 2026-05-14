"""iowarp/clio-agent#19: dynamic agent registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_capability_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["agent_write"] is True


def test_post_agent_then_list(client: TestClient) -> None:
    new = client.post("/v1/agents", json={
        "id": "code_reviewer",
        "title": "Code Reviewer",
        "description": "Reviews diffs for style + correctness",
        "tier": 2,
        "specialization": "code_editing",
        "keywords": ["review", "lint"],
        "tools": ["fs_read_file"],
    })
    assert new.status_code == 201
    body = new.json()
    assert body["id"] == "code_reviewer"
    assert body["source"] == "user"
    assert body["tier"] == 2

    # GET /v1/agents now includes it (and the built-ins).
    rows = client.get("/v1/agents").json()["agents"]
    ids = {a["id"] for a in rows}
    assert "code_reviewer" in ids
    assert "main" in ids  # built-in still listed


def test_put_agent_replaces_existing(client: TestClient) -> None:
    client.post("/v1/agents", json={
        "id": "code_reviewer",
        "title": "Code Reviewer",
    })
    resp = client.put("/v1/agents/code_reviewer", json={
        "id": "ignored-by-server",
        "title": "Strict Code Reviewer",
        "description": "now stricter",
        "tier": 2,
    })
    assert resp.status_code == 200
    body = resp.json()
    # URL id wins over body id (server enforces).
    assert body["id"] == "code_reviewer"
    assert body["title"] == "Strict Code Reviewer"


def test_post_agent_refuses_builtin_id(client: TestClient) -> None:
    resp = client.post("/v1/agents", json={
        "id": "data",
        "title": "Steal the built-in id",
    })
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["error"] == "permission_error"
    assert "built-in" in body["error"]["message"]


def test_put_agent_refuses_builtin(client: TestClient) -> None:
    resp = client.put("/v1/agents/data", json={"id": "data"})
    assert resp.status_code == 409


def test_delete_user_agent_works(client: TestClient) -> None:
    client.post("/v1/agents", json={
        "id": "to_drop",
        "title": "Drop me",
    })
    resp = client.delete("/v1/agents/to_drop")
    assert resp.status_code == 204
    rows = client.get("/v1/agents").json()["agents"]
    assert all(a["id"] != "to_drop" for a in rows)


def test_delete_unknown_agent_404s(client: TestClient) -> None:
    resp = client.delete("/v1/agents/never_existed")
    assert resp.status_code == 404


def test_delete_builtin_refused(client: TestClient) -> None:
    resp = client.delete("/v1/agents/data")
    assert resp.status_code == 409


def test_persistence_round_trip(tmp_path: Path) -> None:
    """First app instance creates an agent; a second instance at
    the same path sees it."""

    c1 = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    c1.post("/v1/agents", json={"id": "persisted", "title": "x"})
    c2 = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    rows = c2.get("/v1/agents").json()["agents"]
    assert any(a["id"] == "persisted" for a in rows)
