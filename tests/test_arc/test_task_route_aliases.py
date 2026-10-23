"""Shipped REST aliases share current Core snapshots and authorization."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import TaskOwner, TaskSnapshot
from clio_agent.gact.native_work import now
from clio_agent.gact.routes.tasks import register_task_routes
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_conversation_owner_contracts import create_environment


async def no_cancel(_task: TaskSnapshot, _caller: TaskCaller) -> dict[str, Any]:
    pytest.fail("A read alias must not dispatch cancellation")


@pytest.mark.asyncio
async def test_rest_result_cursor_reconstructs_exact_unicode_output(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "64")
    body = {"output": "é 中文 🚀 " * 80, "count": 80}

    def prepare() -> TaskSnapshot:
        result = owner.history.write_content(root.owner.session_id, body)
        task = TaskSnapshot(
            handle="task_paged",
            kind="Shell",
            owner=root.owner,
            tool="shell_bash",
            label="Large output",
            invocation_id="paged",
            created_at=now(),
            updated_at=now(),
            status="completed",
            raw_status="completed",
            terminal_at=now(),
            settlement="confirmed",
            result=result,
        )
        owner.history.record_snapshot(task, observation_id="paged-result")
        return task

    task = await asyncio.to_thread(prepare)
    service = FastAPI()
    service.state.task_history, service.state.sessions = owner.history, app.state.sessions
    service.state.human_principal = "human"
    service.state.task_operations = TaskOperations(owner.history, no_cancel)
    register_task_routes(service)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url="http://task-service"
        ) as client:
            path = f"/v1/sessions/{root.owner.session_id}/tasks/{task.handle}/result"
            cursor, chunks, offsets = None, [], []
            for _ in range(100):
                response = await client.get(path, params={"cursor": cursor} if cursor else {})
                assert response.status_code == 200
                page = response.json()
                assert page["offset"] not in offsets
                offsets.append(page["offset"])
                chunks.append(page["result"]["preview"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                assert len(cursor) <= 64, "a model has to copy this cursor verbatim (#1844)"
            else:
                pytest.fail("Result pages did not advance to completion")
            assert len(offsets) > 1 and offsets == sorted(offsets)
            assert json.loads("".join(chunks)) == body
            assert owner.history.acknowledged_notifications(root.owner.session_id) == frozenset()
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("activity", "expected"),
    [
        ("busy", "running"),
        ("executing", "running"),
        ("idle", "yielded"),
        ("yielded", "yielded"),
        ("stopping", "stopping"),
        ("cancelling", "cancellation_requested"),
        ("waiting_for_input", "waiting_user"),
    ],
)
async def test_agent_aliases_and_processes_expose_one_authorized_display_state(
    arc: ARCMemory, tmp_path: Path, activity: str, expected: str
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)

    def prepare() -> tuple[TaskSnapshot, TaskSnapshot]:
        session = app.state.sessions.create(
            workspace_id="workspace", parent_session_id=root.owner.session_id
        )
        child = root.model_copy(
            update={
                "handle": "task_child",
                "kind": "Subagent",
                "owner": TaskOwner(
                    human_id="human",
                    session_id=session.id,
                    assignment_id="task_child",
                    parent_handle=root.handle,
                ),
                "invocation_id": "spawn-child",
                "activity": activity,
            }
        )
        owner.history.record_snapshot(child, observation_id="child-state")
        foreign = child.model_copy(
            update={
                "handle": "task_foreign",
                "owner": child.owner.model_copy(
                    update={"human_id": "other-human", "assignment_id": "task_foreign"}
                ),
                "status": "completed",
                "terminal_at": now(),
                "settlement": "confirmed",
            }
        )
        owner.history.record_snapshot(foreign, observation_id="foreign-state")
        return child, foreign

    child, foreign = await asyncio.to_thread(prepare)
    service = FastAPI()
    service.state.task_history = owner.history
    service.state.sessions = app.state.sessions
    service.state.human_principal = "human"
    service.state.task_operations = TaskOperations(owner.history, no_cancel)
    register_task_routes(service)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url="http://task-service"
        ) as client:
            detail = await client.get("/v1/agent-tasks/" + child.handle)
            listed = await client.get(f"/v1/sessions/{root.owner.session_id}/agent-tasks")
            processes = await client.get(f"/v1/sessions/{root.owner.session_id}/async-processes")
            assert detail.status_code == listed.status_code == processes.status_code == 200
            assert detail.json()["task_id"] == child.handle
            assert detail.json()["parent_session_id"] == root.owner.session_id
            assert listed.json()["tasks"] == [detail.json()]
            process = next(
                row for row in processes.json()["processes"] if row["handle_id"] == child.handle
            )
            assert detail.json()["live_state"] == process["live_state"] == expected
            denied = await client.get("/v1/agent-tasks/" + foreign.handle)
            missing = await client.get("/v1/agent-tasks/task_missing")
            assert denied.status_code == missing.status_code == 404
            assert denied.json() == missing.json()
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
