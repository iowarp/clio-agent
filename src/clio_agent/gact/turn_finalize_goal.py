"""Finalize + the awaited GOAL step — the one LM call in the finalize region (#1333).

:func:`finalize_turn` is synchronous and runs on the server loop: fast in-memory
work (parts, publishes, persistence, hooks) that must stay single-threaded with the
request handlers (``app.state.pending_diffs`` is an unlocked ledger shared with the
diff apply/reject routes). The run-until GOAL judge is different — it is an LM call.
Run synchronously on the loop thread it froze every session, SSE stream, and
heartbeat for the judge's duration and, for a provider whose sync path nests
``asyncio.run()`` (codex), crashed as ``judge unavailable`` on every turn. So the
goal step is AWAITED here, after the sync finalize returns, through the provider's
native async path.

Why a separate owner module: ``turn_finalize.py`` and ``turn.py`` sit at their
size-ratchet baselines (baselines only move down), and the goal/loop compose is
finalize glue, not turn orchestration.

Why inside the turn coroutine (not a detached task): a not-met verdict ENQUEUES one
re-drive on the loop-inbox, and that enqueue must precede the ``TurnRunner``
done-callback that drains the inbox into the next turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact.goal import dispatch_goal_at_finalize
from clio_agent.gact.turn_finalize import (
    compose_goal_loop_stop_at_finalize,
    finalize_turn,
)

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState


async def finalize_turn_async(
    state: "TurnState",
    pred: Any,
    *,
    drain_observed_tool_calls: Callable[..., Any],
    update_retry_attempt: Callable[..., Any],
) -> None:
    """Run the sync finalize, then await the GOAL judge and compose it with the loop.

    Order: ``finalize_turn`` (sync, on the loop) -> ``dispatch_goal_at_finalize``
    (awaited; never raises except ``CancelledError``) ->
    ``compose_goal_loop_stop_at_finalize`` (a judge-met goal stops an armed loop with the
    typed ``loop_goal_met`` reason). The caller's ``settle_failed_finalize`` envelope covers
    the whole sequence.
    """

    finalize_turn(
        state,
        pred,
        drain_observed_tool_calls=drain_observed_tool_calls,
        update_retry_attempt=update_retry_attempt,
    )
    goal_decision = await dispatch_goal_at_finalize(
        state.app, session_id=state.sid, turn_id=state.turn_id, trace_id=state.trace_id
    )
    compose_goal_loop_stop_at_finalize(state.app, state.sid, goal_decision)


__all__ = ["finalize_turn_async"]
