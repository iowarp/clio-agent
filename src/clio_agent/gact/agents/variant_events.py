"""A variant run on the highway, and the context one try runs in (Phase 9).

Events (recorded on clio-core first, then the UI, trace and hooks):

* ``variant.try`` -- per try, when it starts (status ``running``) and when it ends
  (``completed`` with its final text, score and tokens, or ``failed`` with the error);
* ``variant.try.delta`` -- the try's streamed text / thinking, live, for its tab;
* ``variant.selected`` -- the selection: the judge's scores or the user's pick and
  comment, and the selected text;
* ``variant.closed`` -- a human-judged run ended without a pick: ``superseded`` (a new
  turn started first), ``cancelled`` or ``expired`` (:mod:`variant_close`).

Every payload carries the run's stable ``variants_id`` and the try's ``try_index``. While
a try runs (:func:`try_context`), every other semantic event emitted in it -- its steps,
its tool calls, its lifecycle -- is stamped with the same two keys
(:func:`clio_agent.gact.context.stamp_active_try`), so a client groups a try's normal
step events under its tab.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import dspy

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.variant_records import TryRecord, VariantRun

__all__ = [
    "VARIANT_CLOSED",
    "VARIANT_SELECTED",
    "VARIANT_TRY",
    "VARIANT_TRY_DELTA",
    "emit_closed",
    "emit_selected",
    "emit_try",
    "try_context",
]

VARIANT_TRY = "variant.try"
VARIANT_TRY_DELTA = "variant.try.delta"
VARIANT_SELECTED = "variant.selected"
VARIANT_CLOSED = "variant.closed"

_SEMANTIC_STATUS = {"running": "running", "completed": "completed", "failed": "failed"}


def _emit(event_type: str, status: str, agent_id: str, payload: dict[str, Any]) -> None:
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    app, sid = _ctx.active_app(), _ctx.active_session_id()
    if app is None or not sid:
        return  # a bare call outside a session: there is no highway to put it on
    _emit_semantic_event(
        app,
        sid,
        event_type,
        status=status,
        summary=f"{agent_id or 'agent'} {event_type.removeprefix('variant.')}",
        actor={"agent_id": agent_id, "role": "expert"},
        payload=payload,
    )


def _run_payload(run: VariantRun) -> dict[str, Any]:
    return {
        "variants_id": run.variants_id,
        "agent_id": run.agent_id,
        "origin": run.origin,
        "variant": run.strategy,
        "strategy": run.strategy,
        "judge": run.judge,
        "n": run.n,
        "rubric": run.rubric,
    }


def emit_try(run: VariantRun, record: TryRecord) -> None:
    """Put one try's start or end on the highway."""
    payload: dict[str, Any] = {
        **_run_payload(run),
        "try_index": record.try_index,
        "run_index": record.try_index,
        "scope": record.scope,
        "status": record.status,
    }
    if record.forked_from is not None:
        payload["forked_from"] = record.forked_from
    if record.advice:
        payload["advice"] = record.advice
    if record.status == "completed":
        payload["text"] = record.text
        payload["tokens"] = dict(record.tokens)
        if record.score is not None:
            payload["score"] = record.score
    elif record.status == "failed":
        payload["error"] = record.error
    _emit(VARIANT_TRY, _SEMANTIC_STATUS[record.status], run.agent_id, payload)


def emit_selected(run: VariantRun) -> None:
    """Put the selection on the highway: scores or the user's pick, and the chosen text."""
    assert run.selected_index is not None
    chosen = run.try_at(run.selected_index)
    scores = [
        {"run_index": t.try_index, "try_index": t.try_index, "score": t.score}
        for t in run.tries
        if t.score is not None
    ]
    payload: dict[str, Any] = {
        **_run_payload(run),
        "selected_index": chosen.try_index,
        "winning_index": chosen.try_index,
        "selected_scope": chosen.scope,
        "text": chosen.text,
        "scores": scores,
    }
    if chosen.score is not None:
        payload["winning_score"] = chosen.score
    if run.judge == "user":
        payload["pick"] = run.pick
        payload["comment"] = run.comment
    _emit(VARIANT_SELECTED, "completed", run.agent_id, payload)


