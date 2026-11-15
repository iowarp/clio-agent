"""A run whose own turn is retired mid-wait leaves the call open, not uncertain.

At a graceful server shutdown the run's assignment is retired: ``wait`` returns
``interrupted="stopped"`` with the member still pending. Recording that as
``task_state_uncertain`` wrote a researcher (and the critics started after it) into
the journal as failed while they were still running, so the resumed deep-research run
dropped their work. The call must stay open so the resumed run waits on it again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.flows.context import FlowContext
from clio_agent.flows.outcomes import TaskStateUncertain


def _ctx(waited: dict[str, Any]) -> tuple[SimpleNamespace, list[tuple[str, str]]]:
    settled: list[tuple[str, str]] = []

    async def wait(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return waited

    async def run(fn: Any, *args: Any) -> Any:
        return fn(*args)

    async def settle(invocation: str, status: str) -> None:
        settled.append((invocation, status))

    ctx = SimpleNamespace(
        caller=object(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                task_operations=SimpleNamespace(wait=wait),
                task_history=SimpleNamespace(io=SimpleNamespace(run=run)),
            )
        ),
        _collect=lambda _handle: None,
        _settle=settle,
    )
    return ctx, settled


def test_a_retired_wait_cancels_without_settling_the_call() -> None:
    """SABOTAGE: drop the ``interrupted == "stopped"`` branch and the call is settled
    ``task_state_uncertain`` (a running researcher recorded as failed)."""
    ctx, settled = _ctx({"outcomes": [], "pending": ["task_r"], "interrupted": "stopped"})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(FlowContext._wait(ctx, "task_r", "inv", kind="subagent", name="researcher"))
    assert settled == []


def test_a_member_still_pending_without_interruption_is_uncertain() -> None:
    ctx, settled = _ctx(
        {
            "outcomes": [{"handle": "task_r", "status": "task_state_uncertain"}],
            "pending": ["task_r"],
        }
    )
    with pytest.raises(TaskStateUncertain):
        asyncio.run(FlowContext._wait(ctx, "task_r", "inv", kind="subagent", name="researcher"))
    assert settled == [("inv", "task_state_uncertain")]
