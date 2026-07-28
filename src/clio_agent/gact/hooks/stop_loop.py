"""Bounded ``Stop``-hook self-loops (P2.5, hooks-research invariant 6).

The ``Stop`` event fires at the turn-finalize boundary (the ported ``post_message``
consumer). P2.5 promotes it from a pure observation to a BOUNDED completion gate: a
Stop hook that returns ``deny`` (``block``) means *"the turn is not actually done —
re-drive one more turn"* (the canonical use: a test-gate hook that blocks while the
suite is red, or a todo-gate that blocks while items are open). The re-drive rides
the EXISTING #1031 idle-hook re-drive seam — the finalize path enqueues a re-drive
into the session's :class:`~clio_agent.gact.loop_inbox.LoopInbox` and the turn-runner
idle hook (``drain_inbox_to_new_turn``) starts exactly ONE new turn when the slot
clears. No new store: the loop counters live on ``session.metadata`` (the #948
``AgentTask`` no-fifth-store projection pattern, RULE 4).

The re-entry is bounded three ways so it can NEVER run away (the release-gating
invariant):

* **per-hook ``loopLimit``** — how many times ONE hook's block may re-drive within a
  stop-sequence (R5);
* **a global cap** — a hard ceiling on total re-drives per stop-sequence
  (:data:`STOP_LOOP_GLOBAL_CAP_DEFAULT`, the ``CLAUDE_CODE_STOP_HOOK_BLOCK_CAP``
  analog), overridable via ``hooks.stop_loop_cap`` / ``CLIO_HOOKS_STOP_LOOP_CAP``;
* **``stop_hook_active``** — a flag placed in the Stop envelope payload from the 2nd
  firing onward so a hook can self-limit.

When either bound trips, the turn settles DONE (no further re-drive) and a typed
``stop_loop_cap`` reason is recorded (no-silent-fallback) — never an infinite loop,
never a silent stop. :func:`evaluate_stop_loop` is a PURE decision function
(unit-testable in isolation); :func:`run_stop_hooks` is the finalize-boundary
orchestrator that fires the hooks, persists the counters, and enqueues the re-drive.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clio_agent.gact.hooks.dispatcher import dispatch_stop, get_global_dispatcher
from clio_agent.gact.hooks.events import STOP
from clio_agent.gact.hooks.wire import HookOutcome, record_hook_reason

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Session

logger = logging.getLogger(__name__)

#: The hard global ceiling on Stop-driven re-drives within one stop-sequence — the
#: ``CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`` analog. A stop-sequence is the run of
#: consecutive Stop-blocked re-drives; it resets the moment a Stop dispatch settles
#: WITHOUT a block (the agent is genuinely done) or the cap trips.
STOP_LOOP_GLOBAL_CAP_DEFAULT = 8

#: ``session.metadata`` key holding the per-session stop-loop counters (no fifth
#: store): ``{"count": int, "per_hook": {hook_id: int}}``.
STOP_LOOP_METADATA_KEY = "stop_loop"


def global_cap() -> int:
    """Return the effective global Stop-loop cap (config-overridable, default 8).

    Read live from :mod:`clio_agent.conf` so a deployment (or a test) can lower the
    ceiling without a code change. A non-positive configured value is clamped to
    ``1`` so the bound is always meaningful (at least one re-drive, then settle).
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    value = conf.resolve(
        "hooks.stop_loop_cap",
        env="CLIO_HOOKS_STOP_LOOP_CAP",
        default=STOP_LOOP_GLOBAL_CAP_DEFAULT,
        cast=conf.as_int,
    )
    return max(int(value), 1)


def read_stop_loop_state(session: "Session | None") -> dict[str, Any]:
    """Return the stop-loop counters recorded on ``session.metadata`` (or empties)."""

    if session is None:
        return {"count": 0, "per_hook": {}}
    raw = getattr(session, "metadata", None)
    state = raw.get(STOP_LOOP_METADATA_KEY) if isinstance(raw, Mapping) else None
    if not isinstance(state, Mapping):
        return {"count": 0, "per_hook": {}}
    per_hook = state.get("per_hook")
    return {
        "count": int(state.get("count", 0) or 0),
        "per_hook": dict(per_hook) if isinstance(per_hook, Mapping) else {},
    }


