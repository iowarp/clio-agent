"""Explicit task cancellation releases input without publishing premature settlement."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from clio_agent.gact.events import EventBus
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.task_supervisor import task_supervisor
from clio_agent.gact.user_question_ledger import record_user_question
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, set_task_record_store
from tests.test_gact.test_task_input_recovery import (
    call_input,
    invocation_for,
    question_for,
)


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Use an isolated durable task/input owner with real session persistence."""
    monkeypatch.setenv("CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DEFAULT_UNHINTED", "0")
    app = FastAPI()
    app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
    app.state.user_questions = {}
    app.state.bus = EventBus()
    session = app.state.sessions.create(workspace_id="ws", title="input cancellation")
    key = TaskKey("server-a", session.id, "backend-task", "backend-session-a")
    assert app.state.sessions.task_store is not None
    app.state.sessions.task_store.put(
        TaskRecord(key, tool="guarded", invocation_id="inv-original", status="input_required")
    )
    yield app, key
    set_task_record_store(None)


async def test_shared_cancel_wakes_pending_input_before_backend_settlement(owner: Any) -> None:
    """A real supervisor request wakes its form; an ack alone never settles the task."""
    app, key = owner
    store = app.state.sessions.task_store
    original = store.get(key)
    store.put(replace(original, handle="task-original"))
    question = question_for(key, status="pending")
    record_user_question(app, question)
    app.state.mcp_app_loop = asyncio.get_running_loop()
    supervisor = task_supervisor(app)
    released = asyncio.Event()
    input_returned = asyncio.Event()
    answers = []
    calls = []

    async def drive() -> None:
        answers.append(await call_input(app, invocation_for(key), timeout=10))
        input_returned.set()
        await released.wait()

    async def cancel() -> dict[str, bool]:
        calls.append("backend-cancel")
        return {"acknowledged": True}

    supervisor.supervise("task-original", drive(), cancel)
    driver = supervisor.drivers["task-original"]
    try:
        await asyncio.sleep(0)
        assert not input_returned.is_set()
        row = {
            "handle": "task-original",
            "task_kind": "MCP",
            "effective_status": "input_required",
            "key": key.to_wire(),
        }
        assert await asyncio.to_thread(supervisor.request_cancel, row)
        await asyncio.wait_for(input_returned.wait(), timeout=1)
        assert answers[0].action == "cancel"
        assert app.state.user_questions[question.id].status == "cancelled"
        assert not driver.done(), "Owner cleanup/settlement must remain independent of the ack"
        current = store.get(key)
        assert current.status == "input_required" and current.cancel_requested
        assert current.cancel_acknowledged
        assert await asyncio.to_thread(supervisor.request_cancel, row)
        assert calls == ["backend-cancel"]
        released.set()
        await driver
    finally:
        released.set()
        await supervisor.shutdown()


async def test_cancel_before_input_publication_does_not_park_a_new_question(owner: Any) -> None:
    """Persisted intent wins even when no input question existed at cancellation time."""
    app, key = owner
    store = app.state.sessions.task_store
    store.put(replace(store.get(key), cancel_requested=True))
    result = await asyncio.wait_for(call_input(app, invocation_for(key), timeout=10), timeout=1)
    assert result.action == "cancel"
    questions = list(app.state.user_questions.values())
    assert len(questions) == 1 and questions[0].status == "cancelled"
    assert questions[0].metadata["elicitation"]["task_key"] == key.to_wire()


@pytest.mark.parametrize("collision", ["server_id", "backend_session_id", "session_id"])
async def test_explicit_cancel_never_closes_another_task_input(owner: Any, collision: str) -> None:
    """The input wake is bound to the same full identity as the backend cancel request."""
    from clio_agent.gact.task_input_questions import cancel_task_inputs

    app, key = owner
    store = app.state.sessions.task_store
    other = replace(key, **{collision: "unrelated-owner"})
    first = question_for(key, status="pending")
    second = question_for(other, status="pending").model_copy(update={"id": "other-question"})
    record_user_question(app, first)
    record_user_question(app, second)
    store.put(replace(store.get(key), cancel_requested=True))
    assert cancel_task_inputs(app, key) == 1
    assert app.state.user_questions[first.id].status == "cancelled"
    assert app.state.user_questions[second.id].status == "pending"
    assert cancel_task_inputs(app, key) == 0


async def test_task_cancel_keeps_an_already_committed_human_answer(owner: Any) -> None:
    """The first-wins question transition remains authoritative during a cancel race."""
    from clio_agent.gact.task_input_questions import cancel_task_inputs

    app, key = owner
    store = app.state.sessions.task_store
    question = question_for(key)
    record_user_question(app, question)
    store.put(replace(store.get(key), cancel_requested=True))
    assert cancel_task_inputs(app, key) == 0
    assert app.state.user_questions[question.id] == question


async def test_cancel_recovers_durable_input_missing_from_hot_projection(owner: Any) -> None:
    """An acknowledged cancel survives restart without leaving an old form pending."""
    from clio_agent.gact.task_input_questions import cancel_task_inputs

    app, key = owner
    store = app.state.sessions.task_store
    question = question_for(key, status="pending")
    record_user_question(app, question)
    app.state.user_questions.clear()
    store.put(replace(store.get(key), cancel_requested=True, cancel_acknowledged=True))
    assert cancel_task_inputs(app, key) == 1
    assert app.state.user_questions[question.id].status == "cancelled"
    stored = app.state.sessions.get(key.session_id).metadata["user_questions"][question.id]
    assert stored["status"] == "cancelled"


async def test_cancel_leaves_ambiguous_legacy_input_for_ownership_reconciliation(
    owner: Any,
) -> None:
    """Equal old backend identifiers cannot authorize closing another server session's input."""
    from clio_agent.gact.task_input_questions import cancel_task_inputs

    app, key = owner
    store = app.state.sessions.task_store
    question = question_for(key, status="pending")
    del question.metadata["elicitation"]["task_key"]
    record_user_question(app, question)
    row = store.get(key)
    store.put(replace(row, cancel_requested=True))
    store.put(replace(row, key=replace(key, backend_session_id="colliding-backend")))
    assert cancel_task_inputs(app, key) == 0
    assert app.state.user_questions[question.id].status == "pending"
