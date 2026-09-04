"""Forced-submit and audit owners for the CLIO ReActV2 loop."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import dspy
from dspy.adapters.types.tool import ToolCalls

REACT_FORCED_SUBMIT_REJECTED = "react_forced_submit_rejected"


def adapter_tool_intent_from_exception(
    exc: BaseException,
    *,
    allowed_tools: Iterable[str],
) -> dict[str, Any] | None:
    """Recover a typed tool intent emitted where DSPy expected final fields."""

    from clio_agent.gact.app import _json_objects_from_text  # noqa: PLC0415

    allowed = {str(name).strip() for name in allowed_tools if str(name).strip()}
    message = str(exc)
    if not allowed or "tool_name" not in message or "tool_args" not in message:
        return None
    for obj in _json_objects_from_text(message):
        if not isinstance(obj, Mapping):
            continue
        tool_name = str(obj.get("tool_name") or obj.get("name") or "").strip()
        if tool_name not in allowed:
            continue
        tool_args = obj.get("tool_args") or obj.get("args") or obj.get("arguments") or {}
        return {
            "tool_name": tool_name,
            "tool_args": dict(tool_args) if isinstance(tool_args, Mapping) else {},
        }
    return None


def call_recovered_dspy_tool(tool: Any, args: Mapping[str, Any]) -> Any:
    """Call a DSPy tool recovered from malformed ReAct adapter output."""

    if callable(tool):
        return tool(**dict(args))
    func = getattr(tool, "func", None)
    if callable(func):
        return func(**dict(args))
    raise TypeError(f"tool is not callable: {getattr(tool, 'name', '<unknown>')}")


def tool_names(tools: Iterable[Any]) -> list[str]:
    """Return stable names from DSPy tool-like objects."""

    return [name for tool in tools if (name := str(getattr(tool, "name", "") or "").strip())]


def _forced_submit_error_info(break_reason: str) -> dict[str, Any] | None:
    """Describe a forced partial answer caused by malformed provider output.

    A forced ``submit`` can preserve useful text after the normal tool loop is no
    longer viable, but it must not turn a protocol failure into a successful turn.
    The ``partial`` marker keeps that text visible while the GACT turn settles as
    failed and remains explicitly retryable.
    """
    if break_reason != "empty_tool_calls":
        return None
    return {
        "error": "provider_protocol_error",
        "message": (
            "The provider repeatedly returned an agent step without a structured "
            "tool call. Any partial response is preserved; retry the turn."
        ),
        "details": {
            "partial": True,
            "termination_reason": break_reason,
            "recovery_actions": ["retry_turn"],
        },
        "recoverable": True,
    }


def active_react_scope_safe() -> str:
    """Return the active ReAct scope, or an empty value outside a live turn."""
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        return _ctx.active_react_scope()
    except Exception:  # noqa: BLE001 - scope is optional off-turn
        return ""


def record_submit_audit(
    reason: str,
    *,
    agent_id: str,
    field: str,
    text: str,
    suppressed: bool,
) -> None:
    """Emit one queryable V2-path stream-audit record."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(
        "bridge.contract_field",
        agent_id=agent_id or "",
        field=field,
        chunk_len=len(text),
        visible=False,
        duplicate_suppressed=suppressed,
        duplicate_reason=reason,
        head=text[:120],
        full_text=text[:12000],
    )
    if reason != "react_submit_repair_attempted":
        return
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _emit_semantic_event,
        _llm_provider_payload,
    )

    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    if app is None or not session_id:
        return
    trajectory = _ctx.active_trajectory() or {}
    termination = str(trajectory.get("termination_reason") or "")
    repair_reason = "no_tool_call" if termination == "empty_tool_calls" else "parse_repair"
    provider = _llm_provider_payload(app, agent_id)
    _emit_semantic_event(
        app,
        session_id,
        "agent.submit_repair.attempted",
        turn_id=_ctx.active_turn_id(),
        trace_id=_ctx.active_trace_id(),
        status="running",
        summary="Agent output required a bounded submit re-ask.",
        actor={"agent_id": agent_id},
        provider=provider,
        payload={"reason": repair_reason},
        detail_level="off",
    )


def record_forced_submit_rejection(attempted: str) -> None:
    """Record a forced-finalization response that did not produce ``submit``."""
    record_submit_audit(
        REACT_FORCED_SUBMIT_REJECTED,
        agent_id=active_react_scope_safe(),
        field="submit",
        text=attempted,
        suppressed=False,
    )


def forced_submit(
    owner: Any,
    history: dspy.History,
    pending_inputs: dict[str, Any],
    break_reason: str,
    turn_index: int,
) -> Any:
    """Finalize a ReActV2 owner through its sole legal ``submit`` operation."""
    from dspy.predict.react_v2 import (  # noqa: PLC0415
        _append_history_event,
        _coerce_tool_calls,
        _ensure_tool_call_ids,
    )
    from dspy.primitives.prediction import Prediction  # noqa: PLC0415
    from dspy.utils.exceptions import (  # noqa: PLC0415
        AdapterParseError,
        ContextWindowExceededError,
    )

    try:
        pred = owner.react(
            history=history,
            tools=[owner.tools["submit"]],
            config={
                "tool_choice": {"type": "function", "function": {"name": "submit"}},
                "reasoning_effort": None,
            },
            **pending_inputs,
        )
        tool_calls = _ensure_tool_call_ids(
            _coerce_tool_calls(getattr(pred, "tool_calls", None)), turn_index
        )
    except (AdapterParseError, ValueError, ContextWindowExceededError) as exc:
        record_forced_submit_rejection(type(exc).__name__)
        return Prediction(history=history, termination_reason=break_reason or "failed")

    submit_calls = ToolCalls(
        tool_calls=[call for call in tool_calls.tool_calls if call.name == "submit"]
    )
    if not submit_calls.tool_calls:
        attempted = [call.name for call in tool_calls.tool_calls]
        record_forced_submit_rejection(", ".join(attempted) or "<empty>")
        return Prediction(history=history, termination_reason=break_reason or "failed")

    tool_call_results, final_outputs = owner._execute_tool_calls(submit_calls)
    event = owner._history_event(pending_inputs, pred, submit_calls, tool_call_results)
    if final_outputs is not None:
        event.update(final_outputs)
    _append_history_event(history, event)

    if final_outputs is not None:
        prediction = Prediction(
            **final_outputs,
            history=history,
            termination_reason="forced_submit",
        )
        error_info = _forced_submit_error_info(break_reason)
        if error_info is not None:
            prediction.error_info = error_info
        return prediction
    return Prediction(history=history, termination_reason=break_reason or "failed")
