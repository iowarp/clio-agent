"""Assemble turn output once, confirm Core recording, then publish and settle it.

Recording failures retain the completed message for owner-local recovery.
Execution failures retain their original error outcome.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.cas_gc import finalize_cas_budget_check
from clio_agent.gact.artifacts.grounding import ground_answer_artifacts
from clio_agent.gact.artifacts.minting import clear_turn_artifacts
from clio_agent.gact.artifacts.wire import append_turn_resource_links
from clio_agent.gact.delegation import _produced_turn_workflow_state
from clio_agent.gact.direct_response import promote_tool_free_response
from clio_agent.gact.enrichment import _finalize_context_frame
from clio_agent.gact.events import Event, EventBus, _publish_transcript_event
from clio_agent.gact.file_changes import stamp_turn_file_changes
from clio_agent.gact.hooks.answer import apply_answer_hooks
from clio_agent.gact.part_atom_minter import (
    close_turn_minter,
    failed_finalize_identity,
    persist_finalized_message,
)
from clio_agent.gact.runtime.globals import (
    _emit_semantic_event,
    _iso_from_epoch,
    _new_message_id,
    _new_part_id,
    _session_agent_id,
)
from clio_agent.gact.streaming import (
    _pop_stream_fallback,
    _pop_stream_fallback_notes,
    _stream_fallback_payload,
)
from clio_agent.gact.tool_observer import (
    _sanitize_handoff_tool_metadata,
    _sanitize_tools_called_metadata,
)
from clio_agent.gact.tool_result_reconcile import reconcile_tool_result_text
from clio_agent.gact.transcript_projection import final_message_embed
from clio_agent.gact.turn_settle_status import publish_turn_settled_status
from clio_agent.gact.turn_stream import assemble_stream_metadata, settle_turn_transcript
from clio_agent.gact.turn_usage import context_usage_metadata_patch, model_usage_after_turn
from clio_agent.gact.types import (
    ErrorInfo,
    Message,
    Part,
    Tokens,
)
from clio_agent.gact.usage import capture_reasoning_log
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from clio_agent.gact.turn_state import TurnState

logger = logging.getLogger(__name__)


def finalize_turn(
    state: "TurnState",
    pred: Any,
    *,
    drain_observed_tool_calls: "Callable[[list[dict[str, Any]]], list[dict[str, Any]]]",
    update_retry_attempt: "Callable[..., None]",
) -> bool:
    """Assemble the original assistant output, then record it before publication.

    Provider execution is already finished. Recording retry must never repeat
    grounding, artifact creation, prediction processing or tool execution.
    """

    from clio_agent.gact.app import _enrich_cancellation_error_info  # noqa: PLC0415

    # Final user-facing text only: correct any fabricated local artifact path the
    # answer presents as produced, by grounding it against the session's REGISTERED
    # artifacts (the designation truth: ids + content hashes, include_children reach),
    # scoped to the plugin schema's declared deliverable extensions. Applied once on
    # the assembled answer, never on child rows (S7 #973 — re-sourced from registry).
    if state.answer_text and state.expert_handoffs:
        state.answer_text = ground_answer_artifacts(
            state.app,
            state.sid,
            state.answer_text,
            schema=state.workflow_schema,
        )

    responder_agent_id = state.selected_agent or state.invocation_agent_id or "main"
    answer_agent_ids = {
        responder_agent_id,
        state.active_agent_id or state.invocation_agent_id or "main",
    } - {""}
    promote_tool_free_response(state.transcript, pred, answer_agent_ids)
    current_stream_part_id = state.transcript.current_stream_part_id
    live_assistant_parts = state.transcript.snapshot()
    has_live_parts = bool(live_assistant_parts or current_stream_part_id)
    # Task acceptance and other visible operations can be the whole segment's
    # output. Assignment disposition remains the conversation owner's decision.
    has_operation_output = any(
        part.type in {"tool_call", "tool_result", "expert_handoff"} for part in live_assistant_parts
    )
    if (
        state.error_info is None
        and not state.answer_text
        and not state.thinking_text
        and not state.nanoagents
        and not has_operation_output
        and getattr(pred, "assignment_disposition", None) != "yield"
    ):
        state.error_info = ErrorInfo(
            error="empty_response",
            message="Agent completed without user-visible output.",
            details={
                "session_id": state.sid,
                "routing_mode": getattr(state.sess, "routing_mode", "auto"),
                "selected_agent": state.selected_agent,
                "termination_reason": str(getattr(pred, "termination_reason", "") or ""),
            },
            recoverable=True,
        )
    reconcile_tool_result_text(live_assistant_parts, state.tools_called)

    # Take the canonical-answer channel FIRST: its exactly-once identity seeds
    # from the pre-append ledger (the still-open streamed answer part included).
    # It covers the responder PLUS the stream tap's attribution fallback label
    # (``emit_chunk``'s chat-path default) — the same top-level LM call's
    # answer can stream under either; a delegated child's channel is NOT
    # covered (its deliverable settled at its LM-call site and must never
    # suppress the responder's distinct final answer).
    answer_channel = state.transcript.turn_answer_stream(
        responder_agent_id,
        state.active_agent_id or state.invocation_agent_id or "main",
    )
    # Clean-wire rule (owner 2026-08-05): the routing decision is OBSERVABILITY,
    # never a transcript part — it rides the semantic highway. Same
    # ``route:{agent}`` once-key the live tool observer consumes, so the event
    # lands exactly once whether it fired live or lands here.
    if state.selected_agent and state.transcript.mark_part_key(f"route:{state.selected_agent}"):
        _emit_semantic_event(
            state.app,
            state.sid,
            "routing.decision",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="completed",
            summary=f"routed to {state.selected_agent}",
            actor={"agent_id": state.invocation_agent_id or "main", "role": "orchestrator"},
            subject={"selected_agent": state.selected_agent},
            payload={
                k: v
                for k, v in {
                    "selected_agent": state.selected_agent,
                    "rationale": state.rationale,
                    "route_source": state.route_source,
                    "route_reason": state.route_reason,
                    "execution_path": state.execution_path,
                }.items()
                if v
            },
        )
    # Mechanism 2 replaced: there is no finalize rebuild-from-rows — delegation
    # appended its expert_handoff parts once, at emit time; the
    # ``expert_handoffs`` rows stay message METADATA only (design §4 row 6).
    #
    state.transcript.close_open_text()
    if state.thinking_text and not state.transcript.has_closed_text(
        responder_agent_id, "reasoning"
    ):
        state.transcript.append_part(
            Part(
                id=_new_part_id(),
                type="thinking",
                agent_id=responder_agent_id,
                text=state.thinking_text,
            ),
            stream_source="batch",
        )
    # Mechanisms 4+5 replaced: the canonical turn answer settles its exactly-once
    # channel — when an ``answer``-field part already landed this turn (streamed
    # live and closed with its own cleaned buffer, or a terminal expert's batch
    # burst), the fallback is audited + ignored BY OP IDENTITY; otherwise ONE
    # batch added+completed burst lands now. Never both; never a text swap
    # (the streamed part's close already carried the cleaned buffer as
    # final_text — there is nothing to swap). The turn responder is a react main
    # whose ``answer`` IS the user deliverable, so its batch fallback always lands.
    stream_fallback = _pop_stream_fallback(state.app, state.sid)
    batch_turn_text = current_stream_part_id is None
    if (
        batch_turn_text
        and (bool(state.answer_text) or state.error_info is not None)
        and not stream_fallback
    ):
        stream_fallback = _stream_fallback_payload("sync_execution_path")
    landed_answer = answer_channel.finish(
        fallback_text=str(state.answer_text or ""),
        fallback_metadata=(
            {"stream_fallback": stream_fallback} if stream_fallback and batch_turn_text else {}
        ),
    )
    apply_answer_hooks(
        state,
        answer_text=landed_answer or "",
        answer_part_id=answer_channel.part_id or "",
        agent_id=responder_agent_id,
    )
    append_turn_resource_links(
        state.app, state.sid, state.turn_id, state.transcript, agent_id=responder_agent_id
    )
    # Owner ask 2026-08-06: roll up child/subagent mints too (owner module:
    # artifacts/wire.py) — a delegated child's artifacts otherwise never surface
    # on the parent's own settled message.
    from clio_agent.gact.artifacts.wire import append_turn_child_resource_links  # noqa: PLC0415

    append_turn_child_resource_links(
        state.app, state.sid, state.turn_id, state.transcript, agent_id=responder_agent_id
    )

    finalize_cas_budget_check(state.app, state.sess, state.sid)

    state.error_info = _enrich_cancellation_error_info(state.app, state.sid, state.error_info)
    state.cancelled_turn = state.error_info is not None and state.error_info.error == "cancelled"
    if state.cancelled_turn:
        state.app.state.cancel_flags.discard(state.sid)
        ledger = getattr(state.app.state, "tool_call_ledger", None)
        if ledger is not None:
            ledger.pop(state.sid, None)

    state.assistant_metadata = {}
    if state.turn_agent_id:
        state.assistant_metadata["agent_override"] = {
            "requested_agent_id": state.turn_agent_id,
            "session_agent_id": _session_agent_id(state.sess),
            "effective_agent_id": state.selected_agent or state.turn_agent_id,
            "scope": "turn",
        }
    # Stamp stream provenance (verbatim behaviour): mark the turn's text live vs
    # batch and, on a batch answer, record the delivery-path stream_fallback payload.
    assemble_stream_metadata(
        state,
        stream_fallback=stream_fallback,
        current_stream_part_id=current_stream_part_id,
        has_live_parts=has_live_parts,
    )
    # Degradations that did NOT change the delivery path (native model inputs the
    # executing agent could not accept) ride here rather than in the single
    # delivery slot above, which a clean live stream discards -- so a dropped
    # attachment stays on the turn's record instead of vanishing with it.
    stream_degradations = _pop_stream_fallback_notes(state.app, state.sid)
    if stream_degradations:
        state.assistant_metadata["stream_degradations"] = stream_degradations
    # A live observer completion can arrive after the immediate post-forward drain
    # but before the assistant message is persisted. Reconcile once more at the
    # final metadata boundary so reloads retain the same tool facts as the live bus.
    if not state.cancelled_turn:
        state.tools_called = drain_observed_tool_calls(state.tools_called)
    state.tools_called = _sanitize_tools_called_metadata(state.tools_called)
    if state.tools_called:
        state.assistant_metadata["tools_called"] = state.tools_called
    if state.expert_handoffs:
        state.expert_handoffs = [
            _sanitize_handoff_tool_metadata(row) if isinstance(row, Mapping) else row
            for row in state.expert_handoffs
        ]
        state.assistant_metadata["expert_handoffs"] = state.expert_handoffs
    produced_wf = _produced_turn_workflow_state(
        state.pred, state.expert_handoffs, state.app, state.sid, schema=state.workflow_schema
    )
    if produced_wf:
        state.assistant_metadata["workflow_state"] = produced_wf
    if state.context_file_provenance["files"]:
        state.assistant_metadata["context_files"] = state.context_file_provenance
    if state.memory_search_metadata:
        state.assistant_metadata["memory_search"] = state.memory_search_metadata
    if state.agent_runtime:
        state.assistant_metadata["agent_runtime"] = state.agent_runtime
    if state.prompt_resolution:
        state.assistant_metadata["prompt_resolution"] = state.prompt_resolution
    if getattr(state.pred, "variant_selection", None):
        state.assistant_metadata["variant_selection"] = state.pred.variant_selection
    # Reasoning capture: persist per-call chain-of-thought onto the assistant
    # message metadata (owner: usage.capture_reasoning_log). Best-effort, gated
    # by CLIO_CAPTURE_REASONING; mutates state.assistant_metadata in place.
    capture_reasoning_log(state)
    asst_id = state.transcript.ensure_message()
    assistant_parts = state.transcript.finalize()
    assistant_msg = Message(
        id=asst_id,
        # Correlate the assistant reply to the user-turn that produced it (#711).
        turn_id=state.turn_id,
        session_id=state.sid,
        role="assistant",
        created_at=_iso_from_epoch(time.time()),
        updated_at=_iso_from_epoch(time.time()),
        parts=assistant_parts,
        tokens=Tokens(**state.turn_tokens),
        cost_usd=state.turn_cost if state.turn_cost_known else None,
        stop_reason="cancelled"
        if state.cancelled_turn
        else ("error" if state.error_info else "end_turn"),
        error_info=state.error_info,
        metadata=state.assistant_metadata,
    )
    _finalize_context_frame(
        state.app,
        state.sid,
        state.context_frame["id"],
        assistant_msg.id,
        "cancelled" if state.cancelled_turn else ("error" if state.error_info else "completed"),
        error_info=state.error_info,
    )

    # Name the message holding this turn's file_diff parts on the change ledger rows.
    stamp_turn_file_changes(state.app, state.sid, assistant_parts, message_id=assistant_msg.id)

    completed_payload: dict[str, Any] = {
        "turn_id": state.turn_id,
        "message_id": assistant_msg.id,
        "stop_reason": "cancelled"
        if state.cancelled_turn
        else ("error" if state.error_info else "end_turn"),
        "tokens": dict(state.turn_tokens),
        "cost_usd": state.turn_cost if state.turn_cost_known else None,
    }
    if state.error_info is not None:
        completed_payload["error_info"] = state.error_info.model_dump(exclude_none=True)
    if state.assistant_metadata:
        completed_payload["metadata"] = state.assistant_metadata
    state.app.state.turn_transcripts.close(state.sid)
    return persist_finalized_message(
        state.app,
        state.sid,
        assistant_msg,
        after_confirmed=lambda: publish_finalized_turn(
            state, assistant_msg, completed_payload, update_retry_attempt=update_retry_attempt
        ),
        after_withdrawn=lambda: settle_withdrawn_turn(
            state.app, state.sid, state.turn_cancel_event, update_retry_attempt=update_retry_attempt
        ),
    )


def settle_withdrawn_turn(
    app: Any,
    sid: str,
    turn_cancel_event: threading.Event,
    *,
    update_retry_attempt: Callable[..., None] | None = None,
) -> None:
    """Retire a withdrawn turn's projections without publishing its discarded output."""
    if update_retry_attempt is not None:
        update_retry_attempt("cancelled", metadata_patch={"reason": "transcript_withdrawn"})
    if app.state.cancel_events.get(sid) is turn_cancel_event:
        app.state.cancel_events.pop(sid, None)
    with app.state.task_history.serialized():
        current = app.state.sessions.get(sid)
        if (
            current is not None
            and current.status == "waiting_user"
            and current.metadata.get("execution_gate")
        ):
            return  # A surfaced approval still owns this input gate.
        publish_turn_settled_status(
            app, sid, "idle", payload_extra={"reason": "transcript_withdrawn"}
        )