@dataclass(frozen=True)
class StopLoopResult:
    """The bounded-self-loop decision for one Stop dispatch.

    ``redrive`` is the only action-bearing field: when true the finalize orchestrator
    enqueues ONE re-drive turn carrying ``reason``/``additional_context`` as the
    "why you are not done yet" feedback. ``capped`` marks a bounded TERMINATION (the
    global ceiling or every blocker's per-hook ``loopLimit`` was reached), which the
    orchestrator records as a typed ``stop_loop_cap`` reason. ``new_state`` is the
    counter snapshot to persist back onto ``session.metadata``.
    """

    redrive: bool = False
    reason: str = ""
    additional_context: str = ""
    capped: bool = False
    cap_scope: str = ""  # "" | "global" | "per_hook"
    blocking_hook_ids: tuple[str, ...] = ()
    new_state: dict[str, Any] = field(default_factory=lambda: {"count": 0, "per_hook": {}})
    #: P2.6: a Stop hook returned ``defer`` — the turn SUSPENDED (waiting_user) for an
    #: out-of-band approval instead of re-driving or settling. Distinct from ``redrive``
    #: (which continues immediately) and ``capped`` (bounded stop). The pending approval
    #: id is recorded on ``deferred_permission_id`` when a session was available to park.
    deferred: bool = False
    deferred_permission_id: str = ""


def _blocking_hook_ids(outcome: HookOutcome) -> tuple[str, ...]:
    """Return the ids of the hooks that returned a ``deny`` (block) this dispatch."""

    return tuple(
        str(record.get("hook_id") or "")
        for record in outcome.records
        if record.get("decision") == "deny" and record.get("hook_id")
    )


def evaluate_stop_loop(
    outcome: HookOutcome,
    prior_state: Mapping[str, Any],
    *,
    loop_limits: Mapping[str, int],
    cap: int,
) -> StopLoopResult:
    """Decide re-drive vs. settle for one Stop dispatch — PURE (no I/O).

    ``prior_state`` is the persisted ``{"count", "per_hook"}`` snapshot; ``count`` is
    the number of re-drives already performed in this stop-sequence and ``per_hook``
    the per-hook honored-block tally. ``loop_limits`` maps each Stop hook's id to its
    ``loopLimit`` (``0`` ⇒ bounded only by the global ``cap``). Rules:

    * No hook blocked ⇒ the agent is DONE; reset the sequence, no re-drive.
    * A hook blocked but the global ``count`` already reached ``cap`` ⇒ bounded stop
      (``cap_scope="global"``), settle done.
    * A hook blocked but EVERY blocker already hit its per-hook ``loopLimit`` ⇒
      bounded stop (``cap_scope="per_hook"``), settle done.
    * Otherwise ⇒ re-drive: bump the global count and each still-eligible blocker's
      tally.
    """

    count = int(prior_state.get("count", 0) or 0)
    per_hook_raw = prior_state.get("per_hook", {})
    per_hook: dict[str, int] = {
        str(k): int(v or 0) for k, v in (per_hook_raw or {}).items()
    }
    blocking = _blocking_hook_ids(outcome)

    if not outcome.denied or not blocking:
        # The agent settled without a Stop block: the stop-sequence is complete.
        return StopLoopResult(new_state={"count": 0, "per_hook": {}})

    def _limit_for(hook_id: str) -> int:
        limit = int(loop_limits.get(hook_id, 0) or 0)
        # A 0/negative per-hook limit means "bounded only by the global cap".
        return limit if limit > 0 else cap

    eligible = tuple(h for h in blocking if per_hook.get(h, 0) < _limit_for(h))

    if count >= cap:
        return StopLoopResult(
            reason=outcome.reason,
            additional_context=outcome.additional_context,
            capped=True,
            cap_scope="global",
            blocking_hook_ids=blocking,
            new_state={"count": 0, "per_hook": {}},
        )
    if not eligible:
        # Every hook that wants to block has exhausted its own loopLimit — a bounded
        # per-hook stop (distinct from the global ceiling; both settle done).
        return StopLoopResult(
            reason=outcome.reason,
            additional_context=outcome.additional_context,
            capped=True,
            cap_scope="per_hook",
            blocking_hook_ids=blocking,
            new_state={"count": 0, "per_hook": {}},
        )

    new_per_hook = dict(per_hook)
    for hook_id in eligible:
        new_per_hook[hook_id] = new_per_hook.get(hook_id, 0) + 1
    return StopLoopResult(
        redrive=True,
        reason=outcome.reason,
        additional_context=outcome.additional_context,
        blocking_hook_ids=blocking,
        new_state={"count": count + 1, "per_hook": new_per_hook},
    )


