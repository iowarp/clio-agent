"""V2 event highway + ARC live-plane emission for ``_RetainingReActV2`` (#901 S6).

The ReActV2 append-only loop (``dspy.ReActV2.forward``) is clean but *silent*: it
neither writes the ARC live-context plane nor puts its per-step trajectory on the
semantic-event highway. Both are part of clio's externally-frozen wire/trace
contract (the gact-tui tree view renders ``react.step.completed`` +
``expert.lifecycle.started`` / ``expert.extract.completed`` + ``arc.op`` records),
and the ARC live-plane writes are ALSO what makes V2's own next-iteration History
correct on ops (the S2 fold reads the materialized plane this loop populates).

This module owns the *instrumented* V2 forward loop: it mirrors
``dspy.predict.react_v2.ReActV2.forward`` verbatim (so the append-only History
composition is byte-identical to stock) and interleaves, at the same seams the
classic ``_RetainingReAct.forward`` uses:

* the ARC live-plane writes (``thought`` / ``tool_call`` / ``observation`` segments,
  span-correlated) — the SAME shape the classic loop writes, so replay + fold hold;
* the per-step / per-expert highway events (``_emit_react_step_event`` /
  ``_emit_expert_lifecycle_event``, imported from the SAME
  :mod:`clio_agent.gact.runtime.globals` funnel the classic path uses);
* the proactive auto-compaction TRIGGER (``agent._maybe_autocompact()``) fired
  BEFORE every ``react`` call — the compaction ACTION stays the ARC summarize op
  (V2 has no ``truncate_trajectory``; an ARC op is the sole prefix-reset author).

Kept in a sibling module (not appended to ``reactv2.py``) so the owner module stays
under its 800-line cap (no-accretion rule). One-directional import: this module
imports dspy + the shared runtime funnel + ``gact.context`` only, never
``reactv2.py`` — ``reactv2.py`` imports THIS.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from dspy.predict.react_v2 import (
    _append_history_event,
    _coerce_history,
    _coerce_tool_calls,
    _ensure_tool_call_ids,
)
from dspy.primitives.prediction import Prediction
from dspy.utils.exceptions import AdapterParseError, ContextWindowExceededError

from clio_agent.gact.runtime.context_tokens import _arc_obs_value
from clio_agent.gact.runtime.globals import (
    _active_lm_last_reasoning,
    _active_semantic_turn_id,
    _emit_expert_lifecycle_event,
    _emit_react_step_event,
    _jsonish,
)

logger = logging.getLogger(__name__)


def _arc_scope() -> tuple[Any, str, str]:
    """Resolve ``(ARCMemory, session_id, scope)`` for the live plane, or ``(None, '', '')``.

    The V2 analog of the classic ``_RetainingReAct._arc_scope``: reads the app handle
    + react scope/session from the runtime context. ``arc`` is ``None`` (a no-op live
    plane) whenever ARC is disabled or no react scope is active.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    scope = _ctx.active_react_scope()
    session = _ctx.active_react_session()
    arc = (
        getattr(getattr(app, "state", None), "arc", None) if (app is not None and scope) else None
    )
    return arc, session, scope


