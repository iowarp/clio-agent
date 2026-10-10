"""Conversation to-dos, shared asynchronous controls and retained results coexist."""

import base64
import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import seed_agent_task
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


def test_cancelled_subagent_result_route_commits_provenance_without_blocking_loop(
    tmp_path: Path,
) -> None:
    """The real result owner must publish terminal provenance off the server loop."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "owner"}).json()["id"]
        task = seed_agent_task(
            app,
            parent_session_id=sid,
            agent_ref={"expert_id": "child", "requesting_expert_id": "main"},
            status="cancelled",
        )
        app.state.agent_task_registry.register(
            replace(task, notify_pending=True, result={"answer_excerpt": "retained child output"})
        )
        url = f"/v1/sessions/{sid}/async-tasks/{task.task_id}/result"
        first = client.get(url)
        assert first.status_code == 200
        assert first.json()["status"] == "cancelled"
        assert first.json()["result"]["output"] == "retained child output"
        claimed = app.state.agent_task_registry.get(task.task_id)
        assert claimed.consumed_at and not claimed.notify_pending
        second = client.get(url)
        assert second.status_code == 200 and second.json() == first.json()
        assert app.state.agent_task_registry.get(task.task_id).consumed_at == claimed.consumed_at


def test_task_query_rejects_malformed_cursor_without_collecting_results(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app, raise_server_exceptions=False) as client:
        sid = client.post("/v1/sessions", json={"title": "cursor owner"}).json()["id"]
        store = app_task_store(app)
        for index, handle in enumerate(["task_older", "task_newer"]):
            store.put(
                TaskRecord(
                    key=TaskKey("server", sid, handle),
                    handle=handle,
                    created_at=f"2026-10-09T00:00:0{index}Z",
                    status="completed",
                    result={"text": handle},
                    notify_pending=True,
                )
            )
        url = f"/v1/sessions/{sid}/async-tasks"
        first = client.get(url, params={"limit": 1})
        assert first.status_code == 200
        cursor = first.json()["cursor"]
        envelope = json.loads(base64.urlsafe_b64decode(cursor))
        envelope["after"] = [1, "task_older"]
        malformed = base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode()
        rejected = client.get(url, params={"limit": 1, "cursor": malformed})
        assert rejected.status_code == 422
        assert rejected.json()["error"]["message"] == "invalid task cursor"
        assert rejected.json()["error"]["recoverable"] is True
        resumed = client.get(url, params={"limit": 1, "cursor": cursor})
        assert resumed.status_code == 200
        assert [row["handle"] for row in resumed.json()["tasks"]] == ["task_older"]
        assert all(record.notify_pending and not record.consumed_at for record in store.list())
