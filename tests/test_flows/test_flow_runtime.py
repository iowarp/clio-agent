"""ctx primitives on the real task layer: call, gather, ask, step, limits, checkpoints."""

from __future__ import annotations

from typing import Any

import pytest
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


def test_a_child_waiting_for_input_settles_the_call_instead_of_holding_the_flow(
    flow_app: TestClient,
) -> None:
    """A child that yields with nothing to wake it is a typed outcome with its last answer.

    Live: a workflow's researcher yielded after its fetches were refused, and the flow
    waited on it forever. The child stays open, so a person can still message it.
    """

    sid = new_session(flow_app, "clio/yielderer")
    post(flow_app, sid, "go")
    message = _answer(flow_app, sid)
    assert text_of(message) == "CallAwaitingInput: need approval to continue"
    run = _checkpoint(flow_app, sid)
    (row,) = run["calls"].values()
    assert row["status"] == "awaiting_input"
    child = flow_app.app.state.task_history.task_snapshot(row["handle"])
    assert child.terminal_at is None and child.activity == "yielded"


def test_a_child_whose_turn_stopped_settles_the_call_as_a_typed_outcome(
    flow_app: TestClient,
) -> None:
    """A child whose turn stopped with nothing to resume it no longer holds its flow.

    Live: a deep-research critic's model call stalled, the no-progress bound ended its
    turn, and the flow waited on the open child for good. The child stays open, so a
    person can still resume it.
    """

    sid = new_session(flow_app, "clio/stopperer")
    post(flow_app, sid, "go")
    history = flow_app.app.state.task_history

    def running_child() -> Any:
        rows = flow_app.get(f"/v1/sessions/{sid}/tasks/query?kind=Subagent&limit=50").json()
        for row in rows.get("tasks", []):
            child = history.task_snapshot(row["handle"])
            if child is not None and child.owner.session_id != sid and child.activity == "busy":
                return child
        return None

    child = wait_until(running_child)
    stopped = flow_app.post(f"/v1/sessions/{child.owner.session_id}/cancel")
    assert stopped.status_code in {200, 202, 204}, stopped.text

    message = _answer(flow_app, sid)
    assert text_of(message) == "CallStopped: call_stopped"
    (row,) = _checkpoint(flow_app, sid)["calls"].values()
    assert row["status"] == "stopped"
    child = history.task_snapshot(row["handle"])
    assert child.terminal_at is None and child.activity == "stopped"


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


def test_a_run_that_raises_records_how_it_ended(flow_app: TestClient) -> None:
    """Live, a council whose synthesis raised stayed "running" in its record after its task failed."""

    sid = new_session(flow_app, "clio/breakerer")
    post(flow_app, sid, "go")
    _answer(flow_app, sid)
    run = _checkpoint(flow_app, sid)
    assert run["status"] == "flow_error"
    assert run["result"]["error"] == "ValueError"
    assert run["result"]["message"] == "could not parse the reply to first"


def test_model_call_limit_is_typed(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/greedier")
    post(flow_app, sid, "go")
    message = _answer(flow_app, sid)
    error = message.get("error_info") or {}
    assert error.get("error") == "flow_limit_exceeded", message
    assert error.get("details", {}).get("limit") == "max_model_calls"


@pytest.mark.parametrize("target", ["direct", "nested"])
def test_budget_refuses_queued_children_after_the_first_spends_it(
    flow_app: TestClient, target: str
) -> None:
    """Calls prepared together must recheck spend when their execution slot opens."""
    from clio_agent.gact import context
    from tests.test_flows.conftest import default_program

    executed: list[str] = []

    def spending_program(question: str = "", **kwargs: Any) -> Any:
        executed.append(question)
        flow_app.app.state.sessions.update(
            context.active_session_id(), add_tokens_input=10, add_tokens_output=1
        )
        return default_program(question, **kwargs)

    flow_app.app.state.flow_program["run"] = spending_program
    sid = new_session(flow_app, "clio/budgeter")
    post(flow_app, sid, target)
    assert text_of(_answer(flow_app, sid)) == "CallResult,FlowLimitExceeded,FlowLimitExceeded"
    assert executed == ["budgeted part 0"]
    (row,) = _checkpoint(flow_app, sid)["calls"].values()
    history = flow_app.app.state.task_history
    child_ids = {row["session_id"], *history.session_tree_ids(row["session_id"])}
    children = [flow_app.app.state.sessions.get(child_id) for child_id in child_ids]
    assert sum(child.tokens_input + child.tokens_output for child in children) == 11


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
        lambda: next(
            (m for m in assistant_messages(flow_app, sid) if "approved" in text_of(m)), None
        )
    )
    assert text_of(message) == "done: approved work"


def test_depth_limit_refuses_a_flow_nested_too_deep(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/nesterer")
    post(flow_app, sid, "go")
    # The root (depth 0) may start its flow at depth 1; that run may not start depth 2.
    assert text_of(_answer(flow_app, sid)) == "child:FlowLimitExceeded:flow_limit_exceeded:1"


def test_checkpoint_cap_ends_the_run_with_a_typed_outcome(
    flow_app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("clio_agent.flows.journal.MAX_ENTRIES", 2)
    sid = new_session(flow_app, "clio/fanouter")
    post(flow_app, sid, "the survey")
    message = _answer(flow_app, sid)
    error = message.get("error_info") or {}
    assert error.get("error") == "flow_checkpoint_limit", message
    run = _checkpoint(flow_app, sid)
    assert run["entries"] == 2 and run["status"] == "flow_checkpoint_limit"
