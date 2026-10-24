"""A stopped child whose cancellation is underway is left to its cancellation.

Cancelling a workflow stops its step child's turn. Settling that child as ``stopped``
before its cancellation landed ended the workflow as a stall, not ``cancelled``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.arc.task_types import TaskSnapshot
from clio_agent.gact.assignment_input import waiting_for_input


def _child() -> TaskSnapshot:
    return TaskSnapshot.model_construct(
        handle="task_child",
        kind="Subagent",
        owner=SimpleNamespace(session_id="sess_child"),
        activity="stopped",
        terminal_at=None,
    )


def _app(*, cancelling: bool, job: bool) -> Any:
    conversations = SimpleNamespace(
        cancelling=lambda task: cancelling,
        cancellation_job=lambda handle: object() if job else None,
        runner=SimpleNamespace(busy=lambda sid: True),
    )
    return SimpleNamespace(
        state=SimpleNamespace(conversations=conversations, task_history=object())
    )


def test_a_stopped_child_being_cancelled_is_not_settled() -> None:
    assert waiting_for_input(_app(cancelling=True, job=False), _child()) == ""
    assert waiting_for_input(_app(cancelling=False, job=True), _child()) == ""


def test_a_stopped_child_not_being_cancelled_is_considered() -> None:
    # Its turn has not released yet, so the wait looks again once it has.
    assert waiting_for_input(_app(cancelling=False, job=False), _child()) == "retiring"
