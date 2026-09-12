"""Child-task-done continuation chaining for the spawn/collect surface.

Split out of ``turn_spawn.py`` (#1333 ratchet payment). A message accepted while
a child turn is still finishing may miss the final tool boundary by a few
milliseconds; the turn-runner's idle hook promotes that residual steer into a
NEW in-flight turn before the ORIGINAL turn's completion callback runs. This
module keeps the ONE logical :class:`~clio_agent.gact.agent_tasks.AgentTask`
attached to that continuation instead of terminalizing it from the stale first
answer, so Wait/Collect observe the final answer that actually incorporates the
accepted message.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def build_child_done_callback(
    on_done: Callable[..., None], app: "FastAPI", task_id: str, child_sid: str, mode: str
) -> Callable[[Any], None]:
    """Build the ``add_done_callback`` closure that re-invokes ``on_done`` for this
    ``(task_id, child_sid, mode)`` triple with the just-finished turn attached.

    Shared by ``turn_spawn._launch`` (arming the FIRST completion hook) and
    :func:`resume_on_continuation_turn` (re-arming it on a later continuation
    turn) -- both need the exact same rebind so a task followed through N
    continuations still resolves through ONE callback shape.
    """

    def _callback(finished: Any) -> None:
        on_done(app, task_id, child_sid, mode, finished_turn=finished)

    return _callback


def resume_on_continuation_turn(
    app: "FastAPI",
    task_id: str,
    child_sid: str,
    mode: str,
    finished_turn: Any,
    on_done: Callable[..., None],
) -> bool:
    """Re-arm ``on_done`` on a residual-steer continuation turn instead of running now.

    The identity check (``continuation is not finished_turn``) prevents following
    the turn whose completion invoked this callback if callback ordering ever
    changes.

    Returns ``True`` when a continuation was found and ``on_done`` was re-armed on
    it (the caller must return without running its own body); ``False`` when there
    is no live continuation and the caller should proceed with its own body.
    """

    continuation = app.state.in_flight_turns.get(child_sid)
    if continuation is None or continuation is finished_turn:
        return False
    continuation.add_done_callback(
        build_child_done_callback(on_done, app, task_id, child_sid, mode)
    )
    return True
