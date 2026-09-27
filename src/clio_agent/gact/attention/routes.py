"""HTTP surface of the attention view.

``GET  /v1/sessions/{sid}/messages/{mid}/attention/availability``
    Whether "Understand attention" can run for this message, with the typed
    reason when not (the UI disables the action and shows the reason).
``POST /v1/sessions/{sid}/messages/{mid}/attention``
    Body ``{part_id?, field?, start?, end?, text?}`` -> the attention payload
    (:func:`.service.explain_selection`) or ``{available: false, reason, ...}``.

Both answer 200 with a typed body; an unavailable view is a result, not an
error. Blocking store/tokenizer work runs in the threadpool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from clio_agent.gact.attention.lm_calls import (
    LmCall,
    flowcept_lm_call_filter,
    lm_calls_from_flowcept_rows,
    lm_calls_from_jsonl,
    turn_calls,
)
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.service import SelectionRequest, explain_selection
from clio_agent.gact.attention.store import AttentionStore


class AttentionRequestBody(BaseModel):
    """A selection over one generated text of the message."""

    part_id: str = ""
    field: str = "text"
    start: int | None = None
    end: int | None = None
    #: Rendered text the person selected (located server-side when no span is given).
    text: str = ""


def _backend(app: FastAPI) -> Any:
    return getattr(app.state, "semantic_trace_backend", None)


def _reader(app: FastAPI, name: str) -> Any:
    method = getattr(_backend(app), "reader", None)
    return method(name) if callable(method) else None


def attention_store(app: FastAPI) -> AttentionStore:
    """The Flowcept-backed store, or ``flowcept_not_configured``."""
    flowcept = _reader(app, "flowcept")
    if flowcept is None or not callable(getattr(flowcept, "query_tasks", None)):
        raise AttentionUnavailable(
            "flowcept_not_configured", "add 'flowcept' to provenance.agentic.providers"
        )
    return AttentionStore(flowcept)


def session_lm_calls(app: FastAPI, sid: str) -> list[LmCall]:
    """This session's ``lm.call`` records from the native journal, else Flowcept."""
    backend = _backend(app)
    flush = getattr(backend, "flush", None)
    if callable(flush):
        flush()
    jsonl = _reader(app, "jsonl")
    path = getattr(jsonl, "path", None)
    if isinstance(path, Path):
        file = path if path.suffix else path / f"{sid}.semantic.jsonl"
        calls = lm_calls_from_jsonl(file, sid)
        if calls:
            return calls
    flowcept = _reader(app, "flowcept")
    if flowcept is not None and callable(getattr(flowcept, "query_tasks", None)):
        rows = flowcept.query_tasks(flowcept_lm_call_filter(sid))
        if rows is None:
            raise AttentionUnavailable("attention_query_failed", "lm.call query failed")
        return lm_calls_from_flowcept_rows(rows, sid)
    if jsonl is None:
        raise AttentionUnavailable(
            "lm_call_store_unavailable", "neither the native journal nor Flowcept is configured"
        )
    return []


def _renderer(identity: str) -> Any:
    from clio_agent.gact.attention.tokenizer_source import (  # noqa: PLC0415
        configured_tokenizer,
        renderer_for,
    )

    return renderer_for(configured_tokenizer() or identity)


def availability(app: FastAPI, sid: str, mid: str) -> dict[str, Any]:
    """Cheap per-message check: generated, recorded, vLLM, and a summary exists."""
    messages = app.state.messages.get(sid, [])
    message = next((m for m in messages if m.id == mid), None)
    if message is None:
        raise AttentionUnavailable("message_not_found", mid)
    if message.role != "assistant":
        raise AttentionUnavailable("message_not_generated", message.role)
    store = attention_store(app)
    calls = turn_calls(session_lm_calls(app, sid), message.turn_id)
    if not calls:
        raise AttentionUnavailable("lm_call_not_found", f"no lm.call for turn {message.turn_id}")
    if not any(c.model.startswith("hosted_vllm/") for c in calls):
        raise AttentionUnavailable("provider_not_vllm", calls[-1].model)
    last = calls[-1]
    summary = store.summary_for(last.response_id)
    return {
        "available": True,
        "response_id": last.response_id,
        "top_pct": summary.top_pct,
        "decode_steps": summary.decode_steps,
    }


def register_attention_routes(app: FastAPI) -> None:
    """Mount the attention endpoints on ``app``."""

    @app.get("/v1/sessions/{sid}/messages/{mid}/attention/availability")
    async def get_attention_availability(sid: str, mid: str) -> dict[str, Any]:
        try:
            return await run_in_threadpool(availability, app, sid, mid)
        except AttentionUnavailable as exc:
            return exc.to_wire()

    @app.post("/v1/sessions/{sid}/messages/{mid}/attention")
    async def post_attention(sid: str, mid: str, body: AttentionRequestBody) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            return explain_selection(
                messages=list(app.state.messages.get(sid, [])),
                calls=session_lm_calls(app, sid),
                store=attention_store(app),
                renderer_for=_renderer,
                request=SelectionRequest(
                    message_id=mid,
                    part_id=body.part_id,
                    field=body.field,
                    start=body.start,
                    end=body.end,
                    text=body.text,
                ),
            )

        try:
            return await run_in_threadpool(run)
        except AttentionUnavailable as exc:
            return exc.to_wire()
