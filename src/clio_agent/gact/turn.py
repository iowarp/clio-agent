"""Conversation segment execution and confirmed producer input acceptance.

TurnRunner owns admission and physical worker retirement. This module prepares
and executes an admitted segment, returning its explicit outcome to the
conversation owner. Input acceptance persists typed messages into Core mail;
it never starts a second turn or acknowledges model delivery.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

from clio_agent.errors import ClioError
from clio_agent.flows.outcomes import FlowOutcome
from clio_agent.gact.agents.resolution import SessionAgentUnavailable
from clio_agent.gact.context_reference_delivery import record_context_reference_deliveries
from clio_agent.gact.delegation import (
    _coerce_expert_handoff_rows,
    _prediction_workflow_state,
)
from clio_agent.gact.events import Event, EventBus, _publish_transcript_event
from clio_agent.gact.messaging import (
    _agent_accepts_images,
    _image_part_summaries,
    _user_message_parts,
)
from clio_agent.gact.off_loop import emit_semantic_event_async
from clio_agent.gact.permission_delivery import publish_permission_event
from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.runtime.globals import (
    _cancelled_error_info,
    _coerce_error_info,
    _ContextFileAccessError,
    _iso_from_epoch,
    _new_message_id,
    _session_agent_id,
    _TurnCancelled,
    _TurnTimedOut,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.session_store import _append_session_message
from clio_agent.gact.stream_failures import forward_error_info
from clio_agent.gact.streaming import _extract_tools_called, _pop_stream_fallback
from clio_agent.gact.tool_observer import (
    _merge_tool_call_rows,
    _tool_calls_from_handoff_rows,
)
from clio_agent.gact.turn_cancellation import settle_asyncio_cancellation
from clio_agent.gact.turn_forward import _run_turn_setup_off_loop, forward_turn
from clio_agent.gact.turn_prologue_guard import run_finalize_or_settle_prologue_gap
from clio_agent.gact.turn_start_offloop import prepare_turn_off_loop
from clio_agent.gact.turn_state import DeferredTranscriptJob, new_turn_state
from clio_agent.gact.turn_stream import bind_live_emitter, settle_turn_transcript
from clio_agent.gact.turn_usage import roll_up_usage
from clio_agent.gact.turn_watchdog import make_turn_cancel_event
from clio_agent.gact.types import ErrorInfo, Message, Part, Session
from clio_agent.gact.usage import _snapshot_lm_history_index
from clio_agent.runtime import turn_lm_ledger
from clio_agent.tools.mcp_runtime import wire_value

# NOTE (#714): every turn helper above is imported from its true *leaf* owner,
# not from ``clio_agent.gact.app``. The turn loop originally lived in ``app.py``
# and resolved these through the ``app`` re-export shims; only the agent-builder
# seams + ``_enrich_cancellation_error_info`` (the "danger set" that tests
# monkeypatch on ``clio_agent.gact.app``) are still read back through ``app`` at call time — via a function-local import at
# their single call site — to preserve that monkeypatch contract with zero test
# edits (``stage_intent_user_message`` keeps that contract for the user-message
# persist seam). Those app imports are function-local, so the graph stays acyclic.

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.turn_state import TurnState  # noqa: F401
    from clio_agent.gact.types import AgentDef  # noqa: F401

logger = logging.getLogger(__name__)


def _mint_pack_declared_artifacts(state: "TurnState", workflow_state: dict[str, Any]) -> None:
    """Register pack-declared ``workflow_state.artifact_paths`` at finalize (seam c).

    Secondary/optional designation channel (#966 S1) — never load-bearing.
    Best-effort with a typed reason: an artifact mint must never break the turn.
    """
    try:
        from clio_agent.gact.artifacts.minting import mint_pack_declared_paths  # noqa: PLC0415

        mint_pack_declared_paths(
            state.app,
            state.sid,
            workflow_state=workflow_state,
            path_specs=getattr(state.workflow_schema, "artifact_paths", ()),
            workspace_id=str(getattr(state.sess, "workspace_id", "") or ""),
            turn_id=state.turn_id,
            trace_id=state.trace_id,
        )
    except Exception:  # noqa: BLE001 — a live artifact mint must never break a turn
        logger.warning(
            "artifact mint skipped reason=pack_declared_seam_failed session=%s", state.sid
        )


async def _apply_turn_prediction(
    state: "TurnState",
    drain_observed_tool_calls: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    update_retry_attempt: Callable[..., None],
) -> bool:
    """Project a provider result and preserve input gates before finalization."""
    state.answer_text = getattr(state.pred, "answer", "")
    state.selected_agent = getattr(state.pred, "selected_expert", "") or ""
    state.rationale = getattr(state.pred, "routing_rationale", "")
    state.route_source = getattr(state.pred, "route_source", "") or ""
    state.route_reason = getattr(state.pred, "route_reason", "") or state.rationale
    pred_error_info = _coerce_error_info(getattr(state.pred, "error_info", None))
    if pred_error_info is not None:
        if pred_error_info.error == "cancelled":
            pred_error_info.details.setdefault("session_id", state.sid)
        state.error_info = pred_error_info
        if not state.error_info.details.get("partial", False):
            state.answer_text = ""
    # Ordinary Questions are immediate tools. Plan exit retains its explicit
    # human approval gate and yields through the same conversation owner.
    from clio_agent.gact.plan_mode import maybe_pause_for_plan_exit  # noqa: PLC0415
    from clio_agent.gact.transcript_recording import TranscriptRecordingPending, resume_recording

    try:
        if await _run_turn_setup_off_loop(
            state,
            lambda: maybe_pause_for_plan_exit(state, update_retry_attempt=update_retry_attempt),
        ):
            return False
    except TranscriptRecordingPending as pending:
        try:
            await resume_recording(state.app, pending)
        except asyncio.CancelledError:
            # Recovery owns this immutable paused output. Stop retires this
            # waiter; it must not enter normal finalization with a second output.
            pass
        return False
    # iowarp/clio-agent#25: data branch reports which execution
    # path it took ("fast" or "expert_loop"). Empty when not
    # populated by ClioAgent.forward (older code paths, non-data
    # branches not yet migrated).
    state.execution_path = getattr(state.pred, "execution_path", "") or ""
    state.tools_called = _extract_tools_called(state.pred)
    top_level_workflow_state = _prediction_workflow_state(state.pred, schema=state.workflow_schema)
    if top_level_workflow_state:
        _publish_transcript_event(
            state.bus,
            state.sid,
            "state.updated",
            {
                "turn_id": state.turn_id,
                "value": top_level_workflow_state,
                "visibility": "hidden",
            },
        )
        await asyncio.to_thread(_mint_pack_declared_artifacts, state, top_level_workflow_state)
    raw_handoffs = getattr(state.pred, "expert_handoffs", None) or []
    if not state.expert_handoffs:
        state.expert_handoffs = _coerce_expert_handoff_rows(raw_handoffs)
    state.tools_called = _merge_tool_call_rows(
        state.tools_called,
        _tool_calls_from_handoff_rows(state.expert_handoffs),
    )
    # Drain the per-session observer ledger so direct-tool short-
    # circuits (HDF5/Parquet/fs experts that bypass ReAct) still
    # report tools_called on the assistant message metadata.
    state.tools_called = drain_observed_tool_calls(state.tools_called)
    # iowarp/clio-agent#17 — a ChainOfThought prediction's ``.reasoning``
    # becomes the turn's `thinking` Part.
    state.thinking_text = str(getattr(state.pred, "reasoning", "") or "")
    # cost + token rollup — mutate
    # state.turn_tokens / state.turn_cost from the prediction
    # or the per-turn LM history slice (see turn_usage.py).
    roll_up_usage(state, state.pred)
    state.nanoagents = list(getattr(state.pred, "nanoagents_spawned", None) or [])
    for req in getattr(state.pred, "permissions_requested", None) or []:
        src = (
            req
            if isinstance(req, dict)
            else {
                "tool_call": getattr(req, "tool_call", {}),
                "summary": getattr(req, "summary", ""),
                "id": getattr(req, "id", ""),
            }
        )
        pid = src.get("id") or f"perm_{uuid.uuid4().hex[:12]}"
        row = {
            "id": pid,
            "session_id": state.sid,
            "tool_call": src.get("tool_call") or {},
            "summary": src.get("summary", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        state.app.state.permissions[pid] = row
        enforce_dict_bound(
            state.app, state.app.state.permissions, "permissions", session_id=state.sid
        )
        await emit_semantic_event_async(
            state.app,
            state.sid,
            "permission.requested",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="pending",
            summary="Tool execution requested user permission.",
            actor={"agent_id": state.selected_agent or state.invocation_agent_id},
            subject={"permission_id": pid},
            payload=row,
        )
        publish_permission_event(
            state.app,
            "permission.requested",
            owner_session_id=state.sid,
            payload=row,
        )
    if state.sid in state.app.state.cancel_flags:
        state.app.state.cancel_flags.discard(state.sid)
        state.error_info = _cancelled_error_info(state.sid, execution_cancellation="turn_boundary")
        state.answer_text = ""
        state.tools_called = []
    return True


async def _run_turn_in_background(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
    turn_agent_id: str = "",
    transcript_job: Optional[DeferredTranscriptJob] = None,
) -> Any:
    """Drive an agent turn off the request thread.

    The POST handler returns immediately after staging the user
    message; this coroutine handles the rest: invoking forward() in
    an executor, slicing the result into Parts, publishing every
    SSE event the TUI consumes, persisting the assistant message,
    and settling the session back to idle (or error).

    Errors here are *consumed* — they emit a message.completed with
    error_info and a session.status_changed → error so the TUI sees
    the failure live. We never re-raise; the request that started us
    is long gone.
    """
    bus: EventBus = app.state.bus
    sess = app.state.sessions.get(sid)
    if sess is None:
        # Session evaporated between POST + background start; can't
        # do anything useful. Don't raise — the publishing path
        # would crash and pollute logs with no client to notify.
        return
    # #1215 S5: first-turn bring-up phase from "the background task started
    # running" to "turn.started published" (see the end_phase call below).
    bringup_timing.timer_for_session(app, sid).start_phase("turn.accept_gap")

    # #767 Phase B: the turn's whole working set lives on one mutable ``TurnState``
    # threaded through the closures + body (formerly ~40 function-scope locals).
    # ``new_turn_state`` runs the former inline init: retry-attempt id, turn/trace
    # ids, the turn-identity contextvar bind, and native-image pre-extraction.
    state = new_turn_state(
        app,
        sid,
        user_text,
        user_msg,
        turn_agent_id,
        sess=sess,
        bus=bus,
    )
    state.transcript_job = transcript_job

    def _drain_observed_tool_calls(
        current_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge completed live-observer tool calls into turn metadata rows."""

        ledger = getattr(state.app.state, "tool_call_ledger", None)
        if ledger is None:
            return current_rows
        observed = ledger.pop(state.sid, [])
        if not observed:
            return current_rows
        return _merge_tool_call_rows(current_rows, observed)

    def _update_retry_attempt(
        status: str,
        *,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> None:
        if not state.retry_attempt_id:
            return
        attempt = state.app.state.turn_attempts.get(state.retry_attempt_id)
        if attempt is None:
            return
        metadata = dict(attempt.metadata)
        if metadata_patch:
            metadata.update(metadata_patch)
        updated = attempt.model_copy(
            update={
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata,
            }
        )
        state.app.state.turn_attempts[state.retry_attempt_id] = updated
        enforce_dict_bound(
            state.app, state.app.state.turn_attempts, "turn_attempts", session_id=state.sid
        )
        state.app.state.bus.publish(
            Event(
                type=f"turn.retry_{status}",
                session_id=state.sid,
                payload=updated.model_dump(exclude_none=True),
            )
        )

    if state.retry_attempt_id:
        _update_retry_attempt(
            "running",
            metadata_patch={"executed_user_message_id": state.user_msg.id},
        )
    # iowarp/clio-agent#6: per-token streaming rides the LM token hooks while the module
    # runs; message.part.delta events land as chunks arrive.
    #
    # #767 PR2: the TurnTranscript ledger owns the streamed-part state machine (lazy message mint,
    # per-(agent, field) part open/close, per-part buffers, whole-buffer clean at close, the runtime
    # boundary). The turn loop owns the ledger's LIFECYCLE: opened here, settled on every exit path
    # (success, the #756 finalize error envelope, the ask_user early return).
    # :func:`~clio_agent.gact.turn_stream.emit_chunk` is a thin adapter: semantic lm.token.delta +
    # the parent-resume suppression gate + stream_audit, then one transcript call.
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        session_workflow_state_schema,
    )
    from clio_agent.gact.tool_observer import _open_turn_transcript  # noqa: PLC0415

    # #767 Phase C: resolve the turn's plugin workflow_state schema at the one turn
    # seam and carry it on ``state`` for every delegation/grounding/scrub site.
    state.workflow_schema = session_workflow_state_schema(state.app, state.sid)
    state.transcript = _open_turn_transcript(state.app, state.sid, state.turn_id)
    # TRICKY #1 (Phase B spec): bind the emitter over ``state`` so its LATE reads
    # of state.active_agent_id / state.invocation_agent_id see the forward seam's
    # IN-PLACE mutations.

    # Unified LM token highway (#693): bind this turn's loop + chat publisher so a
    # agent or subagent LM call streamed in an executor thread feeds the SAME
    # emitter — one streaming path for chat AND agent turns, instead of the
    # old executor drain-and-discard. The executor inherits this binding via the
    # contextvars.copy_context() at the forward sites below.
    bind_live_emitter(state, asyncio.get_running_loop())

    # iowarp/clio-agent#8: snapshot LM history before the turn so we
    # can sum every call this turn made. ContextVars don't propagate
    # to asyncio executor threads (so dspy.settings.usage_tracker is
    # unreliable from worker threads), but ``lm.history`` IS shared
    # across threads — list.append under the GIL gives us a clean,
    # thread-safe ledger. We diff history[start:end] post-turn. The LMs the
    # forward builds itself (one per expert forward) join the turn LM ledger.
    lm_ledger_token = turn_lm_ledger.open_ledger()
    state.history_start = _snapshot_lm_history_index(state.app)
    _pop_stream_fallback(state.app, state.sid)
    # #767 Phase B Slice 5: mint + register the cancel event and derive the
    # no-progress watchdog cadence onto ``state`` (formerly the inline setup
    # block); the watchdog itself is now the free functions
    # ``cancel_requested`` / ``await_turn_work`` in ``turn_watchdog.py`` that the
    # forward + delegation seams drive off ``state``.
    make_turn_cancel_event(state)

    try:
        # #1334: the whole prologue (the user message's deferred transcript persist, the
        # ``turn.started`` event, enrichment incl. a memory-search RPC, the context
        # frame, the UserPromptSubmit hooks) runs on the turn executor -- owner module
        # ``turn_start_offloop.py``. Inside the try: a cancellation or a crash during it
        # (a mint that fails) settles through the handlers below, never escaping the
        # detached task; a non-"proceed" hook outcome ends the turn as before.
        outcome = await _run_turn_setup_off_loop(
            state, lambda: prepare_turn_off_loop(state, update_retry_attempt=_update_retry_attempt)
        )
        if outcome != "proceed":
            settle_turn_transcript(state)
            from clio_agent.gact.conversation_assignments import ConversationOutcome

            return ConversationOutcome({}, disposition="yield", input_hold="hook_" + outcome)
        if state.context_file_error is not None:
            raise _ContextFileAccessError(state.context_file_error)

        if state.sid in state.app.state.cancel_flags:
            state.app.state.cancel_flags.discard(state.sid)
            raise _TurnCancelled(
                _cancelled_error_info(state.sid, execution_cancellation="turn_boundary")
            )

        # Re-read the shared mailbox after the prologue's abort/veto checks. This
        # stages content only; the Core recipient-input boundary acknowledges it
        # immediately before each model call, including subsequent iterations.

        # #767 Phase B Slice 5: agent resolve -> module build -> streamed/sync
        # forward (the agent's flow) lives in ``turn_forward.py``.
        # It sets state.active_agent_id / invocation_agent_id (IN PLACE, TRICKY
        # #1) / agent_runtime / prompt_resolution / dynamic_agent_used /
        # expert_handoffs and returns the prediction (TRICKY #2: pred is a seam
        # return value, ``state.pred = forward_turn(state)``).
        state.pred = await forward_turn(state)
        # #1215 S5: bring-up as far as instrumented (fleet.mount not wired yet,
        # #1215 stays open) ends here on the success path; a failed first turn's
        # still-open timer settles later via the registry's LRU eviction.
        bringup_timing.finish_bringup(state.app, state.sid)

        if not await _apply_turn_prediction(
            state, _drain_observed_tool_calls, _update_retry_attempt
        ):
            turn_lm_ledger.close_ledger(lm_ledger_token)
            return
    except _TurnCancelled as exc:
        state.error_info = exc.error_info
        state.answer_text = ""
        state.tools_called = []
    except asyncio.CancelledError:
        settle_asyncio_cancellation(state)
    except _TurnTimedOut as exc:
        partial_answer = state.transcript.raw_streamed_text()
        partial_output = bool(partial_answer)
        state.error_info = ErrorInfo(
            error="provider_timeout",
            message=f"agent turn made no progress for {exc.timeout_s:g}s",
            details={
                "session_id": state.sid,
                "no_progress_timeout_s": exc.timeout_s,
                "timeout_s": exc.timeout_s,
                "partial_output": partial_output,
                "execution_cancellation": "best_effort",
                "recovery_actions": [
                    "retry",
                    "increase_turn_timeout",
                    "reconfigure_provider",
                    "exit",
                ],
            },
            recoverable=True,
        )
        state.answer_text = partial_answer
        state.tools_called = []
    except FlowOutcome as exc:
        # A flow ended on a typed outcome it did not handle (a failed gather, a
        # limit, a stall, an uncertain task): the reason is the error, with the
        # handle and details, never a guessed answer.
        state.rationale = f"The agent's flow ended with {exc.reason}."
        state.error_info = ErrorInfo(
            error=exc.reason,
            message=str(exc),
            details=wire_value(exc.to_dict(), mode="gact_runtime"),
            recoverable=True,
        )
        state.answer_text = ""
        state.tools_called = []
    except SessionAgentUnavailable as exc:
        # The session's agent does not resolve (a plugin missing, untrusted,
        # colliding, or the agent uninstalled). Typed, with every resolution
        # error -- never a substitute agent.
        state.selected_agent = exc.ref
        state.rationale = "The session's agent could not be resolved from its plugins."
        state.error_info = ErrorInfo(
            error="agent_unavailable",
            message=str(exc),
            details={
                "agent_ref": exc.ref,
                "reason": exc.reason,
                "errors": exc.errors,
                "recovery_actions": [
                    "install_missing_plugin",
                    "trust_marketplace",
                    "remove_plugin_from_agent",
                    "choose_another_agent",
                ],
            },
            recoverable=True,
        )
        state.answer_text = ""
        state.tools_called = []
    except _UnsupportedSessionAgent as exc:
        state.selected_agent = exc.agent_id
        state.rationale = (
            "Session selected an agent that is registered but not executable "
            "by CLIO's current runtime."
        )
        # #1237: name the SERVER + typed reason when the unavailability is a
        # declared tool's server failing an on-demand mount attempt (never a
        # standing fact -- the next turn re-attempts), rather than leaving the
        # cause visible only in a cache listing.
        mount_failures = getattr(exc, "mount_failures", None) or {}
        message = f"Session agent {exc.agent_id!r} cannot be executed yet."
        if mount_failures:
            named = "; ".join(
                f"{namespace} unavailable: server mount failed reason={reason}"
                for namespace, reason in sorted(mount_failures.items())
            )
            message = f"{message} {named}."
        state.error_info = ErrorInfo(
            error="not_implemented",
            message=message,
            details={
                "agent_id": exc.agent_id,
                "reason": exc.reason,
                "unsupported_tools": exc.tools,
                "mount_failures": mount_failures,
                "recovery_actions": [
                    "choose_another_agent",
                    "remove_custom_agent_tools",
                    "retry",
                    "exit",
                ],
            },
            recoverable=True,
        )
        state.answer_text = ""
        state.tools_called = []
    except _ContextFileAccessError as exc:
        state.error_info = exc.error_info
        state.answer_text = ""
        state.tools_called = []
    except ClioError as exc:
        # #1282 F5 (#1275 ask 2): a typed clio error -- a protocol refusal
        # chief among them -- reaches the parent with its own reason/details
        # (json_rpc_code, protocol_data naming what to re-dial with) instead
        # of the generic agent_error branch below erasing them.
        #
        # N4 (re-verify round): the TOP-LEVEL error field stays "agent_error"
        # -- the SAME value the generic branch below already uses -- rather
        # than exc.to_dict()["error"] (e.g. "mcp_missing_required_client_
        # capability"), which is a CLIO-internal error_type, not a member of
        # the wire taxonomy the client renders; an unrecognized code there
        # falls back to a bare "Internal error". details still carries the
        # FULL to_dict() payload -- json_rpc_code, protocol_data, and
        # details["reason"], which is what actually matters structurally:
        # turn_spawn_failures.child_task_error_reason reads details["reason"]
        # (never the top-level error) to project the typed reason onto a
        # spawned child's AgentTask record (agent_tasks.ERROR_REASONS carries
        # the typed reason strings, not error_type strings).
        payload = exc.to_dict()
        state.error_info = ErrorInfo(
            error="agent_error",
            message=payload["message"],
            details=payload["details"],
            recoverable=True,
        )
        state.answer_text = ""
        state.tools_called = []
    except Exception as exc:  # noqa: BLE001
        # The envelope is short; the log keeps the whole failure, stack included.
        logger.exception("turn forward failed session=%s", state.sid)
        state.error_info = forward_error_info(state, exc, state.transcript.raw_streamed_text())

    # #756 / #1339 / L1: everything finalize does (answer grounding, part assembly,
    # diff indexing, nanoagent spawn, publishes, persistence) reads prologue-derived
    # state -- ``turn_prologue_guard`` gates it on ``state.prologue_phase`` (a turn
    # whose prologue never ran or was cancelled mid-flight settles typed instead of
    # crashing on an unset field) and keeps the #756 envelope for an ordinary
    # finalize-region crash.
    try:
        await run_finalize_or_settle_prologue_gap(
            state,
            drain_observed_tool_calls=_drain_observed_tool_calls,
            update_retry_attempt=_update_retry_attempt,
        )
    finally:
        turn_lm_ledger.close_ledger(lm_ledger_token)
    from clio_agent.gact.conversations import ConversationOutcome
    from clio_agent.gact.turn_finalize import turn_outputs

    return ConversationOutcome(
        outputs=turn_outputs(state),
        disposition=getattr(state.pred, "assignment_disposition", None),
        context=getattr(state.pred, "clio_context_boundary", None),
        error=state.error_info.model_dump(mode="json") if state.error_info is not None else None,
    )


def _start_background_user_turn(
    app: "FastAPI",
    sid: str,
    sess: Session,
    user_text: str,
    *,
    request_parts: Optional[list[Part]] = None,
    metadata: Optional[dict[str, Any]] = None,
    prev_status: str = "idle",
    turn_agent_id: str = "",
    user_msg_id: str = "",
    user_created_at: str = "",
    message_role: Literal["user", "system"] = "user",
    retained_model: Optional[dict[str, Any]] = None,
    mailbox_item: Any = None,
    model_text: str | None = None,
    supersedes_input: str = "",
) -> Message:
    """Confirm typed user/system input; TurnRunner alone admits execution.

    Promotion preserves the accepted message identity. Retry supersession uses
    a server-supplied input identity, never client-overridable metadata.
    """
    owner = getattr(app.state, "conversations", None)
    if owner is None:
        raise RuntimeError("Core conversation admission is not ready")
    from clio_agent.arc.task_types import MailItem

    # This finite request boundary persists input only. TurnRunner owns the
    # decision to inject it into a busy segment or admit an idle session.
    with owner.history.serialized():
        from clio_agent.gact.session_input import capture_retry_input

        retry_binding = capture_retry_input(
            owner.history,
            sid,
            supersedes_input,
            stopping=app.state.turn_runner.stop_requested(sid),
        )
        now = time.time()
        user_metadata = dict(metadata or {})
        from clio_agent.gact.automatic_turn_injection import automatic_turn_injection

        automatic_injection = automatic_turn_injection(user_metadata, user_text)
        user_parts = (
            _user_message_parts(
                request_parts=list(request_parts or []),
                user_text=user_text,
            )
            if automatic_injection is None
            else [automatic_injection]
        )
        if automatic_injection is not None:
            message_role = "system"
        if message_role == "system":
            from clio_agent.gact.providers.config import _model_ref_dict

            model_metadata = (
                retained_model
                if retained_model is not None
                else {"model": _model_ref_dict(sess.model)}
            )
            user_metadata = {**model_metadata, **user_metadata}
        record_context_reference_deliveries(user_metadata, user_parts)
        image_count = sum(1 for part in user_parts if part.type == "image")
        if image_count:
            native_dispatch = _agent_accepts_images(app.state.agent)
            user_metadata["multimodal"] = {
                "image_part_count": image_count,
                "transcript_preserved": True,
                "native_model_dispatch": native_dispatch,
            }
            user_metadata["image_parts"] = _image_part_summaries(user_parts)
        if turn_agent_id:
            user_metadata["agent_override"] = {
                "requested_agent_id": turn_agent_id,
                "session_agent_id": _session_agent_id(sess),
                "scope": "turn",
            }
        user_msg_id = user_msg_id or _new_message_id("user")
        user_msg = Message(
            id=user_msg_id,
            # The turn id IS the user message id (#711); a user message correlates to
            # its own turn.
            turn_id=user_msg_id,
            session_id=sid,
            role=message_role,
            created_at=user_created_at or _iso_from_epoch(now),
            updated_at=_iso_from_epoch(now),
            parts=user_parts,
            metadata=user_metadata,
        )

        accepted_assignment = owner.history.latest_assignment(sid)
        content = owner.history.write_content(
            sid,
            {
                "message_id": user_msg.id,
                "assignment_id": (
                    accepted_assignment.handle
                    if accepted_assignment is not None and accepted_assignment.terminal_at is None
                    else None
                ),
                "text": user_text,
                "agent_id": turn_agent_id,
                "metadata": user_metadata,
                "message": user_msg.model_dump(mode="json"),
                **({"supersedes_input": retry_binding} if retry_binding is not None else {}),
                **({"model_text": model_text} if model_text is not None else {}),
                **(
                    {"queue_context": mailbox_item.content.model_dump(mode="json")}
                    if mailbox_item is not None
                    else {}
                ),
            },
        )
        if mailbox_item is None:
            accepted_input = owner.history.record_mail(
                MailItem(
                    id=user_msg.id,
                    recipient_session=sid,
                    sender_human=owner.caller(sid).human_id if message_role == "user" else None,
                    kind="human" if message_role == "user" else "hook",
                    content=content,
                    created_at=user_msg.created_at,
                )
            )
        else:
            accepted_input = owner.history.update_mail(
                mailbox_item.model_copy(
                    update={
                        "content": content,
                        "delivery": "eligible",
                        "revision": mailbox_item.revision + 1,
                    }
                ),
                expected_revision=mailbox_item.revision,
            )
        from clio_agent.gact.human_controls import relay_confirmed_human_input

        relay_confirmed_human_input(owner.history, accepted_input)
        # Core mail is authoritative. A failure before its commit must not leave
        # a conflicting transcript row; a later projection failure is recoverable
        # from the confirmed message body through startup/segment preparation.
        _append_session_message(app, sid, user_msg)
        app.state.bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=user_msg.to_wire(),
            )
        )

        return user_msg
