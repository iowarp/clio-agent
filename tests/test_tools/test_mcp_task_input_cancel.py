"""A cancelled input task is polled to settlement without answers or replay."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import mcp_types
import pytest

from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskInputAnswer,
    TaskKey,
    TaskRecord,
)
from clio_agent.tools.mcp_tasks import drive_task_to_terminal
from tests.test_tools.test_mcp_tasks import ScriptedSession, _elicit_request, _task_payload


@pytest.mark.parametrize("answer_state", ["absent", "captured", "delivered"])
async def test_cancelled_input_keeps_polling_without_answer_or_no_progress_failure(
    answer_state: str,
) -> None:
    """Cancellation can settle slowly beyond the normal input retry guard."""
    key = TaskKey("owned-server", "owned-session", "original-task")
    store = InMemoryTaskRecordStore()
    answers = (
        ()
        if answer_state == "absent"
        else (
            TaskInputAnswer(
                "input-1",
                {"action": "accept", "content": {"answer": "stored"}},
                delivered=answer_state == "delivered",
            ),
        )
    )
    store.put(
        TaskRecord(key, status="input_required", cancel_requested=True, input_answers=answers)
    )
    session: Any = ScriptedSession(
        [
            *[
                _task_payload(
                    key.task_id,
                    "input_required",
                    input_requests={
                        "input-1": _elicit_request("Input still pending during backend cleanup"),
                    },
                )
                for _ in range(4)
            ],
            _task_payload(key.task_id, "cancelled"),
        ]
    )
    pending_statuses = []

    async def poll_sleep(delay: float) -> None:
        assert delay > 0
        row = store.get(key)
        assert row is not None
        pending_statuses.append(row.status)

    async def forbidden_input(*args: Any) -> Any:
        raise AssertionError("Cancelled tasks must not elicit another answer")

    result = await drive_task_to_terminal(
        session,
        key,
        forbidden_input,
        store=store,
        max_no_progress_rounds=1,
        poll_sleep=poll_sleep,
    )
    assert result.status == "cancelled"
    assert pending_statuses == ["input_required"] * 4
    assert session.methods() == ["tasks/get"] * 5
    row = store.get(key)
    assert row is not None and row.status == "cancelled"
    assert row.input_answers == answers


async def test_cancellation_while_human_waits_sends_no_input_update() -> None:
    """A task cancel waking the callback never becomes a synthetic answer payload."""
    key = TaskKey("owned-server", "owned-session", "original-task")
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key, status="input_required"))
    session: Any = ScriptedSession(
        [
            _task_payload(
                key.task_id,
                "input_required",
                input_requests={
                    "input-1": _elicit_request("Choose a value"),
                },
            ),
            _task_payload(key.task_id, "cancelled"),
        ]
    )
    elicited = []

    async def input_cancelled(context: Any, params: Any) -> Any:
        elicited.append(params.message)
        row = store.get(key)
        assert row is not None
        store.put(replace(row, cancel_requested=True))
        return mcp_types.ElicitResult(action="cancel")

    async def poll_sleep(delay: float) -> None:
        assert delay > 0

    result = await drive_task_to_terminal(
        session,
        key,
        input_cancelled,
        store=store,
        poll_sleep=poll_sleep,
    )
    assert result.status == "cancelled"
    assert elicited == ["Choose a value"]
    assert session.methods() == ["tasks/get", "tasks/get"]
    row = store.get(key)
    assert row is not None and row.input_answers == ()
