"""Conversation to-dos, shared asynchronous controls and retained results coexist."""

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord


def test_shared_task_api_preserves_todos_and_results_after_dismissal(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "owner"}).json()["id"]
        other = client.post("/v1/sessions", json={"title": "unrelated"}).json()["id"]
        todo_response = client.post(f"/v1/sessions/{sid}/tasks", json={"title": "Review output"})
        assert todo_response.status_code == 200
        todo = todo_response.json()
        key = TaskKey("server", sid, "backend-task")
        app_task_store(app).put(
            TaskRecord(
                key=key,
                handle="task_owned",
                status="completed",
                description="Fetch the requested document",
                result={"content": [{"type": "text", "text": "retained document"}]},
                notify_pending=True,
            )
        )
        assert client.get(f"/v1/sessions/{sid}/tasks").json()["tasks"] == [todo]
        snapshot = client.get(f"/v1/sessions/{sid}/async-tasks")
        assert snapshot.status_code == 200
        assert snapshot.json()["total"] == 1
        assert snapshot.json()["tasks"][0]["handle"] == "task_owned"
        assert client.get(f"/v1/sessions/{other}/async-tasks").json()["total"] == 0
        result_url = f"/v1/sessions/{sid}/async-tasks/task_owned/result"
        assert client.get(result_url).json()["result"]["content"][0]["text"] == "retained document"
        assert client.get(f"/v1/sessions/{other}/async-tasks/task_owned/result").status_code == 404
        cancellation = client.post(
            f"/v1/sessions/{sid}/async-tasks/cancel", json={"tasks": ["task_owned", "missing"]}
        )
        assert cancellation.status_code == 200
        assert cancellation.json()["results"][0]["cancellation_requested"] is False
        assert cancellation.json()["errors"] == [
            {"handle": "missing", "error": "unknown_or_unauthorized_task"}
        ]
        assert client.post("/v1/runs/task_owned/dismiss").status_code == 200
        assert client.get(f"/v1/sessions/{sid}/async-processes").json()["processes"] == []
        assert client.get("/v1/runs").json()["runs"] == []
        assert client.get(result_url).json()["result"]["content"][0]["text"] == "retained document"
        assert client.get(f"/v1/sessions/{sid}/async-tasks").json()["total"] == 1
        assert client.get(f"/v1/sessions/{sid}/tasks").json()["tasks"] == [todo]
        record = app_task_store(app).get(key)
        assert record is not None and record.dismissed and record.consumed_at
        assert not record.notify_pending and record.status == "completed"