def publish_finalized_turn(
    state: "TurnState",
    assistant_msg: Message,
    completed_payload: dict[str, Any],
    *,
    update_retry_attempt: "Callable[..., None]",
) -> None:
    """Publish one already-recorded result; never rebuild the prediction or execute it again."""
    from clio_agent.gact.transcript_recording import publication_step

    steps = state.publication_steps
    final_status = "cancelled" if state.cancelled_turn else "error" if state.error_info else "idle"
    retry_status = (
        "cancelled" if state.cancelled_turn else "failed" if state.error_info else "completed"
    )
    settle_turn_transcript(state)
    getattr(state.app.state, "live_assistant_message_ids", {}).pop(state.sid, None)
    getattr(state.app.state, "live_assistant_parts", {}).pop(state.sid, None)
    getattr(state.app.state, "live_assistant_part_keys", {}).pop(state.sid, None)
    publication_step(
        steps,
        "retry_projection",
        lambda: update_retry_attempt(
            retry_status,
            metadata_patch={
                "executed_user_message_id": state.user_msg.id,
                "assistant_message_id": assistant_msg.id,
                "stop_reason": completed_payload["stop_reason"],
            },
        ),
        retry=True,
    )
    current_session = state.app.state.sessions.get(state.sid)
    metadata_patch = context_usage_metadata_patch(state, current_session, assistant_msg)
    if state.publication_usage is None:
        if current_session is None:
            raise ValueError("Finalized turn lost its session")
        state.publication_usage = (
            current_session.tokens_input + state.turn_tokens["input"],
            current_session.tokens_output + state.turn_tokens["output"],
            current_session.cost_usd + (state.turn_cost if state.turn_cost_known else 0.0),
            current_session.cost_known or state.turn_cost_known,
        )
        state.publication_model_usage = model_usage_after_turn(state, current_session)
    publication_step(
        steps,
        "session_projection",
        lambda: state.app.state.sessions.update(
            state.sid,
            message_count=len(state.app.state.messages.get(state.sid, [])),
            usage_totals=state.publication_usage,
            usage_by_model=state.publication_model_usage,
            metadata_patch=metadata_patch,
        ),
        retry=True,
    )
    semantic_completed_payload = {
        **completed_payload,
        **final_message_embed(state.app, state.sid, assistant_msg),
    }
    publication_step(
        steps,
        "semantic",
        lambda: _emit_semantic_event(
            state.app,
            state.sid,
            "turn.completed" if state.error_info is None else "turn.failed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="completed" if state.error_info is None else "failed",
            summary="CLIO turn completed."
            if state.error_info is None
            else f"CLIO turn failed: {state.error_info.error}.",
            actor={"agent_id": state.selected_agent or "orchestrator"},
            subject={"message_id": assistant_msg.id},
            payload=semantic_completed_payload,
        ),
        retry=True,
    )
    publication_step(
        steps,
        "transcript_event",
        lambda: _publish_transcript_event(
            state.bus, state.sid, "turn.completed", {"turn_id": state.turn_id}
        ),
        retry=False,
    )
    publication_step(
        steps,
        "completion_event",
        lambda: state.bus.publish(
            Event(type="message.completed", session_id=state.sid, payload=completed_payload)
        ),
        retry=False,
    )
    cancellation_status: dict[str, Any] = {}
    if state.cancelled_turn and state.error_info is not None:
        cancellation_status = {
            "execution_cancellation": state.error_info.details.get("execution_cancellation"),
            "cancellation_attempt": state.error_info.details.get("cancellation_attempt", {}),
        }
    publication_step(
        steps,
        "status",
        lambda: publish_turn_settled_status(
            state.app, state.sid, final_status, payload_extra=cancellation_status
        ),
        retry=True,
    )
    from clio_agent.gact.spotter_watcher import on_turn_finalized

    publication_step(steps, "spotter", lambda: on_turn_finalized(state.app, state.sid), retry=False)
    if state.tools_called:
        from clio_agent.gact.hooks import fire_post_tool_batch

        publication_step(
            steps,
            "tool_hook",
            lambda: fire_post_tool_batch(
                state.tools_called,
                session_id=state.sid,
                turn_id=state.turn_id,
                cwd=str(getattr(state.sess, "workspace_root", "") or ""),
            ),
            retry=False,
        )
    from clio_agent.gact.conversation_assignments import ConversationOutcome
    from clio_agent.gact.hooks.stop_loop import dispatch_stop_at_finalize

    publication_step(
        steps,
        "stop_hook",
        lambda: dispatch_stop_at_finalize(
            state.app,
            session_id=state.sid,
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            cwd=str(getattr(state.sess, "workspace_root", "") or ""),
            assistant_msg_id=assistant_msg.id,
            assistant_payload=assistant_msg.model_dump(exclude_none=True),
            agent_ref=str(state.dynamic_agent_used.metadata.get("agent_ref") or "")
            if state.dynamic_agent_used is not None
            else "",
            completion=ConversationOutcome(
                outputs=turn_outputs(state),
                context=getattr(state.pred, "clio_context_boundary", None),
                error=state.error_info.model_dump(mode="json")
                if state.error_info is not None
                else None,
            ),
        ),
        retry=False,
    )
    from clio_agent.gact.autonomous_loop import dispatch_loop_at_finalize

    publication_step(
        steps,
        "loop_hook",
        lambda: dispatch_loop_at_finalize(state.app, session_id=state.sid, turn_id=state.turn_id),
        retry=False,
    )
    from clio_agent.gact.replanning import dispatch_stall_monitor_at_finalize

    publication_step(
        steps,
        "stall_hook",
        lambda: dispatch_stall_monitor_at_finalize(
            state.app, session_id=state.sid, turn_id=state.turn_id, tools_called=state.tools_called
        ),
        retry=False,
    )
    if not (
        state.cancelled_turn
        and state.error_info is not None
        and (state.error_info.details.get("execution_cancellation") == "best_effort")
    ):
        if state.app.state.cancel_events.get(state.sid) is state.turn_cancel_event:
            state.app.state.cancel_events.pop(state.sid, None)


