"""The ``UserPromptSubmit`` turn-boundary hook protocol (P2.2 port + P2.6 defer).

Owns the whole finalize-boundary protocol for the turn's prompt hook (mirrors
``stop_loop.dispatch_stop_at_finalize`` for ``Stop``), so ``turn.py`` carries only a
one-line call site (no-accretion; the block used to be ~95 inline lines):

* **deny** VETOES the turn — the session settles to ``error`` (the ported
  ``pre_message`` behaviour, byte-for-byte).
* **defer** (P2.6) SUSPENDS the turn for out-of-band approval — a turn-ending yield,
  so it rides the #1031 deferred-resume: :func:`~clio_agent.gact.hooks.defer.suspend_turn_defer`
  persists a resolvable pending approval + flips the session to ``waiting_user``; an
  out-of-band ``allow`` re-drives the original prompt as a NEW turn (carrying the
  resume once-gate so the hook does not re-defer the just-approved prompt), a ``deny``
  rejects it.
* otherwise the turn PROCEEDS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact.events import Event
from clio_agent.gact.hooks.defer import HOOK_DEFER_RESUME_META, suspend_turn_defer
from clio_agent.gact.runtime.globals import _emit_semantic_event

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState


def run_user_prompt_submit(
    state: "TurnState",
    *,
    update_retry_attempt: Callable[..., Any],
) -> str:
    """Fire ``UserPromptSubmit`` hooks at the prompt boundary. Returns the turn action.

    Returns ``"proceed"`` (run the turn), ``"blocked"`` (a deny vetoed the turn; the
    session settled to ``error``), or ``"deferred"`` (a hook parked the turn for
    out-of-band approval; the session is ``waiting_user``). The caller returns from the
    turn on anything but ``"proceed"``.
    """

    from clio_agent.gact.hooks import dispatch_user_prompt_submit  # noqa: PLC0415

    # Resume once-gate (P2.6): a turn resumed from an approved UserPromptSubmit defer
    # carries HOOK_DEFER_RESUME_META so the SAME hook does not re-defer the prompt it
    # just approved (analogous to plan_exit's constraint-lift). Skip the dispatch.
    if bool((getattr(state.user_msg, "metadata", None) or {}).get(HOOK_DEFER_RESUME_META)):
        return "proceed"

    _emit_semantic_event(
        state.app,
        state.sid,
        "hook.invocation.started",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="running",
        summary="UserPromptSubmit hook dispatch started.",
        actor={"hook": "UserPromptSubmit"},
        subject={"message_id": state.user_msg.id},
        payload={"input": state.enriched_text},
    )
    try:
        outcome = dispatch_user_prompt_submit(
            state.enriched_text,
            session_id=state.sid,
            turn_id=state.turn_id,
            cwd=str(getattr(state.sess, "workspace_root", "") or ""),
        )
        if outcome.is_defer:
            return _suspend_for_defer(state, outcome)
        if outcome.denied:
            raise PermissionError(outcome.reason or "blocked by a UserPromptSubmit hook")
        _emit_semantic_event(
            state.app,
            state.sid,
            "hook.invocation.completed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            summary="UserPromptSubmit hook dispatch completed.",
            actor={"hook": "UserPromptSubmit"},
            subject={"message_id": state.user_msg.id},
            payload={},
        )
        return "proceed"
    except PermissionError as exc:
        _settle_blocked(state, exc, update_retry_attempt=update_retry_attempt)
        return "blocked"


def _suspend_for_defer(state: "TurnState", outcome: Any) -> str:
    """Suspend the turn for a deferred UserPromptSubmit approval (P2.6).

    Persists a resolvable pending approval + flips the session to ``waiting_user`` via
    the defer owner module, finalizes this turn's context frame (no transcript is open
    yet — the pause is BEFORE the model runs), and emits the deferred span. A missing
    session cannot happen for a live turn; if it did, the fail-safe is to PROCEED (never
    silently drop the user's prompt).
    """

    pid = suspend_turn_defer(
        state.app,
        sid=state.sid,
        hook_event="UserPromptSubmit",
        reason=str(getattr(outcome, "reason", "") or ""),
        resume_text=state.enriched_text,
        prev_status="running",
    )
    if pid is None:
        return "proceed"
    _emit_semantic_event(
        state.app,
        state.sid,
        "hook.invocation.deferred",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="waiting_user",
        summary="UserPromptSubmit hook deferred the turn for out-of-band approval.",
        actor={"hook": "UserPromptSubmit"},
        subject={"message_id": state.user_msg.id, "permission_id": pid},
        payload={"permission_id": pid, "reason": str(getattr(outcome, "reason", "") or "")},
    )
    frame = getattr(state, "context_frame", None)
    if isinstance(frame, dict) and frame.get("id"):
        from clio_agent.gact.enrichment import _finalize_context_frame  # noqa: PLC0415

        _finalize_context_frame(
            state.app, state.sid, frame["id"], "", "completed", error_info=None
        )
    return "deferred"


def _settle_blocked(
    state: "TurnState", exc: Exception, *, update_retry_attempt: Callable[..., Any]
) -> None:
    """Settle a UserPromptSubmit VETO — the session goes to ``error`` (ported verbatim)."""

    _emit_semantic_event(
        state.app,
        state.sid,
        "hook.invocation.blocked",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="blocked",
        summary="UserPromptSubmit hook blocked the turn.",
        actor={"hook": "UserPromptSubmit"},
        subject={"message_id": state.user_msg.id},
        payload={"error": str(exc)},
    )
    _emit_semantic_event(
        state.app,
        state.sid,
        "turn.failed",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="blocked",
        summary="CLIO turn was blocked by a UserPromptSubmit hook.",
        actor={"hook": "UserPromptSubmit"},
        subject={"message_id": state.user_msg.id},
        payload={"error": str(exc)},
    )
    state.bus.publish(
        Event(
            type="message.completed",
            session_id=state.sid,
            payload={
                "turn_id": state.turn_id,
                "message_id": state.user_msg.id,
                "stop_reason": "blocked",
                "error_info": {
                    "error": "permission_error",
                    "message": str(exc),
                    "recoverable": True,
                },
            },
        )
    )
    state.app.state.sessions.update(state.sid, status="error")
    update_retry_attempt(
        "failed",
        metadata_patch={
            "execution_error": "permission_error",
            "executed_user_message_id": state.user_msg.id,
        },
    )
    state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=state.sid,
            payload={
                "session_id": state.sid,
                "status": "error",
                "prev_status": "running",
                "reason": "pre_message hook blocked turn",
            },
        )
    )
