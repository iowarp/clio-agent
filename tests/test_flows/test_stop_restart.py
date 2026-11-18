"""Stop reaches what a conversation started; restart keeps finished work.

``docs/design/stop-and-restart.md``. These drive the API with scripted models: they
verify the mechanism, not the agent's judgement.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from tests.test_flows.conftest import (
    assistant_messages,
    default_program,
    new_session,
    post,
    wait_until,
)

TERMINAL = {"cancelled", "completed", "failed", "task_state_uncertain"}


def _snapshot(client: TestClient, handle: str) -> Any:
    return client.app.state.task_history.task_snapshot(handle)


def _calls(client: TestClient, handle: str) -> dict[str, Any]:
    checkpoint = client.app.state.task_history.workflow_checkpoint(handle)
    if checkpoint is None:
        return {}
    return client.app.state.task_history.read_content(checkpoint.content)


def _council_starter(
    client: TestClient, gate: threading.Event, *, model_call: str = ""
) -> dict[str, Any]:
    """The orchestrator starts one council; its members wait on ``gate``."""

    from clio_agent.gact.artifacts.observer_bridge import MODEL_CALL_SLOT
    from clio_agent.gact.flow_tools import start_flow

    started: dict[str, Any] = {}

    def program(question: str = "", **kwargs: Any) -> Any:
        if "Answer independently" in question:
            gate.wait(timeout=30)
        if question.startswith("convene") and not started:
            token = MODEL_CALL_SLOT.set({"model_call_id": model_call} if model_call else {})
            try:
                started.update(
                    asyncio.run(start_flow("council", question, {"synthesizer": "self"}))
                )
            finally:
                MODEL_CALL_SLOT.reset(token)
            return dspy.Prediction(answer=f"started {started['handle']}")
        return default_program(question=question, **kwargs)

    client.app.state.flow_program["run"] = program
    return started


def _members(client: TestClient, handle: str) -> list[str] | None:
    calls = _calls(client, handle).get("calls") or {}
    handles = [row.get("handle") for row in calls.values()]
    return handles if len(handles) == 3 and all(handles) else None


def test_stop_scope_lists_the_running_flow_by_its_name(flow_app: TestClient) -> None:
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _council_starter(flow_app, gate)
    try:
        post(flow_app, sid, "convene the council on naming")
        handle = wait_until(lambda: started.get("handle"))
        wait_until(lambda: _members(flow_app, handle))
        scope = flow_app.get(f"/v1/sessions/{sid}/stop-scope").json()["open_children"]
        row = next(row for row in scope if row["handle"] == handle)
        assert row["label"] == "council flow" and row["kind"] == "Subagent"
        # Members are the flow's own children: cancelling the flow covers them.
        assert {r["handle"] for r in scope}.isdisjoint(_members(flow_app, handle) or [])
        found = flow_app.get(
            f"/v1/sessions/{sid}/tasks/query", params={"query": "council", "limit": 50}
        ).json()["tasks"]
        assert handle in {task["handle"] for task in found}
    finally:
        gate.set()
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=90)


def test_stop_with_children_settles_a_flow_mid_gather(
    flow_app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = flow_app.app.state.task_history
    recorded: list[dict[str, Any]] = []
    original = history.record_intervention

    def spy(session_id: str, identity: str, payload: dict[str, Any]) -> None:
        recorded.append(payload)
        original(session_id, identity, payload)

    monkeypatch.setattr(history, "record_intervention", spy)
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _council_starter(flow_app, gate)
    try:
        post(flow_app, sid, "convene the council on the incident")
        handle = wait_until(lambda: started.get("handle"))
        members = wait_until(lambda: _members(flow_app, handle), timeout=60)
        response = flow_app.post(f"/v1/sessions/{sid}/cancel", json={"children": "stop"})
        assert response.status_code == 204, response.text
    finally:
        gate.set()
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, handle).status == "cancelled"
    for member in members:
        wait_until(lambda m=member: _snapshot(flow_app, m).terminal_at is not None, timeout=60)
        assert _snapshot(flow_app, member).status == "cancelled"
    # Nothing the conversation started is left open, and the run says how it ended.
    wait_until(lambda: not flow_app.get(f"/v1/sessions/{sid}/stop-scope").json()["open_children"])
    run = _calls(flow_app, handle)
    assert run["status"] == "cancelled"
    # The run's reason is the person's stop, not the member whose call it cancelled.
    result = run["result"]
    assert result["reason"] == "stopped"
    assert result["stopped_by"] == "person"
    assert result["stop_reason"] == "conversation_stop"
    assert result["message"] == "Stopped from the conversation's Stop."
    assert result["ended_by"]["status"] in {"cancelled", "gather_failed"}
    # The run view draws its end as cancelled, not as a failed step.
    from clio_agent.flows.run_view import flow_run

    view = flow_run(history, handle)
    assert view is not None and view["outcome"]["reason"] == "stopped"
    # The stop is recorded with its reason.
    wait_until(
        lambda: any(
            row.get("action") == "cancel"
            and row.get("phase") == "effect"
            and row.get("reason") == "conversation_stop"
            for row in recorded
        )
    )


def _orphaned_call(client: TestClient, member: str) -> str:
    """A search a restart cut off under ``member``: uncertain, never resubmitted."""
    from clio_agent.arc.task_types import TaskOwner, TaskProgress, TaskSnapshot
    from clio_agent.gact.native_work import now

    history = client.app.state.task_history
    owner = _snapshot(client, member)
    handle, stamp = "task_orphaned_search", now()
    history.record_snapshot(
        TaskSnapshot(
            handle=handle,
            kind="MCP",
            owner=TaskOwner(
                human_id=owner.owner.human_id,
                session_id=owner.owner.session_id,
                assignment_id=member,
                parent_handle=member,
            ),
            tool="search",
            label="search",
            description="search: cut off by a restart",
            description_missing=False,
            invocation_id="call_orphaned",
            created_at=stamp,
            updated_at=stamp,
            raw_status="accepted",
            status="task_state_uncertain",
            settlement="unknown",
            progress=TaskProgress(message="Acceptance identity is unknown."),
            attention_episode="orphaned",
        ),
        observation_id="fixture:orphaned",
    )
    return handle


def test_stop_after_a_restart_settles_despite_a_cut_off_call(
    flow_app: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash left a member's search uncertain; Stop everything still settles the tree.

    SABOTAGE: drop ``orphaned`` from ``conversation_controls._cancel_subtree`` and the
    member, the flow and the conversation stay ``task_state_uncertain`` for ever.
    """
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _council_starter(flow_app, gate)
    try:
        post(flow_app, sid, "convene the council on the incident")
        handle = wait_until(lambda: started.get("handle"))
        members = wait_until(lambda: _members(flow_app, handle), timeout=60)
        orphan = _orphaned_call(flow_app, members[0])
        response = flow_app.post(f"/v1/sessions/{sid}/cancel", json={"children": "stop"})
        assert response.status_code == 204, response.text
    finally:
        gate.set()
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, handle).status == "cancelled"
    for member in members:
        wait_until(lambda m=member: _snapshot(flow_app, m).terminal_at is not None, timeout=60)
        assert _snapshot(flow_app, member).status == "cancelled"
    # The cut-off call keeps its own truthful status; nothing claims it was cancelled.
    assert _snapshot(flow_app, orphan).status == "task_state_uncertain"
    # The stop is an outcome: no turn worker is reported as failed.
    failed = [r.getMessage() for r in caplog.records if "session_worker_failed" in r.getMessage()]
    assert not failed, failed