def settle_failed_finalize(
    app: "FastAPI",
    sid: str,
    *,
    turn_id: str,
    trace_id: str,
    turn_tokens: Mapping[str, int],
    turn_cost: float | None,
    turn_cancel_event: threading.Event,
    update_retry_attempt: "Callable[..., None]",
    exc: BaseException,
) -> None:
    """Record the original finalize error and preserve already-streamed parts.

    A recording outage retains this exact message. It does not create another
    execution failure or silently discard the output.
    """

    reason = getattr(exc, "settle_reason", "turn_finalize_error")
    logger.error(
        "turn finalize failed: reason=%s session=%s turn=%s error=%s",
        reason,
        sid,
        turn_id,
        type(exc).__name__,
        exc_info=exc,
    )
    if trace.HF_ON:
        trace.hot("TURN-FINALIZE-FAIL", "%s %s: %s", sid, type(exc).__name__, exc)

    asst_id, sealed_parts = failed_finalize_identity(app, sid)
    registry = getattr(app.state, "turn_transcripts", None)
    if registry is not None:
        transcript = registry.get(sid)
        if transcript is not None:
            transcript.abandon()
        registry.close(sid)

    clear_turn_artifacts(app, sid)

    sess = app.state.sessions.get(sid)
    if sess is not None and getattr(sess, "status", "") != "running":
        close_turn_minter(app, sid)
        return

    error_info = ErrorInfo(
        error=getattr(exc, "settle_error_code", "finalize_error"),
        message=f"turn finalize raised: {exc}",
        details={
            "reason": reason,
            "session_id": sid,
            "turn_id": turn_id,
            "original_error": type(exc).__name__,
            "stage": "finalize",
        },
        recoverable=True,
    )
    now = time.time()
    assistant_msg = Message(
        id=asst_id or _new_message_id("asst"),
        turn_id=turn_id,
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(now),
        updated_at=_iso_from_epoch(now),
        parts=sealed_parts,
        tokens=Tokens(**dict(turn_tokens)),
        cost_usd=turn_cost,
        stop_reason="error",
        error_info=error_info,
    )
    completed_payload: dict[str, Any] = {
        "turn_id": turn_id,
        "message_id": assistant_msg.id,
        "stop_reason": "error",
        "tokens": dict(turn_tokens),
        "cost_usd": turn_cost,
        "error_info": error_info.model_dump(exclude_none=True),
    }
    persist_finalized_message(
        app,
        sid,
        assistant_msg,
        after_confirmed=lambda: publish_failed_finalize(
            app,
            sid,
            assistant_msg,
            completed_payload,
            turn_id=turn_id,
            trace_id=trace_id,
            turn_cancel_event=turn_cancel_event,
            update_retry_attempt=update_retry_attempt,
        ),
        after_withdrawn=lambda: settle_withdrawn_turn(
            app, sid, turn_cancel_event, update_retry_attempt=update_retry_attempt
        ),
    )


