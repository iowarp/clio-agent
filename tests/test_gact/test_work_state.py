"""Work presentation retains records without changing goal/loop execution."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.autonomous_loop import _put_loop
from clio_agent.gact.goal import arm_goal, clear_goal
from clio_agent.gact.routes.schedules import register_schedules_routes
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.work_state import retained_work_history, work_record_patch, work_snapshot


def test_work_records_keep_identity_and_private_fields_off_projection() -> None:
    metadata: dict[str, Any] = {}
    for index in range(30):
        metadata.update(
            work_record_patch(
                metadata,
                "goal",
                {
                    "goal_id": f"g{index}",
                    "condition": f"Goal {index}",
                    "active": True,
                    "private_capability": "DO NOT EXPOSE",
                },
            )
        )
    first = work_snapshot(metadata)
    second = work_snapshot(metadata, 25)
    assert len(first["goals"]) == 25
    assert len(second["goals"]) == 5
    assert first["goal_next_cursor"] == 25
    assert second["goal_next_cursor"] is None
    assert first["goals"][0]["state"] == "active"
    assert first["goals"][1]["state"] == "superseded"
    assert "DO NOT EXPOSE" not in str(first)
    assert metadata["goal"]["active"] is True


def test_durable_work_route_retains_stopped_goals_and_loops(tmp_path: Path) -> None:
    app = FastAPI()
    path = tmp_path / "sessions.json"
    app.state.sessions = SessionStore(path=path)
    sid = app.state.sessions.create(workspace_id="ws", title="Work").id
    first = arm_goal(app, sid, condition="First goal")
    assert clear_goal(app, sid)
    arm_goal(app, sid, condition="Second goal")
    loop = {
        "loop_id": "loop1",
        "prompt": "Inspect work",
        "active": False,
        "stopped": True,
        "stop_reason": "loop_user_stopped",
    }
    _put_loop(app, sid, loop)
    app.state.sessions = SessionStore(path=path)
    register_schedules_routes(app, cast(Any, None))
    with TestClient(app) as client:
        before = app.state.sessions.get(sid).metadata.copy()
        response = client.get(f"/v1/sessions/{sid}/work")
        assert response.status_code == 200
        body = response.json()
        assert body["goals"][1]["id"] == first["goal_id"]
        assert body["goals"][1]["state"] == "stopped"
        assert body["goal"]["title"] == "Second goal"
        assert body["loop"]["state"] == "stopped"
        assert body["loop"]["reason"] == "loop_user_stopped"
        assert app.state.sessions.get(sid).metadata == before
        assert client.get("/v1/sessions/missing/work").status_code == 404
        assert client.get(f"/v1/sessions/{sid}/work?cursor=-1").status_code == 422


def test_no_history_is_invented_for_legacy_sessions() -> None:
    snapshot = work_snapshot(
        {
            "goal": {"goal_id": "legacy", "condition": "Legacy", "active": False},
            "todos": [{"content": "Read evidence", "status": "in_progress"}],
        }
    )
    assert len(snapshot["goals"]) == 1
    assert snapshot["goal"]["state"] == "stopped"
    assert snapshot["todos"] == [{"content": "Read evidence", "status": "in_progress"}]
    assert snapshot["loop"] is None


def test_previous_todos_and_schedules_are_projected_from_retained_tool_parts() -> None:
    message = SimpleNamespace(
        created_at="2026-09-11T14:00:00Z",
        parts=[
            SimpleNamespace(
                type="tool_call",
                call_id="todo-old",
                tool_name="write_todos",
                input={"todos": [{"content": "First", "status": "pending"}]},
            ),
            SimpleNamespace(
                type="tool_result",
                call_id="todo-old",
                tool_name="write_todos",
                structured_content={"todos": [{"content": "First", "status": "pending"}]},
            ),
            SimpleNamespace(
                type="tool_call",
                call_id="todo-current",
                tool_name="write_todos",
                input={"todos": [{"content": "First", "status": "completed"}]},
            ),
            SimpleNamespace(
                type="tool_result",
                call_id="todo-current",
                tool_name="write_todos",
                structured_content={"todos": [{"content": "First", "status": "completed"}]},
            ),
            SimpleNamespace(
                type="tool_call",
                call_id="schedule-create",
                tool_name="cron_create",
                input={"prompt": "Inspect later", "recurring": False},
            ),
            SimpleNamespace(
                type="tool_result",
                call_id="schedule-create",
                tool_name="cron_create",
                structured_content={
                    "schedule_id": "sched-1",
                    "recurring": False,
                    "next_fire_at": "2026-09-11T14:10:00Z",
                    "timezone": "UTC",
                },
            ),
            SimpleNamespace(
                type="tool_call",
                call_id="schedule-delete",
                tool_name="cron_delete",
                input={"schedule_id": "sched-1"},
            ),
            SimpleNamespace(
                type="tool_result",
                call_id="schedule-delete",
                tool_name="cron_delete",
                structured_content={"schedule_id": "sched-1", "deleted": True},
            ),
        ],
    )
    history = retained_work_history(
        [message],
        current_todos=[{"content": "First", "status": "completed"}],
        current_schedules=[],
    )
    assert history["todo_history"] == [
        {
            "id": "todo-old",
            "created_at": "2026-09-11T14:00:00Z",
            "items": [{"content": "First", "status": "pending"}],
        }
    ]
    assert history["schedule_history"][0] == {
        "id": "sched-1",
        "question": "Inspect later",
        "state": "deleted",
        "created_at": "2026-09-11T14:00:00Z",
        "ended_at": "2026-09-11T14:00:00Z",
        "recurring": False,
        "next_fire_at": "2026-09-11T14:10:00Z",
        "timezone": "UTC",
    }
