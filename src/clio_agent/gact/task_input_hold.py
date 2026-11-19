"""Hold a task-augmented call while its task waits for an answer it asked for.

A plain tool call that elicits input blocks until the answer comes, like a permission
ask: the turn waits and goes on with the answer. A task-augmented call returns a
receipt as soon as the server accepts it. When the server asks for input before that
receipt reaches the agent (MCP ``input_required``), the agent used to get a running
handle, poll it, see it "awaiting input", and end its turn telling the person to look
for a request, which cost a turn once they answered.

:func:`hold_while_input` keeps such a call in the foreground until the task stops
waiting for input (answered, declined, settled or cancelled) or the calling turn is
stopped. Nothing here names a server; any task that asks for input is held the same
way. The wait counts as deliberate activity for the turn watchdog.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from clio_agent.arc.task_types import TaskSnapshot
from clio_agent.runtime.commitment_activity import track

#: How long a held call waits for the server's first status report after acceptance
#: (one ``tasks/get`` round trip), so an ask it makes at once is not missed.
FIRST_REPORT_S = 2.0


def waiting_for_answer(task: TaskSnapshot | None) -> bool:
    """Whether ``task`` is open and waiting for input it asked for."""

    return (
        task is not None
        and task.terminal_at is None
        and (
            task.status == "input_required"
            or task.activity == "waiting_for_input"
            or task.raw_status == "input_required"
        )
    )


async def hold_while_input(
    app: Any,
    handle: str,
    *,
    assignment: str,
    session_id: str,
    reported: Callable[[], bool] = lambda: True,
    first_report_s: float = FIRST_REPORT_S,
) -> bool:
    """Wait while task ``handle`` waits for input; True when the call was held at all.

    Until ``reported()`` says the server has reported on the task since accepting it,
    waits at most ``first_report_s`` for that first report. Then returns once the task
    no longer waits for input, or the calling ``assignment`` was stopped (Stop retires
    a turn only once its calls return). A cancellation of the call propagates; the
    task itself is left to its owner either way.
    """

    history = app.state.task_history
    operations = getattr(app.state, "task_operations", None)
    loop, changed = asyncio.get_running_loop(), asyncio.Event()
    first_by = loop.time() + first_report_s

    def wake(_revision: int) -> None:
        loop.call_soon_threadsafe(changed.set)

    remove = history.listen(wake, handles=(handle, assignment))
    held = False
    try:
        with track(True, session_id=session_id):
            while True:
                changed.clear()
                current = await history.io.run(history.task_snapshot, handle)
                if current is None or current.terminal_at is not None:
                    return held
                if not waiting_for_answer(current):
                    remaining = first_by - loop.time()
                    if held or reported() or remaining <= 0:
                        return held
                    try:
                        await asyncio.wait_for(changed.wait(), remaining)
                    except TimeoutError:
                        pass
                    continue
                if operations is not None and await history.io.run(
                    operations.assignment_stopped, assignment
                ):
                    return held
                held = True
                await changed.wait()
    finally:
        remove()


__all__ = ["hold_while_input", "waiting_for_answer"]
