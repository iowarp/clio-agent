"""HTTP surface of the attention view.

``GET  /v1/sessions/{sid}/attention/availability``
    Whether "Understand attention" can run in this session at all, and for
    which answers: ``{enabled, reason?, message?, messages: {id: bool}}``. The
    UI offers the action only on answers marked true, so a CLIO without
    attention capture (or a non-vLLM answer) never shows it. Cheap: config,
    the recorded lm.calls, and one Flowcept query; no attention files are read.
``POST /v1/sessions/{sid}/messages/{mid}/attention``
    Body ``{part_id?, field?, start?, end?, text?}`` -> the attention payload
    (:func:`.service.explain_selection`) or ``{available: false, reason, ...}``.

Both answer 200 with a typed body; an unavailable view is a result, not an
error. Blocking store/tokenizer work runs in the threadpool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from clio_schemas.attention import AttentionProfile
from clio_schemas.connected_resources import ContentSelection
from fastapi import FastAPI
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from clio_agent.gact.attention.lm_calls import (
    LmCall,
    flowcept_lm_call_filter,
    lm_calls_from_flowcept_rows,
    lm_calls_from_jsonl,
)
from clio_agent.gact.attention.lookup import lookup_attention
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.service import SelectionRequest, explain_selection
from clio_agent.gact.attention.store import AttentionStore
from clio_agent.provenance_config import attention_capture_enabled


class AttentionRequestBody(BaseModel):
    """A selection over one generated text of the message."""

    part_id: str = ""
    field: str = "text"
    start: int | None = None
    end: int | None = None
    #: Rendered text the person selected (located server-side when no span is given).
    text: str = ""
    profile: AttentionProfile = AttentionProfile()
    content_revision: str = ""
    lm_call_id: str = ""


class AttentionLookupBody(BaseModel):
    """Bounded references for either direction, including explicit unavailable media."""

    selections: list[ContentSelection] = Field(min_length=1, max_length=32)
    direction: Literal["generated_to_source", "source_to_generation"] = "generated_to_source"
    profile: AttentionProfile = AttentionProfile()
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=16, ge=1, le=32)


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


def session_availability(app: FastAPI, sid: str) -> dict[str, Any]:
    """Which assistant answers of ``sid`` can show attention (see module docstring)."""
    if not attention_capture_enabled():
        raise AttentionUnavailable(
            "attention_disabled", "set provenance.attention and add 'flowcept' to the providers"
        )
    store = attention_store(app)
    answers = [m for m in app.state.messages.get(sid, []) if m.role == "assistant"]
    calls = session_lm_calls(app, sid) if answers else []
    vllm_by_turn: dict[str, list[str]] = {}
    for call in calls:
        if call.model.startswith("hosted_vllm/") and call.response_id:
            vllm_by_turn.setdefault(call.turn_id, []).append(call.response_id)
    recorded = store.recorded_responses([rid for ids in vllm_by_turn.values() for rid in ids])
    return {
        "enabled": True,
        "messages": {
            m.id: any(rid in recorded for rid in vllm_by_turn.get(m.turn_id, [])) for m in answers
        },
    }


def register_attention_routes(app: FastAPI) -> None:
    """Mount the attention endpoints on ``app``."""

    @app.get("/v1/sessions/{sid}/attention/availability")
    async def get_attention_availability(sid: str) -> dict[str, Any]:
        try:
            return await run_in_threadpool(session_availability, app, sid)
        except AttentionUnavailable as exc:
            wire = exc.to_wire()
            return {
                "enabled": False,
                "reason": wire["reason"],
                "message": wire["message"],
                "detail": wire["detail"],
                "messages": {},
            }

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
                    profile=body.profile,
                    content_revision=body.content_revision,
                    lm_call_id=body.lm_call_id,
                ),
            )

        try:
            return await run_in_threadpool(run)
        except AttentionUnavailable as exc:
            return exc.to_wire()

    @app.post("/v1/sessions/{sid}/attention/lookup")
    async def post_attention_lookup(sid: str, body: AttentionLookupBody) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            return lookup_attention(
                session_id=sid,
                messages=list(app.state.messages.get(sid, [])),
                calls=session_lm_calls(app, sid),
                store=attention_store(app),
                renderer_for=_renderer,
                **body.model_dump(exclude={"selections", "profile"}),
                selections=body.selections,
                profile=body.profile,
            )

        try:
            return await run_in_threadpool(run)
        except AttentionUnavailable as exc:
            return exc.to_wire()
