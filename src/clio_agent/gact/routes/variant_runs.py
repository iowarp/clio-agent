"""``GET /v1/sessions/{sid}/variant-runs``: a session's BestOfN / Refine runs (Phase 9).

Served from clio-core, never rebuilt from the trace: the latest ``variant_record`` of
every run (:mod:`clio_agent.gact.agents.variant_records`), and for each try its own
steps folded from the try's segments (what it saw and did: its task or advice,
thinking, text, tool calls and results). So a client renders the variant tabs after a
reload exactly as it saw them live. ``anchor_message_id`` is the assistant message of
the turn a run (or a try) ran in, where the client places the block (``""`` while that
turn has no assistant message yet). A human-judged run that ended without a pick has
``status`` ``superseded`` / ``cancelled`` / ``expired``, its typed ``closed_reason``,
``closed_at`` and (superseded) ``superseded_by_message_id``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.concurrency import run_in_threadpool

from clio_agent.errors import ClioError
from clio_agent.gact.agents.variant_records import SCHEMA, TryRecord, VariantRun, latest_runs
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

__all__ = ["register_variant_run_routes", "variant_runs_wire"]

#: A failure that is the caller's or the deployment's, not a broken record.
_STATUS = {"no_context_store": 503}


def _error(status: int, error: str, message: str, details: Mapping[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, details=dict(details), recoverable=False)
        ).model_dump(exclude_none=True),
    )


def _part_wire(part: Any) -> dict[str, Any]:
    """One folded message part as JSON (media as its type, never its bytes)."""
    kind = type(part).__name__
    if kind == "TextPart":
        return {"type": "text", "text": part.text}
    if kind == "ThinkingPart":
        return {"type": "thinking", "text": part.text}
    if kind == "ToolCallPart":
        return {"type": "tool_call", "id": part.id, "name": part.name, "input": dict(part.input)}
    if kind == "ToolResultPart":
        return {
            "type": "tool_result",
            "id": part.id,
            "name": part.name or "",
            "is_error": bool(part.is_error),
            "content": [_part_wire(item) for item in part.content],
        }
    if kind in {"ImagePart", "DocumentPart"}:
        return {
            "type": "image" if kind == "ImagePart" else "document",
            "media_type": part.media_type,
        }
    return {"type": kind}


def _steps(plane: Any, session_id: str, record: TryRecord) -> list[dict[str, Any]]:
    """The try's own segments folded into messages (``[]`` for a try that never forked)."""
    from clio_agent.gact.agents.clio_react_record import fold_steps  # noqa: PLC0415

    if not record.segment_ids:
        return []
    wanted = set(record.segment_ids)
    segments = [
        seg
        for seg in plane.list_segments(session_id, record.scope, include_tombstoned=True)
        if seg.id in wanted
    ]
    return [
        {"role": message.role, "parts": [_part_wire(p) for p in message.parts]}
        for message in fold_steps(segments)
    ]


def _anchors(messages: Sequence[Any]) -> dict[str, str]:
    """Each turn's assistant message id (the latest one of the turn)."""
    anchors: dict[str, str] = {}
    for message in messages:
        if getattr(message, "role", "") == "assistant" and getattr(message, "turn_id", ""):
            anchors[str(message.turn_id)] = str(message.id)
    return anchors


def _run_wire(plane: Any, run: VariantRun, anchors: Mapping[str, str]) -> dict[str, Any]:
    tries = [
        {
            "try_index": t.try_index,
            "scope": t.scope,
            "state": t.status,
            "text": t.text,
            "score": t.score,
            "tokens": dict(t.tokens),
            "advice": t.advice,
            "forked_from": t.forked_from,
            "error": t.error,
            "turn_id": t.turn_id,
            "anchor_message_id": anchors.get(t.turn_id, ""),
            "steps": _steps(plane, run.session_id, t),
        }
        for t in run.tries
    ]
    return {
        "schema": SCHEMA,
        "variants_id": run.variants_id,
        "session_id": run.session_id,
        "agent_id": run.agent_id,
        "origin": run.origin,
        "strategy": run.strategy,
        "judge": run.judge,
        "n": run.n,
        "n_requested": run.n_requested,
        "rubric": run.rubric,
        "threshold": run.threshold,
        "status": run.status,
        "turn_id": run.turn_id,
        "anchor_message_id": anchors.get(run.turn_id, ""),
        "question_id": run.question_id,
        "pick": run.pick,
        "comment": run.comment,
        "selected_index": run.selected_index,
        "closed_reason": run.closed_reason,
        "closed_at": run.closed_at,
        "superseded_by_message_id": run.superseded_by_message_id,
        "tries": tries,
    }


def variant_runs_wire(app: Any, session_id: str) -> dict[str, Any]:
    """Every run of ``session_id`` as served (oldest first); raises typed on a failure."""
    from clio_agent.arc.history_plane import plane_for  # noqa: PLC0415

    runs = latest_runs(app, session_id)
    plane = plane_for(app)
    anchors = _anchors(app.state.messages.get(session_id, []) or [])
    return {
        "session_id": session_id,
        "runs": [_run_wire(plane, run, anchors) for run in runs.values()],
    }


def register_variant_run_routes(app: FastAPI) -> None:
    """Register ``GET /v1/sessions/{sid}/variant-runs`` on ``app``."""

    @app.get("/v1/sessions/{sid}/variant-runs")
    async def list_variant_runs(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}", {"session_id": sid})
        try:
            # clio-core reads: off the event loop.
            return await run_in_threadpool(variant_runs_wire, app, sid)
        except ClioError as exc:
            status = _STATUS.get(exc.error_type, 500)
            details = {**exc.details, "session_id": sid}
            raise _error(status, exc.error_type, exc.message, details) from exc
