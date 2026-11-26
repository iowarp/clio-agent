"""Deliberate shared waits retain their own limits and remain isolated by session."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionDefinition, QuestionReply
from clio_agent.gact.runtime.globals import _TurnTimedOut
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.turn_watchdog import await_turn_work
from clio_agent.runtime.commitment_activity import commitment_wait_in_flight
from tests.test_arc.test_conversation_owner_contracts import create_environment


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["wait", "observe"])
@pytest.mark.parametrize("timeout_s", [None, 0.2])
async def test_explicit_question_wait_outlives_watchdog_without_extending_a_neighbor(
    arc: ARCMemory,
    tmp_path: Path,
    operation: str,
    timeout_s: float | None,
) -> None:
    """Actual shared controls survive silence, expire deliberately, and never cancel work."""
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    question = await owner.history.io.run(
        app.state.questions.create,
        root.owner,
        QuestionDefinition(prompt="Which value?"),
        invocation_id="watchdog-question",
    )

    async def forbidden_cancel(_task: Any, _caller: Any) -> dict[str, Any]:
        pytest.fail("Cancelling or expiring a waiter must not cancel its Question")

    operations = TaskOperations(owner.history, forbidden_cancel)
    caller = TaskCaller(sid, root.owner.human_id)

    def watchdog(session: str) -> Any:
        return SimpleNamespace(
            app=app,
            sid=session,
            turn_progress_timeout_s=0.02,
            _watchdog_poll_s=0.005,
            turn_cancel_event=threading.Event(),
        )

    current = watchdog(sid)
    waiting = (
        operations.wait(caller, question.handle, timeout_s=timeout_s)
        if operation == "wait"
        else operations.observe(
            caller, question.handle, pattern="selected-value", timeout_s=timeout_s
        )
    )
    work = asyncio.create_task(await_turn_work(current, waiting))
    registered = asyncio.Event()
    remove = owner.history.listen

    def listen(*args: Any, **kwargs: Any) -> Any:
        release = remove(*args, **kwargs)
        registered.set()
        return release

    # Only instrument the real registration; owner records and controls are real.
    operations.history.listen = listen
    try:
        await asyncio.wait_for(registered.wait(), 10)
        assert commitment_wait_in_flight(sid)
        if timeout_s is None:
            neighbor = watchdog("unrelated-session")
            with pytest.raises(_TurnTimedOut):
                await await_turn_work(neighbor, asyncio.Event().wait())
            assert not work.done() and not current.turn_cancel_event.is_set()
            assert neighbor.turn_cancel_event.is_set()
        if timeout_s is not None:
            result = await asyncio.wait_for(work, 10)
            assert result["timed_out"] is True
            assert not current.turn_cancel_event.is_set()
            assert owner.history.task_snapshot(question.handle) == question
        elif operation == "wait":
            await owner.history.io.run(
                app.state.questions.answer, question.handle, QuestionReply(answer="selected-value")
            )
            result = await asyncio.wait_for(work, 10)
            assert not result["unfinished"]
            assert result["outcomes"][0]["status"] == "completed"
            assert "selected-value" in json.dumps(result)
        else:
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            assert owner.history.task_snapshot(question.handle) == question
        assert not commitment_wait_in_flight(sid)
    finally:
        operations.history.listen = remove
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        await owner.history.io.run(app.state.questions.cancel, question.handle)
        await owner.shutdown()
