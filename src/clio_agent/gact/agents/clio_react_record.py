"""What :class:`~clio_agent.gact.agents.clio_react.ClioReAct` records and reads back.

One step of the loop is recorded three ways, all span-correlated:

* the ARC live plane -- a ``thought`` segment, then a ``tool_call`` + ``observation``
  segment per call (carrying the call id and whether the result is an error), after a
  pre-execution ``step_open`` breadcrumb;
* the semantic highway -- ``react.step.completed`` per step and the expert lifecycle
  (``expert.lifecycle.started`` / ``expert.extract.completed`` / ``expert.lifecycle.failed``);
* the step's context for the tool observer (step thought + parent span).

The loop's context is read back from the same plane BY STEP: :func:`fold_steps` turns
the ordered live segments into one ``dspy.History`` event per step (thought + every
tool call of that step + their results), so a step with concurrent calls renders as
the one step it was. A read failure is a typed :class:`ContextReadError` -- there is no
fallback to another history.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import dspy
from dspy.adapters import ToolCallResults

from clio_agent.errors import ClioError

logger = logging.getLogger(__name__)

# Tools whose successful call is itself the terminal outcome of the current model
# turn. Their authoritative payload stays in session metadata until the post-forward
# pause seam mints the user-facing interaction.
TURN_YIELD_METADATA: dict[str, str] = {
    "ask_user": "pending_ask_user",
    "plan_exit": "pending_plan_exit",
}

_FOLDED_KINDS = frozenset({"thought", "tool_call", "observation", "summary"})


class ContextReadError(ClioError):
    """The loop could not read its context from the ARC live plane (typed turn failure)."""

    reason = "arc_context_read_failed"

    def __init__(self, scope: str, cause: BaseException) -> None:
        super().__init__(
            f"could not read the agent context for scope {scope!r}: {cause}",
            error_type=self.reason,
            details={"scope": scope, "cause": type(cause).__name__},
        )


def arc_scope() -> tuple[Any, str, str]:
    """Resolve ``(ARCMemory, session_id, scope)`` for the live plane, or ``(None, '', '')``.

    ``arc`` is ``None`` whenever there is no app, no ARC, or no react scope (a bare
    unit call, the CLI) -- then the loop's own History is its context. The in-process
    variant try index is folded into the ARC key only (#953).
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    session = _ctx.active_react_session()
    arc = getattr(getattr(app, "state", None), "arc", None) if (app is not None and scope) else None
    if arc is None:
        return None, "", ""
    return arc, session, scope


def reset_working_set(arc: Any, session: str, scope: str) -> None:
    """Tombstone the scope's prior live working set (a new forward is a new turn)."""
    if arc is None:
        return
    try:
        prior = [s.id for s in arc.render_working_set(session, scope)]
        if prior:
            arc.delete_segments(session, scope, prior)
    except Exception:  # noqa: BLE001 - the wipe is best-effort; the read below is typed
        logger.warning("arc live-plane reset failed scope=%s", scope, exc_info=True)


def read_steps(arc: Any, session: str, scope: str) -> list[dict[str, Any]]:
    """Fold the scope's live plane into History events; typed failure on a read error."""
    try:
        segments = arc.render_segments(session, scope)
    except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
        raise ContextReadError(scope, exc) from exc
    return fold_steps(segments)


def fold_steps(segments: list[Any]) -> list[dict[str, Any]]:
    """Group ordered live segments into one ``dspy.History`` event per step.

    A ``thought`` opens a step; its ``tool_call`` / ``observation`` segments attach to
    it, observations matched to calls by call id (by order for a segment written
    without one). A ``summary`` -- or an observation with no open step -- is its own
    event carrying the text as ``next_thought``, so compacted content still reaches
    the wire. Pure; tolerant of malformed content.
    """
    events: list[dict[str, Any]] = []
    step: _StepFold | None = None
    for seg in segments:
        kind = getattr(seg, "kind", "")
        if kind not in _FOLDED_KINDS:
            continue
        content = getattr(seg, "content", None) or {}
        if kind == "thought":
            if step is not None:
                events.append(step.event())
            step = _StepFold(_text(content.get("text")))
        elif kind == "tool_call" and step is not None:
            step.add_call(content)
        elif kind == "observation" and step is not None and step.expects_result():
            step.add_result(content)
        else:
            if step is not None:
                events.append(step.event())
                step = None
            events.append({"next_thought": _text(content.get("text"))})
    if step is not None:
        events.append(step.event())
    return events


class _StepFold:
    """Accumulates one step's thought, calls and results while folding."""

    def __init__(self, thought: str) -> None:
        self.thought = thought
        self.calls: list[dspy.ToolCalls.ToolCall] = []
        self.results: dict[str, tuple[Any, bool]] = {}

    def add_call(self, content: Mapping[str, Any]) -> None:
        args = content.get("args")
        call_id = str(content.get("id") or f"call_{len(self.calls)}")
        self.calls.append(
            dspy.ToolCalls.ToolCall(
                id=call_id,
                name=_text(content.get("name")),
                args=dict(args) if isinstance(args, Mapping) else {},
            )
        )

    def expects_result(self) -> bool:
        return len(self.results) < len(self.calls)

    def add_result(self, content: Mapping[str, Any]) -> None:
        call_id = str(content.get("call_id") or "")
        if call_id not in {c.id for c in self.calls} or call_id in self.results:
            call_id = next(str(c.id) for c in self.calls if c.id not in self.results)
        self.results[call_id] = (content.get("text", ""), bool(content.get("is_error")))

    def event(self) -> dict[str, Any]:
        event: dict[str, Any] = {"next_thought": self.thought}
        if not self.calls:
            return event
        tool_calls = dspy.ToolCalls(tool_calls=list(self.calls))
        answered = [c for c in self.calls if c.id in self.results]
        if answered:
            tool_calls = tool_calls.model_copy(
                update={
                    "tool_call_results": ToolCallResults.from_tool_calls_and_values(
                        answered,
                        [self.results[str(c.id)][0] for c in answered],
                        [self.results[str(c.id)][1] for c in answered],
                    )
                }
            )
        event["tool_calls"] = tool_calls
        return event


class StepRecorder:
    """Writes one expert forward's steps to the ARC live plane and the highway."""

    def __init__(self, arc: Any, session: str, scope: str, *, expert_id: str) -> None:
        from clio_agent.gact.runtime.globals import _active_semantic_turn_id  # noqa: PLC0415

        self.arc = arc
        self.session = session
        self.scope = scope
        self.expert_id = expert_id
        self.turn_id = _active_semantic_turn_id()
        self.expert_span_id = ""

    def started(self, expert_span_id: str, inputs: Mapping[str, Any]) -> None:
        """Open the expert lifecycle on the highway."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415
        from clio_agent.tools.mcp_runtime import wire_value  # noqa: PLC0415

        self.expert_span_id = expert_span_id
        _emit_expert_lifecycle_event(
            "expert.lifecycle.started",
            expert_id=self.expert_id,
            expert_span_id=expert_span_id,
            status="running",
            payload={"input": wire_value(dict(inputs), mode="gact_runtime")},
        )

    def step_open(self, step: int, span: str, thought: str, calls: dspy.ToolCalls) -> None:
        """The pre-execution breadcrumb: a crash mid-step still leaves the step's opening."""
        from clio_agent.arc.working_set_fold import emit_step_open  # noqa: PLC0415

        emit_step_open(
            self.arc,
            self.session,
            self.scope,
            {"thought": thought, "tools": [c.name for c in calls.tool_calls]},
            step=step,
            turn_id=self.turn_id,
            expert_span_id=self.expert_span_id,
            run_span_id=span,
        )

    def step_done(
        self,
        step: int,
        span: str,
        *,
        thought: str,
        reasoning: str,
        calls: dspy.ToolCalls,
        results: ToolCallResults,
    ) -> None:
        """Write the step's segments and put ``react.step.completed`` on the highway."""
        from clio_agent.gact.runtime.context_tokens import _arc_obs_value  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _emit_react_step_event  # noqa: PLC0415

        by_id = {r.call_id: r for r in results.tool_call_results if r.call_id is not None}
        self._write("thought", {"text": thought}, step, span)
        for call in calls.tool_calls:
            result = by_id.get(call.id)
            self._write(
                "tool_call",
                {"id": call.id, "name": call.name, "args": dict(call.args or {})},
                step,
                span,
            )
            self._write(
                "observation",
                {
                    "call_id": call.id,
                    "text": _arc_obs_value(result.value if result is not None else ""),
                    "is_error": bool(result is not None and result.is_error),
                },
                step,
                span,
            )
        _emit_react_step_event(
            expert_id=self.expert_id,
            expert_span_id=self.expert_span_id,
            step_span_id=span,
            step_index=step,
            thought=thought,
            reasoning=reasoning,
            tool_calls=[
                {
                    "id": call.id,
                    "name": call.name,
                    "args": dict(call.args or {}),
                    "observation": by_id[call.id].value if call.id in by_id else "",
                    "is_error": bool(call.id in by_id and by_id[call.id].is_error),
                }
                for call in calls.tool_calls
            ],
            is_finish=any(call.name == "submit" for call in calls.tool_calls),
        )

    def completed(self, outputs: Mapping[str, Any] | None, step_count: int) -> None:
        """Close the expert lifecycle with the final answer and structured outputs."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415

        outputs = outputs or {}
        _emit_expert_lifecycle_event(
            "expert.extract.completed",
            expert_id=self.expert_id,
            expert_span_id=self.expert_span_id,
            status="completed",
            payload={
                "output": str(outputs.get("answer", "") or ""),
                "structured": {
                    k: v for k, v in outputs.items() if k != "answer" and v not in (None, "")
                },
                "step_count": step_count,
            },
        )

    def failed(self, exc: ClioError, step: int, span: str) -> None:
        """Close the lifecycle ``failed`` and record why on the plane (never raises)."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415

        reason = str(getattr(exc, "reason", "") or type(exc).__name__)
        try:
            _emit_expert_lifecycle_event(
                "expert.lifecycle.failed",
                expert_id=self.expert_id,
                expert_span_id=self.expert_span_id,
                status="failed",
                payload={"reason": reason, "error": str(exc)},
            )
            if step >= 0:
                self._write(
                    "observation", {"text": f"[turn escalated] {reason}: {exc}"}, step, span
                )
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            logger.warning(
                "clio_react escalation cleanup failed expert_id=%s reason=%s",
                self.expert_id,
                reason,
                exc_info=True,
            )

    def _write(self, kind: str, content: dict[str, Any], step: int, span: str) -> None:
        if self.arc is None:
            return
        try:
            self.arc.append_segment(
                self.session,
                self.scope,
                kind,
                content,
                step=step,
                token_count=max(1, len(json.dumps(content, default=str)) // 4),
                turn_id=self.turn_id,
                expert_span_id=self.expert_span_id,
                run_span_id=span,
            )
        except Exception:  # noqa: BLE001 - a lost write surfaces as a typed read failure
            logger.warning(
                "arc live-plane append failed kind=%s scope=%s", kind, self.scope, exc_info=True
            )


def pending_turn_yield(calls: dspy.ToolCalls) -> str:
    """The successful turn-ending tool in this step (``ask_user`` / ``plan_exit``), if any.

    The tool name alone is not enough: a rejected ``plan_exit`` or malformed
    ``ask_user`` stays an ordinary tool error. The un-surfaced session-metadata record
    the tool wrote is the proof it completed and the turn should yield to the user.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    names = {str(call.name or "") for call in calls.tool_calls}
    if not names.intersection(TURN_YIELD_METADATA):
        return ""
    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, Mapping):
        return ""
    for tool_name, key in TURN_YIELD_METADATA.items():
        pending = metadata.get(key)
        if (
            tool_name in names
            and isinstance(pending, Mapping)
            and pending
            and not pending.get("surfaced")
        ):
            return tool_name
    return ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value if value is not None else "")