def _loop_limits() -> dict[str, int]:
    """Build the ``{hook_id: loopLimit}`` map from the installed dispatcher."""

    dispatcher = get_global_dispatcher()
    if dispatcher is None:
        return {}
    return {entry.id: entry.loop_limit for entry in dispatcher.entries}


def _enqueue_redrive(app: "FastAPI", session_id: str, result: StopLoopResult) -> None:
    """Enqueue ONE Stop-loop re-drive onto the session's loop-inbox (#1031 seam).

    The turn-runner idle hook drains it into exactly one new turn when the current
    turn's slot clears. The re-drive is marked ``stop_loop_redrive`` in metadata so
    it is identifiable on the trace (never a silent synthetic user turn), and its
    text is the Stop hook's block reason — the "why you are not done" feedback the
    completion gate is meant to carry back to the model.
    """

    from clio_agent.gact.loop_inbox import InboxEvent, inbox_for  # noqa: PLC0415

    text = (result.reason or "").strip()
    if result.additional_context.strip():
        text = f"{text}\n\n{result.additional_context.strip()}" if text else result.additional_context.strip()
    if not text:
        text = "A Stop hook reported the task is not complete; continue working."
    inbox_for(app, session_id).put(
        InboxEvent(
            kind="user_message",
            task_id="",
            text=text,
            metadata={
                "stop_loop_redrive": True,
                "stop_loop_count": int(result.new_state.get("count", 0) or 0),
                "stop_loop_blocking_hooks": list(result.blocking_hook_ids),
            },
        )
    )


def run_stop_hooks(
    app: "FastAPI",
    *,
    session_id: str,
    turn_id: str,
    cwd: str,
    payload: Mapping[str, Any],
) -> StopLoopResult:
    """Fire ``Stop`` hooks at the finalize boundary and apply the bounded self-loop.

    Reads the per-session counters, stamps ``stop_hook_active`` into the envelope
    payload from the 2nd firing on, dispatches the Stop hooks ONCE, evaluates the
    bounded decision, persists the updated counters back to ``session.metadata``, and
    — when the decision is to re-drive — enqueues one re-drive turn on the #1031
    idle-hook seam. A cap-trip records the typed ``stop_loop_cap`` reason. Returns the
    :class:`StopLoopResult` (the caller may emit observability from it).
    """

    session = app.state.sessions.get(session_id) if session_id else None
    prior = read_stop_loop_state(session)
    prior_count = int(prior.get("count", 0) or 0)

    envelope_payload: dict[str, Any] = dict(payload)
    if prior_count > 0:
        # Present ONLY from the 2nd firing onward (Claude Code's contract) so a hook
        # can tell it is re-entering and self-limit.
        envelope_payload["stop_hook_active"] = True

    outcome = dispatch_stop(
        envelope_payload,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
    )
    # P2.6: a Stop hook ``defer`` SUSPENDS the turn for out-of-band approval (a
    # turn-ending yield) rather than re-driving or settling. deny beats defer in the
    # merge, so a defer here means no hook denied. Ride the #1031 deferred-resume: park
    # a pending approval + flip the session to waiting_user; on approve the session
    # releases, on deny it re-drives one more turn (see gact/hooks/defer.py).
    if outcome.is_defer:
        from clio_agent.gact.hooks.defer import suspend_turn_defer  # noqa: PLC0415

        feedback = (outcome.reason or "").strip()
        if outcome.additional_context.strip():
            feedback = (
                f"{feedback}\n\n{outcome.additional_context.strip()}"
                if feedback
                else outcome.additional_context.strip()
            )
        pid = suspend_turn_defer(
            app,
            sid=session_id,
            hook_event=STOP,
            reason=outcome.reason or "A Stop hook deferred completion for approval.",
            resume_text=feedback or "A Stop hook reported the task is not complete; continue working.",
            prev_status="running",
        )
        # A defer leaves the stop-sequence counters untouched (it neither redrove nor
        # settled); persist the prior state so a post-approval re-drive still counts.
        return StopLoopResult(
            deferred=True,
            deferred_permission_id=pid or "",
            reason=outcome.reason,
            additional_context=outcome.additional_context,
            new_state=prior,
        )
    result = evaluate_stop_loop(
        outcome,
        prior,
        loop_limits=_loop_limits(),
        cap=global_cap(),
    )

    # Persist the new counters (no fifth store: session.metadata, #948 pattern). Only
    # when we actually have a session record to update.
    if session is not None:
        app.state.sessions.update(
            session_id, metadata_patch={STOP_LOOP_METADATA_KEY: result.new_state}
        )

    if result.capped:
        record_hook_reason(
            "stop_loop_cap",
            event=STOP,
            session_id=session_id,
            turn_id=turn_id,
            scope=result.cap_scope,
            prior_count=prior_count,
            hook_ids=list(result.blocking_hook_ids),
        )
    elif result.redrive:
        _enqueue_redrive(app, session_id, result)

    return result


