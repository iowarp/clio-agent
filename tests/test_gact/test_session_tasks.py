"""iowarp/clio-agent#18: per-session task CRUD."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


@pytest.fixture()
def sid(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "t"}).json()["id"]


def test_empty_task_list(client: TestClient, sid: str) -> None:
    body = client.get(f"/v1/sessions/{sid}/tasks").json()
    assert body == {"tasks": []}


def test_create_then_list(client: TestClient, sid: str) -> None:
    new = client.post(
        f"/v1/sessions/{sid}/tasks", json={"title": "validate schema"}
    ).json()
    assert new["title"] == "validate schema"
    assert new["status"] == "pending"
    assert new["id"].startswith("task_")
    rows = client.get(f"/v1/sessions/{sid}/tasks").json()["tasks"]
    assert len(rows) == 1
    assert rows[0]["id"] == new["id"]


def test_patch_status(client: TestClient, sid: str) -> None:
    new = client.post(
        f"/v1/sessions/{sid}/tasks", json={"title": "x"}
    ).json()
    patched = client.patch(
        f"/v1/tasks/{new['id']}", json={"status": "completed"}
    ).json()
    assert patched["status"] == "completed"
    rows = client.get(f"/v1/sessions/{sid}/tasks").json()["tasks"]
    assert rows[0]["status"] == "completed"


def test_delete(client: TestClient, sid: str) -> None:
    new = client.post(
        f"/v1/sessions/{sid}/tasks", json={"title": "x"}
    ).json()
    assert client.delete(f"/v1/tasks/{new['id']}").status_code == 204
    rows = client.get(f"/v1/sessions/{sid}/tasks").json()["tasks"]
    assert rows == []


def test_create_missing_title_422(client: TestClient, sid: str) -> None:
    resp = client.post(f"/v1/sessions/{sid}/tasks", json={})
    assert resp.status_code == 422


def test_patch_unknown_404(client: TestClient) -> None:
    assert client.patch("/v1/tasks/task_nope", json={"status": "completed"}).status_code == 404


def test_capability_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["session_tasks"] is True
