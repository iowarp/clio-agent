"""The orchestration plugin: run_flow returns a handle at acceptance; council; replay; cancel."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import dspy
from fastapi.testclient import TestClient

from tests.test_flows.conftest import (
    assistant_messages,
    default_program,
    new_session,
    post,
    wait_until,
)


def _orchestrator(client: TestClient, member_gate: threading.Event | None = None) -> dict[str, Any]:
    """Script the orchestrator's model to start one council through ``run_flow``."""

    from clio_agent.gact.flow_tools import start_flow

    started: dict[str, Any] = {}

    def program(question: str = "", **kwargs: Any) -> Any:
        if "Answer independently" in question and member_gate is not None:
            member_gate.wait(timeout=30)
        if question.startswith("convene the council") and not started:
            started.update(asyncio.run(start_flow("council", question, {"synthesizer": "self"})))
            return dspy.Prediction(answer=f"started {started['handle']}")
        return default_program(question=question, **kwargs)

    client.app.state.flow_program["run"] = program
    return started


def _snapshot(client: TestClient, handle: str) -> Any:
    return client.app.state.task_history.task_snapshot(handle)


def _run(client: TestClient, handle: str) -> dict[str, Any]:
    history = client.app.state.task_history
    checkpoint = history.workflow_checkpoint(handle)
    assert checkpoint is not None
    return history.read_content(checkpoint.content)


def _member_handles(client: TestClient, handle: str) -> list[str] | None:
    checkpoint = client.app.state.task_history.workflow_checkpoint(handle)
    if checkpoint is None:
        return None
    calls = client.app.state.task_history.read_content(checkpoint.content)["calls"]
    return [row["handle"] for row in calls.values()] if len(calls) == 3 else None


def _tree(client: TestClient, sid: str) -> list[dict[str, Any]]:
    response = client.get(f"/v1/sessions/{sid}/tasks/query", params={"limit": 200})
    assert response.status_code == 200, response.text
    return response.json()["tasks"]


def _council(client: TestClient) -> tuple[str, str, dict[str, Any]]:
    sid = new_session(client, "clio/orchestrator")
    started = _orchestrator(client)
    post(client, sid, "convene the council on the retention policy")
    wait_until(lambda: started.get("handle"))
    handle = started["handle"]
    wait_until(lambda: _snapshot(client, handle).status == "completed", timeout=90)
    return sid, handle, started


def test_run_flow_returns_the_handle_and_council_settles_under_it(flow_app: TestClient) -> None:
    sid, handle, started = _council(flow_app)
    assert started["accepted"] is True and started["flow"] == "council"
    assert started["kind"] == "Subagent"
    owner = flow_app.app.state.task_history.latest_assignment(sid)
    flow_task = _snapshot(flow_app, handle)
    assert flow_task.owner.parent_handle == owner.handle

    run = _run(flow_app, handle)
    assert run["flow"] == "council" and run["status"] == "completed" and run["depth"] == 1
    calls = list(run["calls"].values())
    assert len(calls) == 4 and {row["status"] for row in calls} == {"completed"}
    for row in calls:
        assert _snapshot(flow_app, row["handle"]).owner.parent_handle == handle
    assert run["result"]["synthesizer"] in {row["handle"] for row in calls}
    assert len(run["result"]["members"]) == 3

    # The flow's result reaches the starting session through its mailbox.
    wait_until(lambda: len(assistant_messages(flow_app, sid)) >= 2, timeout=60)
    # The flow's own session confirmed its input: one turn, nothing left to wake it.
    child_sid = flow_task.owner.session_id
    assert not flow_app.app.state.task_history.pending_mail(child_sid)
    assert len(assistant_messages(flow_app, child_sid)) == 1


def test_replaying_a_settled_flow_reattaches_to_its_handles(flow_app: TestClient) -> None:
    from clio_agent.flows.runner import run_agent_flow
    from clio_agent.gact import context

    sid, handle, _ = _council(flow_app)
    before = _run(flow_app, handle)
    tasks_before = sorted(row["handle"] for row in _tree(flow_app, sid))
    child_sid = _snapshot(flow_app, handle).owner.session_id
    module = flow_app.app.state.flow_modules[child_sid]

    def replay() -> Any:
        app_token = context.set_app(flow_app.app)
        session_token = context.set_session_id(child_sid)
        try:
            return run_agent_flow(module, {"question": "replay", "session_id": child_sid})
        finally:
            context.reset(session_token)
            context.reset(app_token)

    with ThreadPoolExecutor(1) as pool:
        prediction = pool.submit(replay).result(timeout=60)

    after = _run(flow_app, handle)
    assert prediction.answer == before["result"]["answer"]
    assert {k: v["handle"] for k, v in after["calls"].items()} == {
        k: v["handle"] for k, v in before["calls"].items()
    }
    assert sorted(row["handle"] for row in _tree(flow_app, sid)) == tasks_before


def test_cancelling_a_started_flow_settles_its_subtree(flow_app: TestClient) -> None:
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _orchestrator(flow_app, gate)
    try:
        post(flow_app, sid, "convene the council on the incident")
        wait_until(lambda: started.get("handle"))
        handle = started["handle"]
        members = wait_until(lambda: _member_handles(flow_app, handle), timeout=60)
        response = flow_app.post(f"/v1/sessions/{sid}/tasks/cancel", json={"tasks": handle})
        assert response.status_code == 200, response.text
        assert response.json()["errors"] == []
    finally:
        gate.set()
    terminal = {"cancelled", "completed", "failed"}
    wait_until(lambda: _snapshot(flow_app, handle).status == "cancelled", timeout=60)
    for member in members:
        wait_until(lambda m=member: _snapshot(flow_app, m).status in terminal, timeout=60)
        assert _snapshot(flow_app, member).status == "cancelled"
