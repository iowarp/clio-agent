"""ctx primitives on the real task layer: call, gather, ask, step, limits, checkpoints."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.test_flows.conftest import (
    assistant_messages,
    new_session,
    post,
    text_of,
    wait_until,
)


def _answer(client: TestClient, sid: str, *, timeout: float = 60.0) -> dict[str, Any]:
    return wait_until(lambda: (assistant_messages(client, sid) or [None])[0], timeout=timeout)


def _checkpoint(client: TestClient, sid: str) -> dict[str, Any]:
    history = client.app.state.task_history
    owner = history.latest_assignment(sid)
    checkpoint = history.workflow_checkpoint(owner.handle)
    assert checkpoint is not None
    return history.read_content(checkpoint.content)


def test_gather_runs_children_as_tasks_and_checkpoints_them(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/fanouter")
    post(flow_app, sid, "the survey")
    message = _answer(flow_app, sid)
    assert text_of(message) == " | ".join(f"done: part {i} of the survey" for i in range(3))
    run = _checkpoint(flow_app, sid)
    assert run["flow"] == "fanout" and run["status"] == "completed"
    calls = list(run["calls"].values())
    assert len(calls) == 3 and {c["status"] for c in calls} == {"completed"}
    history = flow_app.app.state.task_history
    owner = history.latest_assignment(sid)
    for row in calls:
        child = history.task_snapshot(row["handle"])
        assert child.kind == "Subagent" and child.owner.parent_handle == owner.handle
        assert child.status == "completed"


def test_gather_member_failure_never_cancels_the_others(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/tolerater")
    post(flow_app, sid, "go")
    assert text_of(_answer(flow_app, sid)) == "CallResult,CallFailed,CallResult"


def test_gather_failure_is_a_typed_outcome(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/stricter")
    post(flow_app, sid, "go")
    message = _answer(flow_app, sid)
    error = message.get("error_info") or {}
    assert error.get("error") == "gather_failed", message
    run = _checkpoint(flow_app, sid)
    assert run["status"] == "gather_failed"
    statuses = sorted(row["status"] for row in run["calls"].values())
    assert statuses == ["completed", "failed"]


def test_step_runs_once_and_is_recorded(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/stepperer")
    post(flow_app, sid, "go")
    assert text_of(_answer(flow_app, sid)) == "AB"
    run = _checkpoint(flow_app, sid)
    assert sorted(row["result"] for row in run["steps"].values()) == ["A", "B"]


def test_model_call_limit_is_typed(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/greedier")
    post(flow_app, sid, "go")
    message = _answer(flow_app, sid)
    error = message.get("error_info") or {}
    assert error.get("error") == "flow_limit_exceeded", message
    assert error.get("details", {}).get("limit") == "max_model_calls"


def test_ask_waits_on_a_question_task(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/askerer")
    post(flow_app, sid, "go")
    history = flow_app.app.state.task_history

    def question() -> Any:
        owner = history.latest_assignment(sid)
        checkpoint = history.workflow_checkpoint(owner.handle) if owner is not None else None
        run = history.read_content(checkpoint.content) if checkpoint is not None else {}
        rows = [r for r in run.get("calls", {}).values() if r.get("kind") == "ask"]
        return rows[0]["handle"] if rows else None

    handle = wait_until(question)
    snapshot = history.task_snapshot(handle)
    assert snapshot.kind == "Question"
    answered = flow_app.post(
        f"/v1/sessions/{sid}/questions/{handle}/answer",
        json={"answer": "yes", "selected_options": ["yes"]},
    )
    assert answered.status_code in (200, 202), answered.text
    message = wait_until(
        lambda: next((m for m in assistant_messages(flow_app, sid) if "approved" in text_of(m)), None)
    )
    assert text_of(message) == "done: approved work"
