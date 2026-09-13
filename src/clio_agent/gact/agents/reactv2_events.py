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

import itertools
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from dspy.adapters.types.tool import ToolCallResults, ToolCalls
from dspy.predict.react_v2 import (
    _append_history_event,
    _coerce_history,
    _coerce_tool_calls,
    _ensure_tool_call_ids,
)
from dspy.primitives.prediction import Prediction
from dspy.utils.exceptions import AdapterParseError, ContextWindowExceededError

from clio_agent.arc.working_set_fold import emit_step_open
from clio_agent.errors import ClioError
from clio_agent.gact.runtime.context_tokens import _arc_obs_value
from clio_agent.gact.runtime.globals import (
    _active_lm_last_reasoning,
    _active_semantic_turn_id,
    _emit_expert_lifecycle_event,
    _emit_react_step_event,
)
from clio_agent.tools.mcp_runtime import wire_value

logger = logging.getLogger(__name__)

# Tools whose successful call is itself the terminal outcome of the current
# model turn.  Their authoritative payload remains in session metadata until
# the post-forward pause seam mints the user-facing interaction.
_TURN_YIELD_METADATA: dict[str, str] = {
    "ask_user": "pending_ask_user",
    "plan_exit": "pending_plan_exit",
}


def _pending_turn_yield_name(tool_calls: ToolCalls) -> str:
    """Return the successful turn-ending tool in this batch, if one is pending.

    The tool name alone is insufficient: a rejected ``plan_exit`` or malformed
    ``ask_user`` call must remain an ordinary tool error.  The corresponding
    un-surfaced session-metadata record is the proof that the tool completed its
    mutation and the outer turn should now yield to the user.
    """

    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    names = {str(call.name or "") for call in tool_calls.tool_calls}
    candidates = names.intersection(_TURN_YIELD_METADATA)
    if not candidates:
        return ""
    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, Mapping):
        return ""
    for tool_name, metadata_key in _TURN_YIELD_METADATA.items():
        pending = metadata.get(metadata_key)
        if (
            tool_name in candidates
            and isinstance(pending, Mapping)
            and pending
            and not pending.get("surfaced")
        ):
            return tool_name
    return ""


def is_turn_yield_prediction(prediction: Any) -> bool:
    """Return whether a ReAct prediction intentionally yielded for user input."""

    return str(getattr(prediction, "termination_reason", "") or "").endswith("_yield")


