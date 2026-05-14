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
    row = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "/tmp/a.py", "mode": "edit", "size": 120},
    ).json()
    assert row["path"] == "/tmp/a.py"
    assert row["mode"] == "edit"
    assert row["size"] == 120
    assert row["added_at"]

    body = client.get(f"/v1/sessions/{sid}/context/files").json()
    assert len(body["files"]) == 1

    # Upsert with a new mode keeps the row but flips mode.
    row2 = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "/tmp/a.py", "mode": "pin"},
    ).json()
    assert row2["mode"] == "pin"
    body = client.get(f"/v1/sessions/{sid}/context/files").json()
    assert len(body["files"]) == 1  # still 1 row — upserted

    # Detach.
    resp = client.request(
        "DELETE",
        f"/v1/sessions/{sid}/context/files",
        json={"path": "/tmp/a.py"},
    )
    assert resp.status_code == 204
    body = client.get(f"/v1/sessions/{sid}/context/files").json()
    assert body["files"] == []


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


def test_invalid_mode_defaults_to_read(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = _sid(client)
    row = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "/tmp/b.py", "mode": "nonsense"},
    ).json()
    assert row["mode"] == "read"
