"""The ``lm.call`` trace event: one per LM call, from a DSPy callback.

Every clio LM carries :data:`LM_CALL_TRACE` (``dspy.LM(callbacks=[...])``). DSPy calls
``on_lm_start`` / ``on_lm_end`` around each call -- the agent loop's typed
``lm(Request)`` calls and the adapter calls of the other modules alike -- and the end
handler records what was sent and what came back as the canonical trace's durable-only
``lm.call`` event (plus the ``provider.batch_response`` audit row). A failed call is
recorded too, with its error. Recording never fails a call: an emit error is traced
(``LM-CALL-CAPTURE``), never raised and never silent.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from dspy.utils.callback import BaseCallback

__all__ = ["LM_CALL_TRACE", "LMCallTrace", "call_record"]


class LMCallTrace(BaseCallback):
    """Emit ``lm.call`` for every call of the LMs that carry it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, tuple[Any, dict[str, Any]]] = {}

    def on_lm_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        """Remember the call's LM and inputs until it ends."""
        with self._lock:
            self._open[call_id] = (instance, inputs)

    def on_lm_end(
        self, call_id: str, outputs: Any | None, exception: BaseException | None = None
    ) -> None:
        """Record the finished (or failed) call."""
        with self._lock:
            instance, inputs = self._open.pop(call_id, (None, {}))
        try:
            _emit(
                call_record(str(getattr(instance, "model", "") or ""), inputs, outputs, exception)
            )
        except Exception as exc:  # noqa: BLE001 - recording must never fail a call
            from clio_agent.runtime import trace  # noqa: PLC0415

            trace.event("LM-CALL-CAPTURE", "lm.call capture/emit failed: %r", exc)


#: The one recorder every clio LM carries.
LM_CALL_TRACE = LMCallTrace()


def call_record(
    model: str, inputs: dict[str, Any], outputs: Any, exception: BaseException | None
) -> dict[str, Any]:
    """The ``lm.call`` payload for one call (typed Request/Response or adapter I/O)."""
    request = inputs.get("prompt") or inputs.get("kwargs", {}).get("request")
    if hasattr(request, "messages") and hasattr(request, "model"):
        messages: Any = _request_messages(request)
    else:
        messages = inputs.get("messages") or request
    content, reasoning, finish, usage = _outputs(outputs)
    record: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "content": content,
        "content_len": len(content),
        "reasoning_content": reasoning,
        "reasoning_len": len(reasoning),
        "finish_reason": finish,
        "usage": usage,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    from clio_agent.gact.attention.declare import (  # noqa: PLC0415
        current_declaration,
        current_wire_messages,
    )
    from clio_agent.provenance_config import response_id_join_enabled  # noqa: PLC0415

    if response_id_join_enabled():
        # Stage 3 kvnorm crosslink: the provider response id keys this call to its
        # kv_token_importance record in the fused Flowcept store.
        record["response_id"] = str(getattr(outputs, "id", "") or "")
    if (declaration := current_declaration()) is not None:
        record["attention"] = declaration
    if (wire_messages := current_wire_messages()) is not None:
        record["attention_messages"] = wire_messages
    if exception is not None:
        record["error"] = f"{type(exception).__name__}: {exception}"
    return record


def _request_messages(request: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if request.system:
        out.append({"role": "system", "content": _system_text(request.system)})
    out.extend({"role": m.role, "parts": [_part(p) for p in m.parts]} for m in request.messages)
    return out


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "".join(getattr(p, "text", "") for p in system)


def _part(part: Any) -> dict[str, Any]:
    kind = str(getattr(part, "type", type(part).__name__))
    if kind in {"text", "thinking"}:
        return {"type": kind, "text": part.text}
    if kind == "tool_call":
        return {"type": kind, "id": part.id, "name": part.name, "input": dict(part.input)}
    if kind == "tool_result":
        return {
            "type": kind,
            "id": part.id,
            "name": part.name,
            "is_error": part.is_error,
            "content": [_part(p) for p in part.content],
        }
    # Media: the kind and type only -- never the bytes.
    return {"type": kind, "media_type": str(getattr(part, "media_type", "") or "")}


def _outputs(outputs: Any) -> tuple[str, str, str, dict[str, Any] | None]:
    """``(content, reasoning, finish_reason, usage)`` of a Response or adapter outputs."""
    message = getattr(outputs, "message", None)
    if message is not None:
        text = "".join(p.text for p in message.parts if getattr(p, "type", "") == "text")
        thinking = "".join(p.text for p in message.parts if getattr(p, "type", "") == "thinking")
        usage = getattr(outputs, "usage", None)
        return text, thinking, str(outputs.finish_reason or ""), _usage(usage)
    first = next(iter(outputs), None) if isinstance(outputs, Iterable) else None
    if isinstance(first, dict):
        return str(first.get("text") or ""), str(first.get("reasoning_content") or ""), "", None
    return str(first or ""), "", "", None


def _usage(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        name: getattr(usage, name)
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")
        if getattr(usage, name, None) is not None
    }


def _emit(record: dict[str, Any]) -> None:
    """Audit row always (when enabled); the ``lm.call`` event inside a GACT turn."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    session, turn, trace_id = (
        _ctx.active_session_id(),
        _ctx.active_turn_id(),
        _ctx.active_trace_id(),
    )
    content, reasoning = record["content"], record["reasoning_content"]
    stream_audit(
        "provider.batch_response",
        provider="dspy_lm",
        session_id=session,
        turn_id=turn,
        trace_id=trace_id,
        model=record["model"],
        source_channel=(
            "content+reasoning_content"
            if content and reasoning
            else ("reasoning_content" if reasoning else "content")
        ),
        content_len=record["content_len"],
        reasoning_len=record["reasoning_len"],
        chunk_len=len(content or reasoning),
        finish_reason=record["finish_reason"],
        head=(content or reasoning)[:120],
    )
    app = _ctx.active_app()
    if app is None or not session:
        return  # no GACT turn (CLI, optimizer): the audit row is the record

    from clio_agent.arc.loop_guard import on_server_loop  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    status = "failed" if "error" in record else "completed"

    def emit() -> Any:
        return _emit_semantic_event(
            app,
            session,
            "lm.call",
            turn_id=turn,
            trace_id=trace_id,
            status=status,
            summary=f"LM call ({record['finish_reason'] or status}).",
            provider={"model_id": record["model"]},
            payload=record,
            detail_level="off",
        )

    if on_server_loop():
        # On the server loop the persist is a blocking store RPC: hand it off.
        from clio_agent.gact.off_loop import schedule_off_loop  # noqa: PLC0415

        schedule_off_loop(emit, label="lm.call")
    else:
        emit()