def _arc_write(
    arc: Any,
    session: str,
    scope: str,
    kind: str,
    content: dict[str, Any],
    idx: int,
    *,
    turn_id: str = "",
    expert_span_id: str = "",
    run_span_id: str = "",
) -> None:
    """Append one produced piece to the live plane, stamping the correlation span ids.

    Byte-identical to the classic ``_RetainingReAct._arc_write`` (same ~4-chars/token
    heuristic, same best-effort contract): a write failure must never break the turn.
    """
    if arc is None:
        return
    try:
        import json as _json  # noqa: PLC0415

        tok = max(1, len(_json.dumps(content, default=str)) // 4)
        arc.append_segment(
            session,
            scope,
            kind,
            content,
            step=idx,
            token_count=tok,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "arc live-plane append failed kind=%s scope=%s", kind, scope, exc_info=True
        )


def _reset_working_set(arc: Any, session: str, scope: str) -> None:
    """Tombstone any prior live WORKING-SET segments in the scope (a new forward == a
    new turn's trajectory), mirroring the classic forward's reset. Best-effort."""
    if arc is None:
        return
    try:
        prior = [s.id for s in arc.render_working_set(session, scope)]
        if prior:
            arc.delete_segments(session, scope, prior)
    except Exception:  # noqa: BLE001
        logger.warning("arc live-plane reset failed scope=%s", scope, exc_info=True)


def _emit_turn(
    arc: Any,
    session: str,
    scope: str,
    *,
    turn_index: int,
    thought: Any,
    reasoning: Any,
    tool_calls: Any,
    tool_call_results: Any,
    expert_id: str,
    expert_span_id: str,
    step_span_id: str,
    turn_id: str,
) -> None:
    """Write the turn's ARC segments and put the ReAct step on the highway.

    Mirrors the classic per-step block: a ``thought`` segment, then a
    ``tool_call`` + ``observation`` segment per executed tool call, and ONE
    ``react.step.completed`` highway event keyed on the primary (first) tool call
    (``is_finish`` when it is the reserved ``submit``). Multiple tool calls in one
    turn each get their own ARC segments (no data loss); the fold re-indexes them.
    """
    calls = list(tool_calls.tool_calls)
    results_by_id = {
        r.call_id: r for r in tool_call_results.tool_call_results if r.call_id is not None
    }
    _arc_write(
        arc,
        session,
        scope,
        "thought",
        {"text": thought},
        turn_index,
        turn_id=turn_id,
        expert_span_id=expert_span_id,
        run_span_id=step_span_id,
    )
    for call in calls:
        _arc_write(
            arc,
            session,
            scope,
            "tool_call",
            {"name": call.name, "args": dict(call.args or {})},
            turn_index,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=step_span_id,
        )
        result = results_by_id.get(call.id)
        obs = result.value if result is not None else ""
        _arc_write(
            arc,
            session,
            scope,
            "observation",
            {"text": _arc_obs_value(obs)},
            turn_index,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=step_span_id,
        )

    primary = calls[0] if calls else None
    primary_result = results_by_id.get(primary.id) if primary is not None else None
    _emit_react_step_event(
        expert_id=expert_id,
        expert_span_id=expert_span_id,
        step_span_id=step_span_id,
        step_index=turn_index,
        thought=thought,
        reasoning=reasoning,
        tool_name=(primary.name if primary is not None else ""),
        tool_args=(dict(primary.args or {}) if primary is not None else {}),
        observation=(primary_result.value if primary_result is not None else ""),
        is_finish=(primary is not None and primary.name == "submit"),
    )


def _final_answer_text(final_outputs: dict[str, Any] | None) -> str:
    """The submit turn's visible-lane answer for the expert.extract.completed payload."""
    if not final_outputs:
        return ""
    return str(final_outputs.get("answer", "") or "")


def _structured_metadata(final_outputs: dict[str, Any] | None) -> dict[str, Any]:
    """The non-answer submit output fields for the expert.extract.completed payload."""
    if not final_outputs:
        return {}
    return {
        key: value
        for key, value in final_outputs.items()
        if key != "answer" and value not in (None, "")
    }


def instrumented_forward(agent: Any, **input_args: Any) -> Prediction:
    """Run the ReActV2 append-only loop with clio's ARC + highway + autocompact seams.

    A verbatim mirror of ``dspy.predict.react_v2.ReActV2.forward`` (the append-only
    History composition is unchanged) that additionally: resets the ARC working set
    for the new turn, opens/closes the expert lifecycle on the highway, and — per
    turn — fires the auto-compaction trigger BEFORE the ``react`` call, sets the
    step-thought context for the tool observer, and writes the ARC live plane + emits
    the ``react.step.completed`` highway event AFTER the tool calls execute.

    Every seam is best-effort/no-op when its dependency is absent (ARC disabled, no
    active app/session), so the loop runs identically in a bare unit test.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    max_iters = input_args.pop("max_iters", agent.max_iters)
    history = _coerce_history(input_args.pop("history", None))
    pending_inputs = {
        name: input_args[name] for name in agent.signature.input_fields if name in input_args
    }

    arc, session, scope = _arc_scope()
    _reset_working_set(arc, session, scope)

    expert_id = str(getattr(agent, "_clio_expert_id", "") or "")
    turn_id = _active_semantic_turn_id()
    expert_span_id = uuid.uuid4().hex[:16]
    _emit_expert_lifecycle_event(
        "expert.lifecycle.started",
        expert_id=expert_id,
        expert_span_id=expert_span_id,
        status="running",
        payload={"input": _jsonish(dict(pending_inputs))},
    )
    parent_token = _ctx.set_parent_span(expert_span_id)
    break_reason = "max_iters"
    try:
        for turn_index in range(max_iters):
            step_span_id = uuid.uuid4().hex[:16]
            step_token = _ctx.set_parent_span(step_span_id)
            thought_token = None
            try:
                agent._maybe_autocompact()  # proactive compaction BEFORE the send
                try:
                    pred = agent.react(
                        history=history,
                        tools=list(agent.tools.values()),
                        **pending_inputs,
                    )
                    tool_calls = _coerce_tool_calls(getattr(pred, "tool_calls", None))
                except (AdapterParseError, ValueError):
                    break_reason = "parse_error"
                    break
                except ContextWindowExceededError:
                    break_reason = "context_window_exceeded"
                    break

                if not tool_calls.tool_calls:
                    break_reason = "empty_tool_calls"
                    break

                tool_calls = _ensure_tool_call_ids(tool_calls, turn_index)
                thought = getattr(pred, "next_thought", "")
                reasoning = _active_lm_last_reasoning()
                thought_token = _ctx.set_step_thought(str(thought or ""), str(reasoning or ""))
                tool_call_results, final_outputs = agent._execute_tool_calls(tool_calls)
                event = agent._history_event(pending_inputs, pred, tool_calls, tool_call_results)
                if final_outputs is not None:
                    event.update(final_outputs)
                _append_history_event(history, event)
                pending_inputs = {}

                _emit_turn(
                    arc,
                    session,
                    scope,
                    turn_index=turn_index,
                    thought=thought,
                    reasoning=reasoning,
                    tool_calls=tool_calls,
                    tool_call_results=tool_call_results,
                    expert_id=expert_id,
                    expert_span_id=expert_span_id,
                    step_span_id=step_span_id,
                    turn_id=turn_id,
                )

                if final_outputs is not None:
                    _emit_expert_completed(expert_id, expert_span_id, final_outputs, turn_index + 1)
                    return Prediction(
                        **final_outputs, history=history, termination_reason="submit"
                    )
            finally:
                if thought_token is not None:
                    _ctx.reset(thought_token)
                _ctx.reset(step_token)

        result = agent._forced_submit(history, pending_inputs, break_reason, max_iters)
        produced = set(result.keys()) if hasattr(result, "keys") else set()
        _emit_expert_completed(
            expert_id,
            expert_span_id,
            {k: getattr(result, k) for k in agent.signature.output_fields if k in produced},
            max_iters,
        )
        return result
    finally:
        _ctx.reset(parent_token)


def _emit_expert_completed(
    expert_id: str,
    expert_span_id: str,
    final_outputs: dict[str, Any] | None,
    step_count: int,
) -> None:
    """Close the expert lifecycle on the highway (the V2 analog of the classic
    ``expert.extract.completed``), carrying the final answer + structured outputs.

    Emits the SAME event type the classic path does so the frozen wire/trace contract
    (gact-tui tree view) sees an identical event stream shape.
    """
    _emit_expert_lifecycle_event(
        "expert.extract.completed",
        expert_id=expert_id,
        expert_span_id=expert_span_id,
        status="completed",
        payload={
            "output": _final_answer_text(final_outputs),
            "structured": _structured_metadata(final_outputs),
            "step_count": step_count,
        },
    )
