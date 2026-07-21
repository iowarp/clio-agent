"""Live SSE stream emitter for the GACT turn engine (#767 Phase B).

Slice 3 of the ``turn.py`` decomposition: the streamed-token emitter that used to
live inline in ``_run_turn_in_background`` as a set of closures moves here as free
functions taking :class:`~clio_agent.gact.turn_state.TurnState` first (the gact
seam convention).

The emitter is behavior-preserving. :func:`emit_chunk` is a thin adapter over the
``TurnTranscript`` ledger (#767 PR2): it emits the semantic ``lm.token.delta``
event, records stream audit, and makes ONE transcript call that owns the
streamed-part state machine.

TRICKY #1 (Phase B spec): the live chunk emitter is *bound* (via
:func:`bind_live_emitter`) BEFORE the forward seam resolves the turn's agents, and
it reads ``state.active_agent_id`` / ``state.invocation_agent_id`` *late* whenever
a chunk arrives mid-forward. Correctness requires the orchestrator to MUTATE those
fields in place at the set points (never rebind a fresh local), so the already
bound ``partial(emit_chunk, state)`` sees them. This module only READS them off
``state``, so binding a ``partial`` over ``state`` reproduces the closure's
late-binding semantics exactly (verified by the ``streamed_text_turn.json``
golden staying byte-identical).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.runtime.globals import _emit_semantic_event, _llm_provider_payload
from clio_agent.gact.tool_observer import _mirror_transcript_state
from clio_agent.gact.transcript import _transcript_text_field
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    import asyncio

    from clio_agent.gact.turn_state import TurnState


def _feed_live_edge(
    state: "TurnState",
    part_id: str,
    agent_id: str,
    field: str,
    is_thinking: bool,
    text: str,
) -> None:
    """Coalesce one streamed delta into the #737 S7 live-edge head slot (best-effort).

    A no-op unless the live edge is engaged for the session (flag + atoms regime); it
    reads the part id the transcript just opened and grows the identity-stable slot in
    place. The overlay is a read-model view, never authoritative, so a failure here must
    never fail a turn — ``feed_delta`` swallows-but-logs internally.
    """

    from clio_agent.gact.live_edge import feed_delta  # noqa: PLC0415 - avoid import cycle

    feed_delta(
        state.app,
        state.sid,
        part_id=part_id,
        agent_id=agent_id,
        field=field,
        kind="thinking" if is_thinking else "text",
        chunk=text,
    )


def settle_turn_transcript(state: "TurnState") -> None:
    """Retire the turn's ledger: freeze (no-op after finalize), close.

    ``abandon()`` freezes without publishing so late producer ops are rejected +
    audited instead of silently absorbed into the next turn; on the success path
    ``transcript.finalize()`` already froze the ledger. Runs on EVERY turn exit
    path (success, ask_user early return; the #756 error envelope settles through
    ``_settle_failed_finalize``).
    """

    state.transcript.abandon()
    # #737 S7: seal the live-edge slot against the finalized parts (the in-process
    # byte-match check) and drop the ephemeral checkpoint lane. A no-op unless engaged.
    from clio_agent.gact.live_edge import seal_and_settle  # noqa: PLC0415 - avoid import cycle

    seal_and_settle(state.app, state.sid, state.transcript.snapshot())
    state.app.state.turn_transcripts.close(state.sid)


async def emit_chunk(
    state: "TurnState",
    text: str,
    agent_id: Optional[str] = None,
    field_name: str = "answer",
) -> None:
    """Publish one streamed token delta through the turn's transcript ledger.

    Thin adapter over the ``TurnTranscript`` (#767 PR2): emits the semantic
    ``lm.token.delta`` event, records stream audit, and makes ONE transcript call
    that mints the message on first arrival, opens/splits parts per
    ``(agent, field)``, cleans the buffer at close, and publishes
    ``message.created`` / ``part.added`` / ``part.delta``.

    Reads ``state.active_agent_id`` / ``state.invocation_agent_id`` LATE (TRICKY
    #1): the emitter is bound before the forward seam resolves them, so it must
    resolve the generating agent off ``state`` on each call.
    """

    # The generating expert (passed by the LM token tap from its react scope);
    # falls back to the turn's selected/invocation agent for the chat path.
    chunk_agent = agent_id or state.active_agent_id or state.invocation_agent_id or "main"
    stream_field = str(field_name or "answer")
    is_provider_thinking = stream_field.startswith("provider_thinking:")
    try:
        from clio_agent.gact.semantic_events import (  # noqa: PLC0415
            LM_TOKEN_DELTA,
            lm_token_delta_payload,
        )

        _emit_semantic_event(
            state.app,
            state.sid,
            LM_TOKEN_DELTA,
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="running",
            summary="LM token delta.",
            actor={"agent_id": chunk_agent, "role": "expert"},
            provider=_llm_provider_payload(state.app, chunk_agent),
            payload=lm_token_delta_payload(content=text, field=stream_field),
            # Capture/derive through ARC-as-source without adding a second
            # served transcript event; message.part.delta remains the UI stream.
            detail_level="off",
        )
    except Exception:  # noqa: BLE001,S110 - transcript streaming must not fail a turn
        pass
    if not text:
        trace.HF_ON and trace.hot(
            "STREAM-SSE",
            "ignored_empty_delta agent=%s field=%s",
            chunk_agent,
            stream_field,
        )
        return
    # ONE transcript call: mints the message id on first arrival, opens/
    # splits parts per (agent, field), cleans the whole buffer once at
    # close, and publishes message.created/part.added/part.delta — the state
    # machine that used to live here.
    state.transcript.append_text_delta(chunk_agent, stream_field, text)
    if state.transcript.frozen:
        # Settled turn: the ledger rejected + audited this late chunk.
        # Do NOT mirror — re-populating the popped legacy dicts would hand
        # the dead turn's identity to the next turn's carried-state
        # adoption (the poison class the settle exists to prevent).
        return
    _mirror_transcript_state(state.app, state.sid, state.transcript)
    stream_part_id = state.transcript.current_stream_part_id or ""
    # #737 S7: coalesce this delta into the mutable live-edge head slot so a mid-stream
    # canonical-log read sees the growing edge (surface 1.3). A no-op unless the live
    # edge is engaged (flag + atoms regime); the log gains no per-token atom (shape i).
    _feed_live_edge(state, stream_part_id, chunk_agent, stream_field, is_provider_thinking, text)
    stream_audit(
        "sse.normalized_emit",
        session_id=state.sid,
        turn_id=state.turn_id,
        agent_id=chunk_agent,
        part_id=stream_part_id,
        field=stream_field,
        **(
            {}
            if is_provider_thinking
            else {"transcript_field": _transcript_text_field(stream_field)}
        ),
        normalized_event=("turn.trace.delta" if is_provider_thinking else "turn.text.delta"),
        chunk_len=len(text),
        duplicate_suppressed=False,
        head=text[:120],
        full_text=text[:12000],
    )
    trace.HF_ON and trace.hot(
        "STREAM-SSE",
        "published_delta sid=%s msg=%s part=%s agent=%s field=%s len=%d head=%r",
        state.sid,
        state.transcript.message_id,
        stream_part_id,
        chunk_agent,
        stream_field,
        len(text),
        text[:80],
    )


def bind_live_emitter(state: "TurnState", loop: "asyncio.AbstractEventLoop") -> None:
    """Bind this turn's loop + chat publisher onto the unified LM token highway (#693).

    Binds ``partial(emit_chunk, state)`` so a blueprint/expert LM call streamed in
    an executor thread feeds the SAME emitter — one streaming path for chat AND
    blueprint turns, instead of the old executor drain-and-discard. The executor
    inherits this binding via the ``contextvars.copy_context()`` at the forward
    sites. Best-effort: live-stream wiring must never fail a turn.

    TRICKY #1: the partial closes over ``state``, so late in-place mutations of
    ``state.active_agent_id`` / ``state.invocation_agent_id`` by the forward seam
    are visible to chunks that arrive after binding.
    """

    try:
        from clio_agent.runtime.lm_activity import set_live_chunk_emitter  # noqa: PLC0415

        # Pass the transcript's SYNCHRONOUS tap-dedup recorder alongside the async
        # emitter (#732): the tap records the streamed field text in-thread before
        # scheduling the cross-thread emit, so the same-thread tool observer's
        # thought-dedup gate has a race-free source. Bound method over the turn's
        # transcript, so it is naturally turn-scoped and dies with the turn.
        set_live_chunk_emitter(
            loop,
            partial(emit_chunk, state),
            state.transcript.record_streamed_field_text,
        )
    except Exception:  # noqa: BLE001,S110 - live-stream wiring is best-effort
        pass


def assemble_stream_metadata(
    state: "TurnState",
    *,
    stream_fallback: dict[str, Any],
    current_stream_part_id: str | None,
    has_live_parts: bool,
) -> None:
    """Stamp the turn's stream provenance onto the assistant message metadata.

    ``current_stream_part_id`` (captured BEFORE the finalize appends) keeps the
    legacy semantic: a text part opened since the last mid-turn runtime boundary
    marks the turn's text as live-streamed even after it closed. Per-part
    ``stream_source`` is no longer restamped here -- every part carries the
    provenance its producer appended it with (#767 PR3: finalize never rewrites
    the ledger). A batch answer with a real ``stream_fallback`` payload records
    it so the delivery-path downgrade is queryable (no-silent-fallback).

    (Formerly ``assemble_stream_and_degradation_metadata`` in the retired
    ``turn_degradation`` module; the turn-degradation ledger it also drained was
    deleted in #948 S4 when its sole reason -- an answer substituted from settle
    evidence -- was removed with the settle/synthesis layer.)

    Args:
        state: The active turn state (mutated: ``assistant_metadata`` is written).
        stream_fallback: The popped single-slot stream-fallback payload for the turn.
        current_stream_part_id: The pre-append open streamed text part id (or ``None``).
        has_live_parts: Whether any live part streamed this turn.
    """

    should_report_stream_provenance = (
        bool(state.answer_text) or state.error_info is not None or has_live_parts
    )
    text_stream_source = ""
    if bool(state.answer_text) or state.error_info is not None:
        text_stream_source = "live" if current_stream_part_id is not None else "batch"
    elif has_live_parts:
        text_stream_source = "live"
    if should_report_stream_provenance and text_stream_source:
        state.assistant_metadata["stream_source"] = text_stream_source
    if text_stream_source == "batch" and (bool(state.answer_text) or state.error_info is not None):
        state.assistant_metadata["stream_fallback"] = stream_fallback