def publish_failed_finalize(
    app: "FastAPI",
    sid: str,
    assistant_msg: Message,
    completed_payload: dict[str, Any],
    *,
    turn_id: str,
    trace_id: str,
    turn_cancel_event: threading.Event,
    update_retry_attempt: "Callable[..., None]",
) -> None:
    """Publish the already-recorded error outcome without rebuilding or re-executing it."""
    error_info = assistant_msg.error_info
    if error_info is None:
        raise ValueError("Failed-finalize output has no original error")
    bus: EventBus = app.state.bus
    sess = app.state.sessions.get(sid)
    try:
        _emit_semantic_event(
            app,
            sid,
            "turn.failed",
            turn_id=turn_id,
            trace_id=trace_id,
            status="failed",
            summary=f"CLIO turn failed: {error_info.error}.",
            actor={"agent_id": "orchestrator"},
            subject={"message_id": assistant_msg.id},
            payload={
                **completed_payload,
                **final_message_embed(app, sid, assistant_msg),
            },
        )
    except Exception:  # noqa: BLE001 - the bus publishes below must still go out
        logger.exception(
            "turn.failed semantic emit failed during finalize settle: session=%s turn=%s",
            sid,
            turn_id,
        )
    _publish_transcript_event(bus, sid, "turn.completed", {"turn_id": turn_id})
    bus.publish(
        Event(
            type="message.completed",
            session_id=sid,
            payload=completed_payload,
        )
    )
    close_turn_minter(app, sid)  # #1334: after the persist; the thread stops here
    try:
        update_retry_attempt(
            "failed",
            metadata_patch={
                "assistant_message_id": assistant_msg.id,
                "stop_reason": "error",
            },
        )
    except Exception:  # noqa: BLE001 - retry bookkeeping degraded; keep settling
        logger.exception(
            "retry-attempt update failed during finalize settle: session=%s turn=%s",
            sid,
            turn_id,
        )
    getattr(app.state, "live_assistant_message_ids", {}).pop(sid, None)
    getattr(app.state, "live_assistant_parts", {}).pop(sid, None)
    getattr(app.state, "live_assistant_part_keys", {}).pop(sid, None)
    if sess is not None:
        app.state.sessions.update(sid, message_count=len(app.state.messages.get(sid, [])))
    publish_turn_settled_status(app, sid, "error")
    if app.state.cancel_events.get(sid) is turn_cancel_event:
        app.state.cancel_events.pop(sid, None)


def turn_outputs(state: Any) -> dict[str, Any]:
    """The assignment result a turn records: its prediction summary and typed state.

    The typed ``workflow_state`` the turn produced rides on the result, so a flow
    merging its children's results (``ctx.gather``) reads it from the task.
    """

    from clio_agent.gact.messaging import _prediction_summary  # noqa: PLC0415

    outputs = (
        _prediction_summary(state.pred) if state.pred is not None else {"answer": state.answer_text}
    )
    if state.tools_called:
        # The turn's merged tool calls (the prediction's trace plus every call the
        # tool observer saw): a plan reads an editor's proposals from them.
        from clio_agent.tools.mcp_runtime import wire_value  # noqa: PLC0415

        outputs["tools_called"] = wire_value(list(state.tools_called), mode="gact_runtime")
    workflow_state = state.assistant_metadata.get("workflow_state")
    if isinstance(workflow_state, Mapping) and workflow_state:
        outputs["workflow_state"] = dict(workflow_state)
    return outputs