def _arc_scope() -> tuple[Any, str, str]:
    """Resolve ``(ARCMemory, session_id, scope)`` for the live plane, or ``(None, '', '')``.

    The V2 analog of the classic ``_RetainingReAct._arc_scope``: reads the app handle
    + react scope/session from the runtime context. ``arc`` is ``None`` (a no-op live
    plane) whenever ARC is disabled or no react scope is active.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    # Fold the in-process variant try index into the ARC KEY only (#953): N tries of
    # one module in one session must not share a live-plane partition. Bare off-variant
    # (react_run == -1). Attribution readers use active_react_scope() unchanged.
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    session = _ctx.active_react_session()
    arc = getattr(getattr(app, "state", None), "arc", None) if (app is not None and scope) else None
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
        logger.warning("arc live-plane append failed kind=%s scope=%s", kind, scope, exc_info=True)


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
        payload={"input": wire_value(dict(pending_inputs), mode="gact_runtime")},
    )
    parent_token = _ctx.set_parent_span(expert_span_id)
    break_reason = "max_iters"
    # #1226 D1b: max_iters <= 0 is UNLIMITED, not an error and not a silent
    # 0-iteration loop -- the standing ruling is 0/unlimited by default; a
    # cap survives only as an explicit, blueprint-declared opt-in runaway
    # backstop (see ``_tool_user_agent_max_iters``). itertools.count() never
    # exhausts, so the loop below can only end via an explicit break (parse
    # error / context window) or a direct-response/submit return -- never
    # by "running out of turns" mid-task.
    turn_indices = range(max_iters) if max_iters > 0 else itertools.count()
    # Both iterables always yield at least once (range(max_iters) for
    # max_iters > 0; itertools.count() is infinite) -- pre-bound only so a
    # static checker can see `turn_index` is never actually unbound below.
    # step_span_id/thought are ALSO pre-bound (#1282 F6) so the escalation
    # except branch below can reference "whatever the last iteration reached"
    # even when an exception fires before either is (re)assigned this turn.
    turn_index = -1
    step_span_id = ""
    thought: Any = ""
    try:
        for turn_index in turn_indices:
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
                    thought = getattr(pred, "next_thought", "")
                    reasoning = _active_lm_last_reasoning()
                    event = agent._history_event(
                        pending_inputs,
                        pred,
                        tool_calls,
                        ToolCallResults(tool_call_results=[]),
                    )
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
                        tool_call_results=ToolCallResults(tool_call_results=[]),
                        expert_id=expert_id,
                        expert_span_id=expert_span_id,
                        step_span_id=step_span_id,
                        turn_id=turn_id,
                    )
                    answer = str(thought or "")
                    outputs = {"answer": answer}
                    _emit_expert_completed(
                        expert_id,
                        expert_span_id,
                        outputs,
                        turn_index + 1,
                    )
                    return Prediction(
                        **outputs,
                        history=history,
                        termination_reason="direct_response",
                    )

                break_reason = "max_iters"
                tool_calls = _ensure_tool_call_ids(tool_calls, turn_index)
                thought = getattr(pred, "next_thought", "")
                reasoning = _active_lm_last_reasoning()
                thought_token = _ctx.set_step_thought(str(thought or ""), str(reasoning or ""))
                # Pre-execution breadcrumb (caveat b): under the working-set fold this
                # lands the step's opening atoms on the canonical log BEFORE the tools
                # run, so a crash mid-step still leaves them. A no-op when not folding;
                # excluded from every render, so it never perturbs the working set.
                emit_step_open(
                    arc,
                    session,
                    scope,
                    {
                        "thought": str(thought or ""),
                        "tools": [c.name for c in tool_calls.tool_calls],
                    },
                    step=turn_index,
                    turn_id=turn_id,
                    expert_span_id=expert_span_id,
                    run_span_id=step_span_id,
                )
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

                # ``ask_user`` and ``plan_exit`` are runtime turn boundaries, not
                # prompt advice.  Keep the completed call/result in History and on
                # the semantic highway, then return before another LM iteration.
                # The post-forward pause seam consumes the authoritative metadata
                # and changes the session to ``waiting_user``.
                if yield_name := _pending_turn_yield_name(tool_calls):
                    return Prediction(
                        history=history,
                        termination_reason=f"{yield_name}_yield",
                    )

                if final_outputs is not None:
                    _emit_expert_completed(expert_id, expert_span_id, final_outputs, turn_index + 1)
                    return Prediction(**final_outputs, history=history, termination_reason="submit")
            finally:
                if thought_token is not None:
                    _ctx.reset(thought_token)
                _ctx.reset(step_token)

        step_count = max_iters if max_iters > 0 else turn_index + 1
        _emit_expert_completed(
            expert_id,
            expert_span_id,
            {},
            step_count,
        )
        return Prediction(history=history, termination_reason=break_reason)
    except ClioError as exc:
        # #1282 F6 (#1275 ask 3): a TYPED clio escalation (the D1 refusal
        # escalation is the concrete reproducer; ClioError/MCPProtocolError
        # covers every other typed clio error too) propagating out of the
        # loop body must not leave the expert lifecycle span dangling on the
        # highway (a "started" with no matching close) or skip publishing
        # the retained History (the S4 repair entry's only read of what THIS
        # turn actually produced before it died). Both fire BEFORE
        # re-raising -- the exception itself is never swallowed here, only
        # observed. Reaches the SSE UI wire for free via the existing
        # status="failed" always-pass rule (gact/semantic_events.py's
        # ``_SSE_ALWAYS_STATUSES`` — no event-type catalog change needed);
        # dedicated RENDERING for it is gact-tui#384, filed separately.
        #
        # SCOPE (re-verify round, narrowed from a bare ``except Exception``):
        # a GENERIC crash (RuntimeError et al.) is NOT caught here and
        # re-raises completely unchanged -- no lifecycle.failed, no closing
        # observation, no retained-history publish. That is ARC's own
        # deliberate crash contract (working_set_fold.py §2.8b,
        # ``emit_step_open``'s own docstring, pinned by
        # ``test_working_set_fold_step_open.py::test_crash_leaves_step_open``):
        # a hard mid-step crash leaves ONLY the pre-execution step_open
        # breadcrumb on the canonical log, never a synthesized closing
        # observation authored after the fact. Widening this except clause
        # to Exception (an earlier version of this fix did exactly that)
        # silently violated that contract for every ordinary crash, not just
        # typed refusals. Fixing the dangling-span/lost-history gap for a
        # GENERIC crash is a separate decision against the ARC fold
        # contract, out of this slice's scope.
        reason = str(getattr(exc, "reason", "") or type(exc).__name__)
        try:
            _emit_expert_lifecycle_event(
                "expert.lifecycle.failed",
                expert_id=expert_id,
                expert_span_id=expert_span_id,
                status="failed",
                payload={"reason": reason, "error": str(exc)},
            )
            if turn_index >= 0:
                _arc_write(
                    arc,
                    session,
                    scope,
                    "observation",
                    {"text": f"[turn escalated] {reason}: {exc}"},
                    turn_index,
                    turn_id=turn_id,
                    expert_span_id=expert_span_id,
                    run_span_id=step_span_id,
                )
            _ctx.publish_trajectory(
                {
                    "history": list(history.messages),
                    "input_args": dict(pending_inputs),
                    "termination_reason": "escalated_error",
                }
            )
        except Exception:  # noqa: BLE001 - cleanup must never swallow the REAL error
            logger.warning(
                "reactv2 escalation cleanup failed expert_id=%s reason=%s",
                expert_id,
                reason,
                exc_info=True,
            )
        raise
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
