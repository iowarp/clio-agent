"""Finalize off the loop + the awaited GOAL step (#1333, #1334).

The turn orchestrator (``turn.py::_run_turn_in_background``) is an async task on the
server loop. Two things in the finalize region must NOT run on that loop thread:

* **The GOAL judge** (#1333): an LM call. Run synchronously on the loop it froze every
  session, SSE stream, and heartbeat for its duration and, for a provider whose sync
  path nests ``asyncio.run()`` (codex), crashed as ``judge unavailable`` every turn.
  It is AWAITED here through the provider's native async path.
* **The synchronous finalize itself** (#1334): ARC scope persistence issues several
  clio-core RPCs per turn and the loop thread sat in ``threading.wait`` on each of
  them (main-thread stack samples inside the live gate's 5 s stall), and the Stop /
  loop hooks run operator subprocesses. ``finalize_turn`` therefore runs on the turn
  executor with the caller's contextvars copied, the pattern
  ``turn_forward.py::_run_turn_setup_off_loop`` established for cold setup. The
  in-memory list ledgers finalize appends to are guarded
  (``runtime.retention.ledger_guard``) so the diff / context routes on the loop cannot
  race the executor.

Why a separate owner module: ``turn_finalize.py`` and ``turn.py`` sit at their
size-ratchet baselines (baselines only move down), and the goal/loop compose is
finalize glue, not turn orchestration.

Why inside the turn coroutine (not a detached task): a not-met verdict ENQUEUES one
re-drive on the loop-inbox, and that enqueue must precede the ``TurnRunner``
done-callback that drains the inbox into the next turn. Event ordering holds too:
finalize's publishes are bridged onto the loop with ``call_soon_threadsafe`` from
the worker BEFORE the executor future resolves, so they are delivered before this
coroutine resumes.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact.goal import dispatch_goal_at_finalize
from clio_agent.gact.turn_finalize import (
    compose_goal_loop_stop_at_finalize,
    finalize_turn,
)
from clio_agent.gact.turn_forward import _forward_executor

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState


async def finalize_turn_async(
    state: "TurnState",
    pred: Any,
    *,
    drain_observed_tool_calls: Callable[..., Any],
    update_retry_attempt: Callable[..., Any],
) -> None:
    """Run finalize on the turn executor, then await the GOAL judge and the loop compose.

    Order: ``finalize_turn`` (sync, on the turn executor, contextvars copied) ->
    ``dispatch_goal_at_finalize`` (awaited; never raises except ``CancelledError``) ->
    ``compose_goal_loop_stop_at_finalize`` (a judge-met goal stops an armed loop with the
    typed ``loop_goal_met`` reason). The caller's ``settle_failed_finalize`` envelope covers
    the whole sequence: an exception in the worker propagates through the await.
    """

    loop = asyncio.get_running_loop()
    turn_context = contextvars.copy_context()
    await loop.run_in_executor(
        _forward_executor(state),
        lambda: turn_context.run(
            finalize_turn,
            state,
            pred,
            drain_observed_tool_calls=drain_observed_tool_calls,
            update_retry_attempt=update_retry_attempt,
        ),
    )
    goal_decision = await dispatch_goal_at_finalize(
        state.app,
        session_id=state.sid,
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        executor=_forward_executor(state),  # #1334: the post-judge persistence, off-loop
    )
    compose_goal_loop_stop_at_finalize(state.app, state.sid, goal_decision)


__all__ = ["finalize_turn_async"]