def dispatch_stop_at_finalize(
    app: "FastAPI",
    *,
    session_id: str,
    turn_id: str,
    trace_id: str,
    cwd: str,
    assistant_msg_id: str,
    assistant_payload: Mapping[str, Any],
    blueprint_id: str,
) -> "StopLoopResult | None":
    """Fire the bounded Stop gate at the turn-finalize boundary + emit observability.

    The whole Stop-hook finalize protocol lives HERE (the hooks owner module), not
    inlined into ``turn_finalize`` (no-accretion): emit the ``hook.invocation.started``
    span, run the bounded self-loop (:func:`run_stop_hooks`), emit a
    ``hook.stop_loop.redrive`` or ``.capped`` span for the decision, then the
    ``completed`` span. A dispatch error is swallowed (the post-turn contract) and
    surfaced as a typed ``hook.invocation.failed`` span — never fatal to the settled
    turn. Returns the :class:`StopLoopResult`, or ``None`` if the dispatch failed.
    """

    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    try:
        _emit_semantic_event(
            app,
            session_id,
            "hook.invocation.started",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary="Stop hook dispatch started.",
            actor={"hook": "Stop"},
            subject={"message_id": assistant_msg_id},
            payload={"assistant": dict(assistant_payload)},
        )
        result = run_stop_hooks(
            app,
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            payload={"assistant": dict(assistant_payload), "blueprint_id": blueprint_id},
        )
        if result.redrive:
            _emit_semantic_event(
                app,
                session_id,
                "hook.stop_loop.redrive",
                turn_id=turn_id,
                trace_id=trace_id,
                summary="Stop hook blocked — re-driving one more turn (bounded).",
                actor={"hook": "Stop"},
                subject={"message_id": assistant_msg_id},
                payload={
                    "count": int(result.new_state.get("count", 0) or 0),
                    "blocking_hooks": list(result.blocking_hook_ids),
                },
            )
        elif result.deferred:
            _emit_semantic_event(
                app,
                session_id,
                "hook.stop_loop.deferred",
                turn_id=turn_id,
                trace_id=trace_id,
                status="waiting_user",
                summary="Stop hook deferred completion — turn suspended for out-of-band approval.",
                actor={"hook": "Stop"},
                subject={"message_id": assistant_msg_id},
                payload={"permission_id": result.deferred_permission_id},
            )
        elif result.capped:
            _emit_semantic_event(
                app,
                session_id,
                "hook.stop_loop.capped",
                turn_id=turn_id,
                trace_id=trace_id,
                summary="Stop-loop cap tripped — turn settled done (stop_loop_cap).",
                actor={"hook": "Stop"},
                subject={"message_id": assistant_msg_id},
                payload={
                    "scope": result.cap_scope,
                    "blocking_hooks": list(result.blocking_hook_ids),
                },
            )
        _emit_semantic_event(
            app,
            session_id,
            "hook.invocation.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary="Stop hook dispatch completed.",
            actor={"hook": "Stop"},
            subject={"message_id": assistant_msg_id},
            payload={},
        )
        return result
    except Exception:  # noqa: BLE001 - post-turn contract: a Stop dispatch never breaks a settled turn
        _emit_semantic_event(
            app,
            session_id,
            "hook.invocation.failed",
            turn_id=turn_id,
            trace_id=trace_id,
            status="failed",
            summary="Stop hook dispatch failed and was swallowed by policy.",
            actor={"hook": "Stop"},
            subject={"message_id": assistant_msg_id},
            payload={},
        )
        return None
