"""Registered CSV preview route for data-backed A2UI charts."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def _registered_csv(client: TestClient, root: Path) -> str:
    workspace_id = client.post(
        "/v1/workspaces",
        json={"name": "chart data", "root_path": str(root)},
    ).json()["id"]
    session_id = client.post(
        "/v1/sessions",
        json={"workspace_id": workspace_id},
    ).json()["id"]
    source = root / "positions.csv"
    source.write_text(
        "time,east,north,up,ignored\n"
        + "".join(f"{index},{index / 10},{index / 20},{index / 30},x\n" for index in range(101)),
        encoding="utf-8",
    )
    response = client.post(
        f"/v1/sessions/{session_id}/artifacts/pin",
        json={"path": source.name},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["pinned"]["artifact_id"])


def test_csv_preview_samples_full_extent_and_selected_columns(tmp_path: Path) -> None:
    client = _client(tmp_path)
    artifact_id = _registered_csv(client, tmp_path)

    response = client.get(
        f"/v1/artifacts/{artifact_id}/table-preview",
        params={"columns": "time,east,north,up", "limit": 5},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_rows"] == 101
    assert body["sampled_rows"] == 5
    assert body["truncated"] is True
    assert body["rows"][0] == {"time": "0", "east": "0.0", "north": "0.0", "up": "0.0"}
    assert body["rows"][-1]["time"] == "100"
    assert all(set(row) == {"time", "east", "north", "up"} for row in body["rows"])


def test_csv_preview_reports_missing_columns(tmp_path: Path) -> None:
    client = _client(tmp_path)
    artifact_id = _registered_csv(client, tmp_path)

    response = client.get(
        f"/v1/artifacts/{artifact_id}/table-preview",
        params={"columns": "time,missing"},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["error"] == "columns_not_found"
    assert error["details"]["missing"] == ["missing"]
