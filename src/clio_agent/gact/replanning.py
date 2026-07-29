"""Stall-triggered replanning with hysteresis (P1.6d #1068, campaign #1057).

Owner module for the Magentic-One "leaky bucket" stall monitor that watches POST-APPROVAL
execution of an operator-playbook plan and, on sustained lack of progress, SURFACES a typed
replanning suggestion to the model — which then decides (⚑ RULE 1: clio never flips mode from
prose/keywords, and never forces a mode change silently). Two surfaces:

* :func:`dispatch_stall_monitor_at_finalize` — a per-turn finalize hook (fired from
  ``turn_finalize.finalize_turn``, the same seam the loop/goal composition rides). It scores a
  leaky bucket on ``session.metadata`` from TYPED, STRUCTURAL signals only — never model prose:

    - **progress** (bucket -= 1): the execution playbook's active step advanced this turn, OR the
      ``write_todos`` checklist changed (:mod:`clio_agent.gact.todos`).
    - **stall** (bucket += 1): NO progress this turn, OR a *loop* — a tool call identical
      (name + args) to one made the immediately-previous turn (the ``is_in_loop`` structural signal;
      no error-prose parsing).

  Hysteresis: a single bad turn nudges the bucket by one and a good turn decays it, so a TRANSIENT
  stall never fires; only SUSTAINED stalling (bucket >= :data:`STALL_THRESHOLD`) fires. After a
  fire, a :data:`STALL_COOLDOWN_TURNS` cooldown blocks re-firing so one rough patch cannot spam the
  model. Every scoring change emits a typed ``replan.stall_scored`` event (no silent scoring); a
  fire emits ``replan.suggested``. Re-entering plan mode RESETS the bucket (the model acted on the
  suggestion / is replanning). SCOPE: only sessions with an active execution playbook are monitored,
  so a plain session is a byte-identical no-op.

* :func:`inject_replan_suggestion` — the per-turn-input attachment (the plan-mode-reminder /
  todo-recitation pattern). When the monitor has flagged a pending suggestion, this prepends a typed
  notice to the NEXT turn's input EXACTLY ONCE (it clears the flag), telling the model it may
  re-enter plan mode to replan — it is a SUGGESTION, not a mode flip. ``turn.py`` calls it from the
  enrichment step alongside the plan-mode reminder and todo recitation.

State rides ``session.metadata`` (:data:`STALL_STATE_KEY` / :data:`REPLAN_SUGGESTION_KEY`) — the
#948 no-fifth-store projection, exactly like ``loop`` / ``plan_playbook``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.planning import recorded_execution_playbook
from clio_agent.gact.todos import recorded_todos
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: ``session.metadata`` key holding the leaky-bucket state (no fifth store — the #948 pattern).
#: An empty ``{}`` (the plan-re-entry reset tombstone) reads as a fresh bucket.
STALL_STATE_KEY = "stall_monitor"

#: ``session.metadata`` key holding a PENDING replanning suggestion the next turn injects once.
#: An empty ``{}`` reads as ABSENT (a shallow update merge cannot delete a key).
REPLAN_SUGGESTION_KEY = "replan_suggestion"

#: Leaky-bucket ceiling: the score never grows past this even under sustained stalling, so a long
#: stall cannot bank an unbounded backlog that would keep firing forever after one recovery.
STALL_CAP = 6

#: Fire the suggestion when the bucket reaches this (Magentic-One default ``max_stalls`` = 3).
STALL_THRESHOLD = 3

#: Turns to wait after a fire before the suggestion can fire again (the anti-spam cooldown). Without
#: it, every stall turn at/above threshold would re-fire — the regression-locked hysteresis property.
STALL_COOLDOWN_TURNS = 3

#: Marker heading the injected replanning-suggestion block (stable + greppable; #881 discipline).
REPLAN_SUGGESTION_MARKER = "## Replanning suggestion — sustained lack of progress detected"

#: Trace-only semantic event types (registered in ``semantic_events.SSE_TRACE_ONLY_EVENT_TYPES``):
#: bucket telemetry is governance substrate the operator queries after the fact, never a UI atom.
STALL_SCORED_EVENT = "replan.stall_scored"
REPLAN_SUGGESTED_EVENT = "replan.suggested"


def _read_state(session: Any) -> dict[str, Any]:
    """Read the leaky-bucket state dict off ``session.metadata`` (fresh ``{}`` when absent/reset)."""

    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, Mapping):
        value = metadata.get(STALL_STATE_KEY)
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _todos_signature(session: Any) -> str:
    """A structural signature of the current ``write_todos`` checklist (status + content per item).

    Any content OR status change flips the signature — the typed "checklist changed this turn"
    progress signal. Empty when no todos are recorded.
    """

    return "|".join(
        f"{t.get('status', '')}::{t.get('content', '')}" for t in recorded_todos(session)
    )


def _tool_call_signatures(tools_called: list[dict[str, Any]]) -> list[str]:
    """Structural ``name::args`` signatures of this turn's tool calls (for loop detection).

    Uses only the typed tool-row ``name`` + ``args`` (never the result/error prose), so an identical
    call repeated across turns is a purely structural ``is_in_loop`` signal.
    """

    sigs: list[str] = []
    for row in tools_called or []:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "")
        try:
            args = json.dumps(row.get("args") or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args = repr(row.get("args"))
        sigs.append(f"{name}::{args}")
    return sigs


def _reset_bucket(app: "FastAPI", sid: str, session: Any) -> None:
    """Reset the bucket + drop any pending suggestion when the session re-entered plan mode.

    Only writes when there is state to clear, so a plan session that never accumulated a bucket
    stays byte-identical (no spurious metadata churn).
    """

    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, Mapping):
        return
    if not (metadata.get(STALL_STATE_KEY) or metadata.get(REPLAN_SUGGESTION_KEY)):
        return
    app.state.sessions.update(sid, metadata_patch={STALL_STATE_KEY: {}, REPLAN_SUGGESTION_KEY: {}})
    trace.event("REPLAN", "stall bucket reset on plan re-entry for %s", sid)


def dispatch_stall_monitor_at_finalize(
    app: "FastAPI",
    *,
    session_id: str,
    turn_id: str = "",
    trace_id: str = "",
    tools_called: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Score the leaky bucket for one finalized turn; surface a replan suggestion at threshold.

    The per-turn finalize hook. NEVER raises (a monitor error must not crash a turn). Returns the
    new bucket state dict when it scored, or ``None`` when it was a no-op (no session, plan mode, or
    an unstructured session with no execution playbook — the byte-identical golden path).
    """

    try:
        return _run_stall_monitor(app, session_id, turn_id, trace_id, tools_called or [])
    except Exception:  # noqa: BLE001 - the finalize hook must never crash a turn
        logger.warning("stall monitor finalize hook error", exc_info=True)
        return None