def test_stop_keeping_children_leaves_the_flow_running(flow_app: TestClient) -> None:
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _council_starter(flow_app, gate)
    try:
        post(flow_app, sid, "convene the council on retention")
        handle = wait_until(lambda: started.get("handle"))
        wait_until(lambda: _members(flow_app, handle), timeout=60)
        response = flow_app.post(f"/v1/sessions/{sid}/cancel")
        assert response.status_code == 204
        assert _snapshot(flow_app, handle).cancellation_requested_at is None
    finally:
        gate.set()
    wait_until(lambda: _snapshot(flow_app, handle).status == "completed", timeout=90)


def test_stop_rejects_an_unknown_children_scope(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/orchestrator")
    response = flow_app.post(f"/v1/sessions/{sid}/cancel", json={"children": "some"})
    assert response.status_code == 422


def _workflow_starter(client: TestClient, gate: threading.Event) -> dict[str, Any]:
    """A three-step workflow whose ``measure`` step waits on ``gate`` the first time."""

    from clio_agent.gact.flow_tools import start_flow

    started: dict[str, Any] = {"attempts": {}}
    workflow = {
        "steps": [
            {"id": "gather", "target": "self", "input": "STEP gather"},
            {"id": "measure", "target": "self", "input": "STEP measure", "after": ["gather"]},
            {"id": "report", "target": "self", "input": "STEP report", "after": ["measure"]},
        ]
    }

    def program(question: str = "", **kwargs: Any) -> Any:
        attempts = started["attempts"]
        if question.startswith("STEP "):
            step = question.split()[1]
            attempts[step] = attempts.get(step, 0) + 1
            if step == "measure" and attempts[step] == 1:
                gate.wait(timeout=30)
            return dspy.Prediction(answer=f"{step} done")
        if question.startswith("run the workflow") and "handle" not in started:
            started.update(
                asyncio.run(start_flow("workflow", {"workflow": workflow, "task": "t"}, None))
            )
            return dspy.Prediction(answer=f"started {started['handle']}")
        return default_program(question=question, **kwargs)

    client.app.state.flow_program["run"] = program
    return started


def _steps(client: TestClient, handle: str) -> dict[str, str]:
    sid = _snapshot(client, handle).owner.session_id
    view = client.get(f"/v1/sessions/{sid}/tasks/{handle}/flow").json()
    return {row["id"]: row.get("handle", "") for row in (view.get("view") or {}).get("steps", [])}


def test_restart_after_stop_keeps_finished_steps(flow_app: TestClient) -> None:
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _workflow_starter(flow_app, gate)
    try:
        post(flow_app, sid, "run the workflow")
        handle = wait_until(lambda: started.get("handle"))
        wait_until(lambda: started["attempts"].get("measure") == 1, timeout=60)
        response = flow_app.post(f"/v1/sessions/{sid}/cancel", json={"children": "stop"})
        assert response.status_code == 204
    finally:
        gate.set()
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, handle).status == "cancelled"
    before = _steps(flow_app, handle)
    assert _snapshot(flow_app, before["gather"]).status == "completed"
    plan_session = _snapshot(flow_app, handle).owner.session_id

    answered = len(assistant_messages(flow_app, sid))
    response = flow_app.post(f"/v1/sessions/{sid}/agent-controls/{handle}/restart")
    assert response.status_code == 200, response.text
    history = flow_app.app.state.task_history
    restarted = wait_until(
        lambda: (latest := history.latest_assignment(plan_session)).handle != handle and latest
    )
    wait_until(lambda: _snapshot(flow_app, restarted.handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, restarted.handle).status == "completed"
    after = _steps(flow_app, restarted.handle)
    assert after["gather"] == before["gather"]  # finished: the same task, not run again
    assert after["measure"] != before["measure"]
    assert started["attempts"] == {"gather": 1, "measure": 2, "report": 1}
    # The stopped run's view points at the run that continues it, so its card follows.
    stopped_view = flow_app.get(f"/v1/sessions/{sid}/tasks/{handle}/flow").json()
    assert stopped_view["restarted_as"] == restarted.handle
    assert (
        "restarted_as"
        not in flow_app.get(f"/v1/sessions/{sid}/tasks/{restarted.handle}/flow").json()
    )
    # The stopped conversation got a linked successor, and the result woke it.
    parent = history.latest_assignment(sid)
    assert parent.invocation_id == "restart-child:" + handle
    assert _snapshot(flow_app, restarted.handle).owner.parent_handle == parent.handle
    wait_until(lambda: len(assistant_messages(flow_app, sid)) > answered, timeout=60)


def test_restart_after_a_restart_and_stop_ignores_a_cut_off_call(
    flow_app: TestClient,
) -> None:
    """A crash cut off a call under the running step; Stop, then restart, still works.

    SABOTAGE: drop ``uncertain_call`` from ``prepare_terminal_restart.settled`` and the
    restart is refused ("Restart descendant settlement remains unconfirmed").
    """
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _workflow_starter(flow_app, gate)
    try:
        post(flow_app, sid, "run the workflow")
        handle = wait_until(lambda: started.get("handle"))
        wait_until(lambda: started["attempts"].get("measure") == 1, timeout=60)
        measure = wait_until(lambda: _steps(flow_app, handle).get("measure"))
        orphan = _orphaned_call(flow_app, measure)
        response = flow_app.post(f"/v1/sessions/{sid}/cancel", json={"children": "stop"})
        assert response.status_code == 204
    finally:
        gate.set()
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, handle).status == "cancelled"
    plan_session = _snapshot(flow_app, handle).owner.session_id
    response = flow_app.post(f"/v1/sessions/{sid}/agent-controls/{handle}/restart")
    assert response.status_code == 200, response.text
    history = flow_app.app.state.task_history
    restarted = wait_until(
        lambda: (latest := history.latest_assignment(plan_session)).handle != handle and latest
    )
    wait_until(lambda: _snapshot(flow_app, restarted.handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, restarted.handle).status == "completed"
    assert _snapshot(flow_app, orphan).status == "task_state_uncertain"


def test_a_started_flow_is_linked_to_the_model_call_that_started_it(
    flow_app: TestClient,
) -> None:
    from clio_agent.gact.agents.context_repair import started_by_call as _started_by_call
    from clio_agent.gact.spawn_invocation import started_tasks

    gate = threading.Event()
    gate.set()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _council_starter(flow_app, gate, model_call="toolu_01abc")
    post(flow_app, sid, "convene the council on linkage")
    handle = wait_until(lambda: started.get("handle"))
    history = flow_app.app.state.task_history
    found = started_tasks(history, sid, "toolu_01abc")
    assert [row.handle for row in found] == [handle]
    assert started_tasks(history, sid, "toolu_other") == []
    note = _started_by_call(found)
    assert note["started_tasks"][0]["label"] == "council flow"
    assert handle in note["started_note"] and "not started again" in note["started_note"]
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=90)


def test_restarting_a_running_flow_says_what_it_cancels(flow_app: TestClient) -> None:
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _workflow_starter(flow_app, gate)
    try:
        post(flow_app, sid, "run the workflow")
        handle = wait_until(lambda: started.get("handle"))
        wait_until(lambda: started["attempts"].get("measure") == 1, timeout=60)
        running = _steps(flow_app, handle)["measure"]
        response = flow_app.post(f"/v1/sessions/{sid}/agent-controls/{handle}/restart")
        assert response.status_code == 200, response.text
        effect = response.json()
        effect = effect.get("effect", effect)
    finally:
        gate.set()
    # The answer says the target was running and names the work the restart ends.
    assert effect["was_running"] is True
    assert running in {row["handle"] for row in effect["cancelled_unfinished"]}
    assert "cancelled its unfinished work" in effect["note"]
    history = flow_app.app.state.task_history
    plan_session = _snapshot(flow_app, handle).owner.session_id
    restarted = wait_until(
        lambda: (latest := history.latest_assignment(plan_session)).handle != handle and latest
    )
    wait_until(lambda: _snapshot(flow_app, restarted.handle).terminal_at is not None, timeout=90)
