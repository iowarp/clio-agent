"""A view's own call ends without waking the agent; the agent's call is unchanged (MC-F2)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from clio_agent.arc.task_types import TaskOwner, TaskSnapshot
from clio_agent.gact import view_call_ledger as ledger
from clio_agent.tools import call_origin


class _History:
    def __init__(self, snapshot: TaskSnapshot | None) -> None:
        self.snapshot = snapshot
        self.recorded: list[TaskSnapshot] = []

    @contextmanager
    def serialized(self) -> Any:
        yield

    def task_snapshot(self, _handle: str) -> TaskSnapshot | None:
        return self.snapshot

    def record_snapshot(self, snapshot: TaskSnapshot, *, observation_id: str) -> None:
        assert observation_id
        self.recorded.append(snapshot)
        self.snapshot = snapshot


def _task(**update: Any) -> TaskSnapshot:
    return TaskSnapshot(
        handle="task_poll",
        kind="MCP",
        owner=TaskOwner(human_id="h", session_id="sess_1", assignment_id="a", parent_handle="a"),
        label="pdf_poll_pdf_commands",
        invocation_id="inv",
        tool="pdf_poll_pdf_commands",
        created_at="2026-11-14T00:00:00Z",
        updated_at="2026-11-14T00:00:00Z",
        status="running",
        **update,
    )


def _submission(origin: str | None) -> Any:
    attribution = {"agent": "main", **({"origin": origin} if origin else {})}
    return type("Submission", (), {"attribution": attribution})()


def test_the_view_origin_mark_follows_the_call_origin() -> None:
    assert ledger.attribution() == {}
    with call_origin.scope("view"):
        assert ledger.attribution() == {"origin": "view"}
    assert ledger.is_view_call(_submission("view"))
    assert not ledger.is_view_call(_submission(None))


@pytest.mark.parametrize(
    ("message", "status"),
    [
        (ledger.VIEW_CALL_INTERRUPTED_TEXT, "cancelled"),
        (f"{ledger.VIEW_CALL_FAILED}: connection closed", "failed"),
    ],
)
def test_a_cut_off_view_call_ends_unknown_with_no_episode(message: str, status: str) -> None:
    history = _History(_task())
    ledger.settle_cut_off(history, "task_poll", message, status=status)  # type: ignore[arg-type]
    (ended,) = history.recorded
    assert ended.status == status and ended.settlement == "unknown"
    assert ended.raw_status == message.split(":", 1)[0]
    assert ended.terminal_at is not None and ended.attention_episode is None
    assert ended.progress.message == message and ended.supported_actions == ()
    ledger.settle_cut_off(history, "task_poll", message)  # type: ignore[arg-type]
    assert len(history.recorded) == 1  # an ended call keeps its outcome
    ledger.settle_cut_off(_History(None), "gone", message)  # type: ignore[arg-type]


def test_an_unknown_outcome_wakes_the_agent_only_for_its_own_call() -> None:
    woken: list[tuple[str, str]] = []
    history = _History(_task())
    tasks = type(
        "Tasks", (), {"history": history, "_attention": lambda _s, h, r: woken.append((h, r))}
    )()
    ledger.uncertain(tasks, _submission(None), "task_poll", "broken pipe")()
    assert woken == [("task_poll", "broken pipe")] and history.recorded == []
    ledger.uncertain(tasks, _submission("view"), "task_poll", "broken pipe")()
    assert woken == [("task_poll", "broken pipe")]
    (ended,) = history.recorded
    assert ended.status == "failed" and ended.raw_status == ledger.VIEW_CALL_FAILED


@pytest.mark.asyncio
async def test_a_view_call_in_flight_at_restart_is_settled_at_start() -> None:
    history = _History(_task())
    deferred: list[str] = []

    async def record_or_defer(identity: str, write: Any, *, retire: Any = None) -> None:
        deferred.append(identity)
        write()

    tasks = type(
        "Tasks", (), {"history": history, "_record_or_defer": staticmethod(record_or_defer)}
    )()
    await ledger.restored_cut_off(tasks, "task_poll")
    assert deferred == ["external-recovery:task_poll"]
    (ended,) = history.recorded
    assert ended.raw_status == ledger.VIEW_CALL_INTERRUPTED and ended.attention_episode is None