def _run_stall_monitor(
    app: "FastAPI",
    sid: str,
    turn_id: str,
    trace_id: str,
    tools_called: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Compute + persist the leaky-bucket transition for one turn (see the module docstring)."""

    session = app.state.sessions.get(sid)
    if session is None:
        return None
    mode = str(getattr(session, "mode", "") or "")
    if mode == "plan":
        # The session is (re-)planning: reset the bucket + clear any pending suggestion. This is the
        # "bucket resets when the session actually re-enters plan mode" rule.
        _reset_bucket(app, sid, session)
        return None
    # SCOPE: only monitor a structured post-approval execution (an execution-phase playbook). A plain
    # session has no execution record -> strict no-op -> byte-identical behaviour (golden test 5).
    execution = recorded_execution_playbook(session)
    if execution is None:
        return None

    state = _read_state(session)
    prev_score = int(state.get("score", 0))
    prev_step = int(state.get("exec_step", 0))
    prev_todos_sig = str(state.get("todos_sig", ""))
    prev_calls = [str(c) for c in (state.get("last_calls") or [])]

    cur_step = int(execution.active_step)
    cur_todos_sig = _todos_signature(session)
    cur_calls = _tool_call_signatures(tools_called)

    step_advanced = cur_step > prev_step
    todos_changed = bool(cur_todos_sig) and cur_todos_sig != prev_todos_sig
    progress = step_advanced or todos_changed
    # A loop: a tool call identical (name+args) to one the immediately-previous turn made.
    looping = bool(set(cur_calls) & set(prev_calls))

    if not progress or looping:
        delta = 1
    else:
        delta = -1
    score = max(0, min(STALL_CAP, prev_score + delta))

    cooldown = int(state.get("cooldown", 0))
    fired = False
    if cooldown > 0:
        cooldown -= 1
    elif score >= STALL_THRESHOLD:
        fired = True
        cooldown = STALL_COOLDOWN_TURNS

    new_state: dict[str, Any] = {
        "score": score,
        "cooldown": cooldown,
        "exec_step": cur_step,
        "todos_sig": cur_todos_sig,
        "last_calls": cur_calls,
        "turns": int(state.get("turns", 0)) + 1,
        "fired_count": int(state.get("fired_count", 0)) + (1 if fired else 0),
    }
    patch: dict[str, Any] = {STALL_STATE_KEY: new_state}
    if fired:
        patch[REPLAN_SUGGESTION_KEY] = {"pending": True, "score": score, "turn_id": turn_id}
    app.state.sessions.update(sid, metadata_patch=patch)

    if delta != 0:
        # Every scoring change is recorded — no silent scoring (the no-silent-fallback ground rule).
        _emit_stall_event(
            app,
            sid,
            STALL_SCORED_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"stall bucket {prev_score}->{score} (delta {delta:+d})",
            payload={
                "score": score,
                "delta": delta,
                "progress": progress,
                "looping": looping,
                "cooldown": cooldown,
            },
        )
    if fired:
        trace.event(
            "REPLAN", "stall threshold reached (score=%d) — suggesting replan for %s", score, sid
        )
        _emit_stall_event(
            app,
            sid,
            REPLAN_SUGGESTED_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"sustained stall (score={score}) — replanning suggested",
            payload={"score": score, "threshold": STALL_THRESHOLD},
        )
    return new_state


def _emit_stall_event(
    app: "FastAPI",
    sid: str,
    event_type: str,
    *,
    turn_id: str,
    trace_id: str,
    summary: str,
    payload: dict[str, Any],
) -> None:
    """Emit a trace-only stall/suggestion semantic event (best-effort; never breaks scoring)."""

    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            event_type,
            turn_id=turn_id,
            trace_id=trace_id,
            status="completed",
            summary=summary,
            actor={"role": "harness"},
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry never breaks the monitor
        trace.event("REPLAN", "%s emit failed for %s: %s", event_type, sid, exc)


def _suggestion_block() -> str:
    """Compose the typed replanning-suggestion notice (a SUGGESTION — never a mode flip)."""

    return (
        REPLAN_SUGGESTION_MARKER + "\n\n"
        "Your recent turns have shown little measurable progress — no plan-step or checklist "
        "advancement, and/or the same tool call repeated across turns. Consider whether the current "
        "approach is working.\n"
        "- If it is NOT, you MAY re-enter plan mode to replan (invoke the `planning` skill) and "
        "revise your approach. This is your decision — nothing is forced.\n"
        "- If you ARE making progress, disregard this notice and continue."
    )


def inject_replan_suggestion(app: "FastAPI", sid: str, session: Any, enriched_text: str) -> str:
    """Prepend a pending replanning suggestion to this turn's input EXACTLY ONCE (P1.6d #1068).

    Returns ``enriched_text`` unchanged in plan mode (already planning) and when no suggestion is
    pending — so a session the monitor never flagged is byte-identical. When a suggestion is pending
    it clears the flag (inject-once) and prepends the typed notice, reusing the per-turn-input
    reminder mechanism (never the system prompt) so it survives compaction. It is a SUGGESTION the
    model acts on; it NEVER changes ``session.mode``.
    """

    if str(getattr(session, "mode", "") or "") == "plan":
        return enriched_text
    metadata = getattr(session, "metadata", None)
    pending = metadata.get(REPLAN_SUGGESTION_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(pending, Mapping) or not pending.get("pending"):
        return enriched_text
    app.state.sessions.update(sid, metadata_patch={REPLAN_SUGGESTION_KEY: {}})
    return _suggestion_block() + "\n\n---\n\n" + enriched_text
