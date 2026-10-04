"""Single-session reads must preserve the negotiated child-conversation contract."""

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def test_session_detail_projects_v3_and_keeps_legacy_and_workspace_guard(tmp_path: Path) -> None:
    """A reviewer read carries the same route and behavior as its v3 list row."""
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions", json={"title": "Reviewer", "mode": "plan", "approval_mode": "ask"}
        )
        assert created.status_code == 200
        sid = created.json()["id"]
        app.state.sessions.update(sid, model={"provider_id": "codex", "model_id": "luna"})
        headers = {"X-GACT-Version": "0.3"}
        detail = client.get(f"/v1/sessions/{sid}", headers=headers)
        listed = client.get("/v1/sessions", headers=headers).json()["sessions"]
        assert detail.status_code == 200
        assert detail.json() == next(row for row in listed if row["id"] == sid)
        assert detail.json()["provider_id"] == "codex"
        assert detail.json()["model_id"] == "luna"
        assert detail.json()["mode"] == "plan"
        assert client.get(f"/v1/sessions/{sid}").json()["model"]["model_id"] == "luna"
        assert (
            client.get(f"/v1/sessions/{sid}?workspace_id=other", headers=headers).status_code == 403
        )
        assert client.get("/v1/sessions/missing", headers=headers).status_code == 404
