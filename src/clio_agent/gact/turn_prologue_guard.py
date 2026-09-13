"""Prologue-completion guard for the GACT turn finalize seam (#1339 round 5).

``turn.py::_run_turn_in_background`` wraps its whole forward region (the prologue
await + the forward call) in one big ``try/except`` chain, then falls through
UNCONDITIONALLY to the finalize call -- every specific handler (``_TurnCancelled``,
``asyncio.CancelledError`` -> ``settle_asyncio_cancellation``, ``_TurnTimedOut``, ...,
the generic ``except Exception``) sets ``state.error_info``/``answer_text`` but never
returns early. That is correct for a turn that reached ``forward_turn`` and failed
there, but a turn can also be interrupted BEFORE its off-loop prologue
(:func:`~clio_agent.gact.turn_start_offloop.prepare_turn_off_loop`) ever runs: an
``asyncio.CancelledError`` landing on ``_run_turn_in_background`` while it awaits
``_run_turn_setup_off_loop``'s ``loop.run_in_executor(...)`` dispatch is delivered to
the wrapping ``asyncio`` future, which cancels the underlying (still-queued)
``concurrent.futures.Future`` -- the wrapped callable never starts, so
``prepare_turn_off_loop`` never assigns ``context_frame`` / ``context_file_
provenance`` / ``enriched_text`` / ``memory_search_metadata`` at all. Proof this
happened (rather than a partial run) is
:func:`~clio_agent.gact.turn_start_offloop.spawn_user_turn`'s ``_flush_orphaned_job``
done-callback finding the ``DeferredTranscriptJob`` still unclaimed, since
``prepare_turn_off_loop``'s FIRST action is ``state.transcript_job.take()``.

Guarding each prologue-derived field with its own ``None``/default check (the #1338
``b60a6a6f`` fix for ``context_file_provenance``) only moves the crash to the next
untouched field (``context_frame`` next) -- the wrong fix. This module is the single
structural guard: :data:`TurnState.prologue_completed` is one fact, set by
``prepare_turn_off_loop`` itself only once every prologue-derived field it owns has
been assigned, and :func:`run_finalize_or_settle_prologue_gap` checks it ONCE, before
finalize ever touches any of those fields.

Also relocates ``turn.py``'s former ``_settle_failed`` closure (the #756 finalize-crash
envelope) here as :func:`settle_finalize_crash`, a free function instead of a
closure, so both settle paths (a finalize crash, and a prologue that never ran) share
one dispatcher and one owner module -- ``turn.py`` and ``turn_finalize.py`` are both
already at their size-ratchet baselines (#775 no-accretion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact.turn_finalize import settle_failed_finalize
from clio_agent.gact.turn_finalize_goal import finalize_turn_async
from clio_agent.gact.turn_forward import _run_turn_setup_off_loop
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState

#: Typed reason settled through :func:`settle_failed_finalize` when a turn's off-loop
#: prologue never completed -- the STRUCTURAL guard, keyed on one fact
#: (``TurnState.prologue_completed``), replacing per-field ``None`` checks.
REASON_PROLOGUE_NEVER_RAN = "turn_prologue_never_ran"

#: The audit stage name for the stream-audit row recorded alongside the settle
#: (mirrors ``turn_start_offloop.DEFERRED_JOB_ORPHANED``'s own audit call -- both fire
#: for the same underlying event, from the two ends of it: the done-callback proves
#: the job was never claimed, this proves finalize was never allowed to read the
#: fields the same skipped prologue would have set).
AUDIT_PROLOGUE_NEVER_RAN = "turn.prologue_never_ran"


class _TurnPrologueNeverRan(RuntimeError):
    """Carries the typed reason through :func:`settle_failed_finalize` unmodified.

    Never raised/caught as control flow -- constructed once, handed to
    ``settle_failed_finalize`` as its ``exc``, so the existing finalize-crash envelope
    (structured log, ``turn.failed`` event, ``message.completed`` with
    ``stop_reason=error``, persisted assistant error message, terminal session status)
    fires for this case too, tagged with ``settle_reason``/``settle_error_code``
    instead of the generic ``finalize_error``/``turn_finalize_error`` it defaults to.
    """

    settle_reason = REASON_PROLOGUE_NEVER_RAN
    settle_error_code = REASON_PROLOGUE_NEVER_RAN

    def __init__(self, turn_id: str) -> None:
        super().__init__(
            f"turn {turn_id} never reached its off-loop prologue "
            "(prepare_turn_off_loop); finalize is skipped rather than reading "
            "prologue-derived state that was never assigned"
        )
        self.turn_id = turn_id


async def settle_finalize_crash(
    state: "TurnState",
    *,
    update_retry_attempt: Callable[..., Any],
    exc: BaseException,
) -> None:
    """The #756 envelope for a finalize-region crash, off the loop (#1334: it
    persists the error turn's message). Relocated verbatim from ``turn.py``'s former
    ``_settle_failed`` closure -- now a free function so it and
    :func:`run_finalize_or_settle_prologue_gap` share one owner module."""

    await _run_turn_setup_off_loop(
        state,
        lambda: settle_failed_finalize(
            state.app,
            state.sid,
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            turn_tokens=state.turn_tokens,
            turn_cost=state.turn_cost,
            turn_cancel_event=state.turn_cancel_event,
            update_retry_attempt=update_retry_attempt,
            exc=exc,
        ),
    )


async def run_finalize_or_settle_prologue_gap(
    state: "TurnState",
    *,
    drain_observed_tool_calls: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    update_retry_attempt: Callable[..., Any],
) -> None:
    """The single choke point between the forward except-chain and finalize.

    ``state.prologue_completed`` is ``False`` only when
    :func:`~clio_agent.gact.turn_start_offloop.prepare_turn_off_loop` never reached
    its own end -- finalize is skipped entirely (it never touches ``context_frame`` /
    ``context_file_provenance`` / ``enriched_text`` / ``memory_search_metadata`` /
    the workflow schema for such a turn) and the turn is settled typed via the SAME
    #756 machinery a finalize crash uses. Otherwise this is byte-for-byte the former
    ``turn.py`` ``try/except finalize_exc`` envelope.
    """

    if not state.prologue_completed:
        stream_audit(
            AUDIT_PROLOGUE_NEVER_RAN,
            session_id=state.sid,
            turn_id=state.turn_id,
            reason=REASON_PROLOGUE_NEVER_RAN,
        )
        await settle_finalize_crash(
            state,
            update_retry_attempt=update_retry_attempt,
            exc=_TurnPrologueNeverRan(state.turn_id),
        )
        return
    try:
        await finalize_turn_async(
            state,
            state.pred,
            drain_observed_tool_calls=drain_observed_tool_calls,
            update_retry_attempt=update_retry_attempt,
        )
    except Exception as finalize_exc:  # noqa: BLE001 - detached task: settle, no re-raise
        await settle_finalize_crash(
            state, update_retry_attempt=update_retry_attempt, exc=finalize_exc
        )


__all__ = [
    "AUDIT_PROLOGUE_NEVER_RAN",
    "REASON_PROLOGUE_NEVER_RAN",
    "run_finalize_or_settle_prologue_gap",
    "settle_finalize_crash",
]
