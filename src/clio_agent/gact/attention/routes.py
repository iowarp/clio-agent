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
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from clio_agent.gact.attention.items import (
    CSV_COLUMNS,
    items_csv,
    items_payload,
    reduce_items,
    round_number,
)
from clio_agent.gact.attention.lm_calls import (
    LmCall,
    flowcept_lm_call_filter,
    lm_calls_from_flowcept_rows,
    lm_calls_from_jsonl,
)
from clio_agent.gact.attention.lookup import lookup_attention
from clio_agent.gact.attention.profiles import DEFAULT_PROFILE
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.service import SelectionRequest, explain_selection
from clio_agent.gact.attention.store import AttentionStore
from clio_agent.gact.attention.surface_selection import (
    SurfaceSelectionRequest,
    bind_surface_selection,
    validate_surface_reference,
)
from clio_agent.provenance_config import attention_capture_enabled


class AttentionRequestBody(BaseModel):
    """A selection over one generated text of the message."""

    part_id: str = ""
    field: str = "text"
    start: int | None = None
    end: int | None = None
    #: Rendered text the person selected (located server-side when no span is given).
    text: str = ""
    profile: AttentionProfile = DEFAULT_PROFILE
    content_revision: str = ""
    lm_call_id: str = ""


class AttentionItemBody(BaseModel):
    """One answer item: an explicit span over a generated text, or its rendered text."""

    part_id: str = ""
    field: str = "text"
    start: int | None = None
    end: int | None = None
    text: str = ""
    content_revision: str = ""


class AttentionItemsBody(BaseModel):
    """Items of one answer, each reduced on its own (the SPOTTER per-item view)."""

    items: list[AttentionItemBody] = Field(min_length=1, max_length=32)
    #: Cut each item at this delimiter (``;`` reads ``NIL 373; DIVERGES`` as two items).
    split: str = Field(default="", max_length=8)
    profile: AttentionProfile = DEFAULT_PROFILE
    lm_call_id: str = ""
    include_scores: bool = False


class AttentionItemsExportBody(AttentionItemsBody):
    """The per-item view as ``attn_analytics.py`` CSV rows, with its row labels."""

    chain_id: str = ""
    round_no: str = ""
    task_type: str = ""
    model: str = ""


class AttentionLookupBody(BaseModel):
    """Bounded references for either direction, including explicit unavailable media."""

    selections: list[ContentSelection] = Field(min_length=1, max_length=32)
    direction: Literal["generated_to_source", "source_to_generation"] = "generated_to_source"
    profile: AttentionProfile = DEFAULT_PROFILE
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=16, ge=1, le=32)
    lm_call_id: str = Field(default="", max_length=256)


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

    @app.get("/v1/sessions/{sid}/messages/{mid}/attention/content")
    async def get_content_references(
        sid: str,
        mid: str,
        cursor: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        from clio_agent.gact.attention.content_references import message_content_references

        message = next((m for m in app.state.messages.get(sid, []) if m.id == mid), None)
        if message is None:
            raise HTTPException(status_code=404, detail="Message not found in this session")
        rows = await run_in_threadpool(message_content_references, message)
        return {
            "items": rows[cursor : cursor + 100],
            "next_cursor": cursor + 100 if cursor + 100 < len(rows) else None,
        }

    @app.post("/v1/sessions/{sid}/attention/surfaces/{surface_id}/selection")
    async def post_surface_selection(
        sid: str, surface_id: str, body: SurfaceSelectionRequest
    ) -> dict[str, Any]:
        return await run_in_threadpool(bind_surface_selection, app, sid, surface_id, body)

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

    def _items_view(sid: str, mid: str, body: AttentionItemsBody) -> Any:
        return reduce_items(
            messages=list(app.state.messages.get(sid, [])),
            calls=session_lm_calls(app, sid),
            store=attention_store(app),
            renderer_for=_renderer,
            requests=[
                SelectionRequest(
                    message_id=mid,
                    profile=body.profile,
                    lm_call_id=body.lm_call_id,
                    **item.model_dump(),
                )
                for item in body.items
            ],
            delimiter=body.split,
            profile=body.profile,
        )

    @app.post("/v1/sessions/{sid}/messages/{mid}/attention/items")
    async def post_attention_items(sid: str, mid: str, body: AttentionItemsBody) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            return items_payload(_items_view(sid, mid, body), include_scores=body.include_scores)

        try:
            return await run_in_threadpool(run)
        except AttentionUnavailable as exc:
            return exc.to_wire()

    @app.post("/v1/sessions/{sid}/messages/{mid}/attention/items/export")
    async def post_attention_items_export(
        sid: str, mid: str, body: AttentionItemsExportBody
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            view = _items_view(sid, mid, body)
            messages = list(app.state.messages.get(sid, []))
            text = items_csv(
                view,
                chain_id=body.chain_id or sid,
                round_no=body.round_no or str(round_number(messages, mid)),
                task_type=body.task_type,
                model=body.model,
            )
            return {
                "schema": "clio.attention.items.csv.v1",
                "available": True,
                "filename": f"attn_{body.chain_id or sid}_{mid}.csv",
                "columns": list(CSV_COLUMNS),
                "rows": len(view.items),
                "csv": text,
            }

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
                validate_reference=lambda ref: validate_surface_reference(app, sid, ref),
            )

        try:
            return await run_in_threadpool(run)
        except AttentionUnavailable as exc:
            return exc.to_wire()
