"""Prologue-phase guard for the GACT turn finalize seam (L1 slice, #1339 follow-on).

``turn.py::_run_turn_in_background`` wraps its whole forward region (the prologue
await + the forward call) in one big ``try/except`` chain, then falls through
UNCONDITIONALLY to the finalize call -- every specific handler (``_TurnCancelled``,
``asyncio.CancelledError`` -> ``settle_asyncio_cancellation``, ``_TurnTimedOut``, ...,
the generic ``except Exception``) sets ``state.error_info``/``answer_text`` but never
returns early. That is correct for a turn that reached ``forward_turn`` and failed
there, but a turn can also be interrupted BEFORE, or WHILE, its off-loop prologue
(:func:`~clio_agent.gact.turn_start_offloop.prepare_turn_off_loop`) runs. This module
is the single choke point between that except-chain and finalize; it settles by
:attr:`~clio_agent.gact.turn_state.TurnState.prologue_phase` (see that module's
docstring for the four states) instead of a single completed/not-completed bit, so
each of the three broken cases below settles typed with its REAL cause:

* **``failed``** -- an exception escaped the prologue that is NOT a cancel signal
  (e.g. the deferred transcript job raising ``TranscriptIngestError``). The turn.py
  except-chain's generic ``except Exception`` branch already overwrites
  ``state.error_info`` with a throwaway ``agent_error`` ("agent.forward raised: …",
  which is even the WRONG stage name) — this guard ignores that and settles with the
  ACTUAL exception captured on ``state.prologue_error``, so the real cause reaches
  the client instead of a fabricated ``turn_prologue_never_ran``.
* **``running``** -- the prologue was still running (never reached its own
  ``completed``/``failed`` exit) when this guard is reached. The only way that
  happens is a hard cancel: either ``prepare_turn_off_loop``'s own cooperative
  checkpoint raised ``TurnCancelledDuringPrologue`` (or a killed hook subprocess
  raised ``HookCancelled``), or an ``asyncio.CancelledError`` landed on the awaiting
  coroutine while the executor THREAD was still running past that (Python cannot
  forcibly kill a running thread; the cooperative checkpoints are what make this the
  rare case instead of the default one). Settles typed
  ``turn_cancelled_during_prologue``, never ``turn_prologue_never_ran`` -- the
  prologue DID start.
* **``not_started``** -- the prologue callable never began running at all: a cancel
  landed while it was still queued on the executor, before
  ``prepare_turn_off_loop`` could even flip its phase to ``running``. Proof this
  happened (rather than a partial run) is
  :func:`~clio_agent.gact.turn_start_offloop.spawn_user_turn`'s ``_flush_orphaned_job``
  done-callback finding the ``DeferredTranscriptJob`` still unclaimed, since
  ``prepare_turn_off_loop``'s first checkpoint runs before it claims that job.
  Settles typed ``turn_prologue_never_ran`` -- the ONLY case that reason is still
  correct for.

Guarding each prologue-derived field with its own ``None``/default check (the #1338
``b60a6a6f`` fix for ``context_file_provenance``) only moves the crash to the next
untouched field (``context_frame`` next) -- the wrong fix. ``completed`` is the only
phase where finalize is allowed to touch those fields at all.

Also relocates ``turn.py``'s former ``_settle_failed`` closure (the #756 finalize-crash
envelope) here as :func:`settle_finalize_crash`, a free function instead of a
closure, so every settle path (a finalize crash, a real prologue crash, a cancelled
prologue, a never-ran prologue) shares one dispatcher and one owner module --
``turn.py`` and ``turn_finalize.py`` are both already at their size-ratchet baselines
(#775 no-accretion). Every branch below closes the turn minter (via
:func:`settle_finalize_crash` -> ``settle_failed_finalize``'s unconditional
``close_turn_minter`` call) -- the former early return in ``settle_failed_finalize``
that skipped it when the session was already ``cancelled`` is deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact.turn_finalize import settle_failed_finalize
from clio_agent.gact.turn_finalize_goal import finalize_turn_async
from clio_agent.gact.turn_forward import _run_turn_setup_off_loop
from clio_agent.gact.turn_state import (
    PROLOGUE_COMPLETED,
    PROLOGUE_FAILED,
    PROLOGUE_RUNNING,
    TurnCancelledDuringPrologue,
)
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState

#: Typed reason settled through :func:`settle_failed_finalize` when a turn's off-loop
#: prologue never even started (``prologue_phase == "not_started"``).
REASON_PROLOGUE_NEVER_RAN = "turn_prologue_never_ran"

#: Typed reason settled when a turn's off-loop prologue was still ``"running"`` (a
#: hard cancel) by the time this guard runs -- see :class:`TurnCancelledDuringPrologue`.
REASON_CANCELLED_DURING_PROLOGUE = TurnCancelledDuringPrologue.settle_reason

#: The audit stage name for the stream-audit row recorded alongside a never-ran
#: settle (mirrors ``turn_start_offloop.DEFERRED_JOB_ORPHANED``'s own audit call --
#: both fire for the same underlying event, from the two ends of it: the
#: done-callback proves the job was never claimed, this proves finalize was never
#: allowed to read the fields the same skipped prologue would have set).
AUDIT_PROLOGUE_NEVER_RAN = "turn.prologue_never_ran"

#: The audit stage name recorded alongside a cancelled-mid-prologue settle.
AUDIT_CANCELLED_DURING_PROLOGUE = "turn.cancelled_during_prologue"


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

    Branches on :attr:`~clio_agent.gact.turn_state.TurnState.prologue_phase` (see
    the module docstring for the full rationale of each branch):

    * ``"completed"`` -- every prologue-derived field is assigned; run finalize as
      normal (byte-for-byte the former ``turn.py`` ``try/except finalize_exc``
      envelope).
    * ``"failed"`` -- a REAL exception (``state.prologue_error``) escaped the
      prologue; settle with THAT exception, not a fabricated reason.
    * ``"running"`` -- the prologue was cancelled mid-flight; settle typed
      ``turn_cancelled_during_prologue``.
    * ``"not_started"`` (the default) -- the prologue callable never began
      running; settle typed ``turn_prologue_never_ran``, the ONLY case this
      reason is still correct for.
    """

    phase = state.prologue_phase
    if phase == PROLOGUE_COMPLETED:
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
        return

    if phase == PROLOGUE_FAILED:
        exc: BaseException = state.prologue_error or RuntimeError(
            f"turn {state.turn_id} prologue failed with no captured exception"
        )
        await settle_finalize_crash(state, update_retry_attempt=update_retry_attempt, exc=exc)
        return

    if phase == PROLOGUE_RUNNING:
        stream_audit(
            AUDIT_CANCELLED_DURING_PROLOGUE,
            session_id=state.sid,
            turn_id=state.turn_id,
            reason=REASON_CANCELLED_DURING_PROLOGUE,
        )
        await settle_finalize_crash(
            state,
            update_retry_attempt=update_retry_attempt,
            exc=TurnCancelledDuringPrologue(state.turn_id),
        )
        return

    # PROLOGUE_NOT_STARTED (the default): the prologue callable never began running.
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


__all__ = [
    "AUDIT_CANCELLED_DURING_PROLOGUE",
    "AUDIT_PROLOGUE_NEVER_RAN",
    "REASON_CANCELLED_DURING_PROLOGUE",
    "REASON_PROLOGUE_NEVER_RAN",
    "run_finalize_or_settle_prologue_gap",
    "settle_finalize_crash",
]