def emit_closed(app: Any, session_id: str, run: VariantRun) -> None:
    """Put a run that closed without a pick on ``session_id``'s highway.

    Explicit ``app`` / ``session_id``: a run closes outside any turn (the cancel route,
    the question's deadline) as well as at a turn's start.
    """
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    payload: dict[str, Any] = {
        **_run_payload(run),
        "status": run.status,
        "reason": run.closed_reason,
        "question_id": run.question_id,
        "closed_at": run.closed_at,
        "candidates": [
            {"try_index": t.try_index, "scope": t.scope, "text": t.text}
            for t in run.tries
            if t.status == "completed"
        ],
    }
    if run.superseded_by_message_id:
        payload["superseded_by_message_id"] = run.superseded_by_message_id
    _emit_semantic_event(
        app,
        session_id,
        VARIANT_CLOSED,
        turn_id=run.turn_id,
        status="cancelled",
        summary=f"{run.agent_id or 'agent'} drafts {run.status}",
        actor={"agent_id": run.agent_id, "role": "expert"},
        payload=payload,
    )


def _delta_emitter(run: VariantRun, try_index: int) -> Any:
    """The coroutine a try's stream is redirected to: ``variant.try.delta`` events."""
    app, sid = _ctx.active_app(), _ctx.active_session_id()
    turn_id = _ctx.active_turn_id()

    async def emit_delta(text: str, agent_id: str | None, field: str) -> None:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        if app is None or not sid or not text:
            return
        thinking = field in {"reasoning", "next_thought"} or field.startswith("provider_thinking:")
        _emit_semantic_event(
            app,
            sid,
            VARIANT_TRY_DELTA,
            turn_id=turn_id,
            status="running",
            summary="variant try delta",
            actor={"agent_id": agent_id or run.agent_id, "role": "expert"},
            payload={
                "variants_id": run.variants_id,
                "try_index": try_index,
                "kind": "thinking" if thinking else "text",
                "delta": text,
            },
        )

    return emit_delta


def _tokens(tracker: Any) -> dict[str, int]:
    totals = {"input": 0, "output": 0, "total": 0}
    for usage in tracker.get_total_tokens().values():
        totals["input"] += int(usage.get("prompt_tokens") or 0)
        totals["output"] += int(usage.get("completion_tokens") or 0)
        totals["total"] += int(usage.get("total_tokens") or 0)
    return totals


@contextlib.contextmanager
def try_context(run: VariantRun, record: TryRecord) -> Iterator[None]:
    """Run one try: its run-keyed scope, its own stream tab, its token count.

    Inside, the ARC scope is ``<agent>#run<k>`` (``set_react_run``), streamed deltas go
    to ``variant.try.delta`` instead of the turn's answer lane, every semantic event is
    stamped with the try, and the try's token usage is tallied onto ``record.tokens``
    (and still reaches the enclosing usage tracker). The try's turn and, once it ends,
    the ids of its own segments are kept on ``record`` (its steps are served from them).
    """
    from clio_agent.gact.agents.variant_lines import own_ids  # noqa: PLC0415
    from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
        redirect_live_chunks,
        reset_live_chunks,
    )

    record.turn_id = _ctx.active_turn_id()
    run_token = _ctx.set_react_run(record.try_index)
    try_token = _ctx.set_variant_try(run.variants_id, record.try_index)
    stream_token = redirect_live_chunks(_delta_emitter(run, record.try_index))
    try:
        with dspy.track_usage() as tracker:
            try:
                yield
            finally:
                record.tokens = _tokens(tracker)
                record.segment_ids = own_ids(record.try_index, record.prefix_ids)
    finally:
        reset_live_chunks(stream_token)
        _ctx.reset_variant_try(try_token)
        _ctx.reset(run_token)
