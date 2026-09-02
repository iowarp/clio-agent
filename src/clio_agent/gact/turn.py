"""Turn-orchestration engine for the GACT server (#714).

This module owns the *turn loop* — the off-request-thread machinery that drives
one agent turn end to end:

* :func:`_start_background_user_turn` stages a user message + parts, flips the
  session to ``running``, publishes the ``session.status_changed`` /
  ``message.created`` events, and schedules the turn as a tracked ``asyncio``
  task (so cancellation can reach it via ``app.state.in_flight_turns``).
* :func:`_run_turn_in_background` is the body of that task: it invokes
  ``agent.forward`` in an executor, streams/slices the result into Parts
  (a react main routes to its declared children by CALLING the spawn-runtime
  tools, so its ``answer`` is already the deliverable — no post-forward settle
  pass), publishes every SSE event the TUI consumes, persists the assistant
  message, records the context frame + token/cost usage, and returns the session
  to ``idle`` (or ``error``).

It was carved verbatim out of ``clio_agent.gact.app.build_app`` so the route
factories (post-message, question-answer, retry-attempt, schedules) and the
scheduler tick can share the entrypoint via ``GactDeps`` without importing back
into the 24k-line app module. To keep the import graph acyclic (#714), the turn
helpers this engine calls are imported at module top from their true *leaf*
owners (``delegation``/``streaming``/``enrichment``/``evidence``/``usage``/
``messaging``/``session_store``/``agents.resolution``/``runtime.globals`` …) —
so turn.py has ZERO top-level ``clio_agent.gact.app`` imports. Only the
agent-builder + blueprint-runner seams and ``_enrich_cancellation_error_info``
(the "danger set" retargeted by ~83 ``app._X`` test monkeypatches) are still
resolved through ``app`` via a *function-local* import at their single call site,
the same cycle-break pattern ``clio_agent.gact.agents.builders`` uses.

Behavior is byte-for-byte identical to the in-``build_app`` original: the
threading/executor handoff, cooperative + hard cancellation, turn timeout, the
``_ctx`` contextvar set/copy_context semantics, and the trajectory/SSE emission
are all preserved unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.delegation import (
    _coerce_expert_handoff_rows,
    _prediction_workflow_state,
)
from clio_agent.gact.enrichment import (
    _context_file_turn_provenance,
    _record_context_frame,
    consume_pending_agent_task_notifications,
    enrich_turn_context,
    inject_pending_agent_task_notifications,
)
from clio_agent.gact.events import Event, EventBus, _publish_transcript_event
from clio_agent.gact.evidence import _propose_edit_diffs_from_pred
from clio_agent.gact.message_intents import stage_intent_user_message
from clio_agent.gact.messaging import (
    _agent_accepts_images,
    _image_part_summaries,
    _user_message_parts,
)
from clio_agent.gact.permission_delivery import publish_permission_event
from clio_agent.gact.plan_mode import inject_plan_mode_reminder
from clio_agent.gact.replanning import inject_replan_suggestion
from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.runtime.globals import (
    _BlueprintRootDisabled,
    _cancelled_error_info,
    _coerce_error_info,
    _ContextFileAccessError,
    _emit_semantic_event,
    _iso_from_epoch,
    _new_message_id,
    _NoResolvableAgent,
    _session_agent_id,
    _TurnCancelled,
    _TurnTimedOut,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.session_store import (
    _compile_session_conversation_history,
)
from clio_agent.gact.skills import SkillNotDelegatableError
from clio_agent.gact.streaming import (
    _extract_tools_called,
    _format_react_trajectory,
    _pop_stream_fallback,
    _StreamingOutputError,
)
from clio_agent.gact.todos import inject_todo_recitation
from clio_agent.gact.tool_observer import (
    _merge_tool_call_rows,
    _tool_calls_from_handoff_rows,
)
from clio_agent.gact.turn_cancellation import settle_asyncio_cancellation
from clio_agent.gact.turn_finalize import (
    finalize_turn,
    maybe_pause_for_user,
    settle_failed_finalize,
)
from clio_agent.gact.turn_forward import forward_turn
from clio_agent.gact.turn_state import new_turn_state
from clio_agent.gact.turn_stream import (
    bind_live_emitter,
)
from clio_agent.gact.turn_usage import roll_up_usage
from clio_agent.gact.turn_watchdog import make_turn_cancel_event
from clio_agent.gact.types import (
    ErrorInfo,
    Message,
    Part,
    Session,
)
from clio_agent.gact.usage import _snapshot_lm_history_index

# NOTE (#714): every turn helper above is imported from its true *leaf* owner,
# not from ``clio_agent.gact.app``. The turn loop originally lived in ``app.py``
# and resolved these through the ``app`` re-export shims; only the agent-builder /
# blueprint-runner seams + ``_enrich_cancellation_error_info`` (the "danger set"
# that tests monkeypatch as e.g. ``clio_agent.gact.app._build_tool_user_agent_module``)
# are still read back through ``app`` at call time — via a function-local import at
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


async def _run_turn_in_background(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
    turn_agent_id: str = "",
) -> None:
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
    # #714 DANGER SET: the assistant-persist + cancellation-enricher seams are
    # resolved through ``app`` via a *function-local* import so the ~83 ``app._X``
    # test monkeypatches (which retarget these at call time) keep working with
    # zero test edits. ``_EXECUTABLE_SESSION_AGENT_IDS`` is an ``app``-owned module
    # constant kept here too (read by the except-chain). The agent-builder /
    # blueprint-runner danger-set seams moved with the forward orchestration to
    # ``turn_forward.py`` (#767 Phase B Slice 5), which keeps its own function-local
    # ``app`` import. Every other former app helper is imported at module top from
    # its true leaf owner (#714), so turn.py has ZERO top-level ``app`` imports.
    from clio_agent.gact.app import (  # noqa: PLC0415
        _EXECUTABLE_SESSION_AGENT_IDS,
    )

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
    _emit_semantic_event(
        state.app,
        state.sid,
        "turn.started",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="running",
        summary="User turn accepted and CLIO runtime started.",
        actor={"role": "user"},
        subject={"message_id": state.user_msg.id},
        payload={"text": state.user_text, "retry_attempt_id": state.retry_attempt_id},
    )
    _publish_transcript_event(
        state.bus,
        state.sid,
        "turn.started",
        {
            "turn_id": state.turn_id,
            "agent_id": state.turn_agent_id or _session_agent_id(state.sess) or "main",
        },
    )
    bringup_timing.timer_for_session(state.app, state.sid).end_phase("turn.accept_gap")

    # iowarp/clio-agent#5: prepend attached context files to the user's text so the
    # agent's forward() sees them as primed input (plain concat, expert-agnostic).
    context_file_error: ErrorInfo | None = None
    state.context_file_provenance = _context_file_turn_provenance(
        state.app, state.sid, status="prepared"
    )
    state.memory_search_metadata = {}
    try:
        # #1215 S5: enrich_turn_context times BOTH mechanisms below as ONE
        # "enrichment" bring-up phase (owner module gact/enrichment.py).
        state.enriched_text, state.memory_search_metadata = enrich_turn_context(
            state.app, state.sid, state.user_text, state.user_msg
        )
        # #948 S6 [1]/[4]: surface prior-turn background task results (observe-later).
        # STAGE the ids only; consumption + terminal emission defer to the commit-to-
        # run seam below, so a turn aborted after enrichment leaves them pending.
        state.enriched_text, state.pending_notification_task_ids = (
            inject_pending_agent_task_notifications(state.app, state.sid, state.enriched_text)
        )
        # P1.2 #1064: surface plan mode to the model each turn (survives compaction; no-op otherwise).
        state.enriched_text = inject_plan_mode_reminder(
            state.app, state.sid, state.sess, state.enriched_text
        )
        state.enriched_text = inject_todo_recitation(
            state.app, state.sid, state.sess, state.enriched_text
        )
        # P1.6d #1068: surface a pending stall-triggered replanning suggestion once (no-op otherwise).
        state.enriched_text = inject_replan_suggestion(
            state.app, state.sid, state.sess, state.enriched_text
        )
        # Carry prior turns so a follow-up ("now plot it") reuses resolved state (no-op turn 1).
        state.enriched_text = _compile_session_conversation_history(
            state.app, state.sid, state.enriched_text
        )
    except _ContextFileAccessError as exc:
        state.enriched_text = state.user_text
        context_file_error = exc.error_info
        state.context_file_provenance = _context_file_turn_provenance(
            state.app, state.sid, status="error"
        )
    state.context_frame = _record_context_frame(
        state.app,
        state.sid,
        state.sess,
        state.user_msg,
        user_text=state.user_text,
        enriched_text=state.enriched_text,
        context_error=context_file_error,
    )
    if state.memory_search_metadata:
        _emit_semantic_event(
            state.app,
            state.sid,
            "memory.search.completed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            summary="Requested memory search was injected into turn context.",
            actor={"role": "runtime", "component": "memory"},
            subject={"message_id": state.user_msg.id},
            payload=state.memory_search_metadata,
        )
    # P2.2 #1070 / P2.6 #1074: UserPromptSubmit hooks (the ported ``pre_message``
    # consumer). A deny VETOES the turn (session → error); a ``defer`` SUSPENDS it for
    # out-of-band approval (waiting_user, resume as a new turn). The whole finalize-
    # boundary protocol lives in the hooks owner module (no-accretion) — this is only
    # the call site: any non-"proceed" outcome ends the turn here.
    if context_file_error is None:
        from clio_agent.gact.hooks.user_prompt import run_user_prompt_submit  # noqa: PLC0415

        if run_user_prompt_submit(state, update_retry_attempt=_update_retry_attempt) != "proceed":
            return

    # iowarp/clio-agent#6: try real per-token streaming via dspy.streamify when the LM supports it;
    # fall back to the synchronous executor path otherwise. Streaming produces message.part.delta
    # events as chunks arrive — without it the text part lands as one big delta after forward.
    #
    # #767 PR2: the TurnTranscript ledger owns the streamed-part state machine (lazy message mint,
    # per-(agent, field) part open/close, per-part buffers, whole-buffer clean at close, the runtime
    # boundary). The turn loop owns the ledger's LIFECYCLE: opened here, settled on every exit path
    # (success, the #756 finalize error envelope, the ask_user early return).
    # :func:`~clio_agent.gact.turn_stream.emit_chunk` is a thin adapter: semantic lm.token.delta +
    # the parent-resume suppression gate + stream_audit, then one transcript call.
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _active_workflow_state_schema,
    )
    from clio_agent.gact.tool_observer import _open_turn_transcript  # noqa: PLC0415

    # #767 Phase C: resolve the turn's pack workflow_state schema at the one turn
    # seam and carry it on ``state`` for every delegation/grounding/scrub site.
    state.workflow_schema = _active_workflow_state_schema(state.app, state.sid)
    state.transcript = _open_turn_transcript(state.app, state.sid, state.turn_id)
    # TRICKY #1 (Phase B spec): bind the emitter over ``state`` so its LATE reads
    # of state.active_agent_id / state.invocation_agent_id see the forward seam's
    # IN-PLACE mutations. ``forward_turn`` reconstructs the same
    # ``partial(emit_chunk, state)`` for its streamed-forward sites, so both the
    # executor rail and the streamed forward resolve the generating agent
    # identically.

    # Unified LM token highway (#693): bind this turn's loop + chat publisher so a
    # blueprint/expert LM call streamed in an executor thread feeds the SAME
    # emitter — one streaming path for chat AND blueprint turns, instead of the
    # old executor drain-and-discard. The executor inherits this binding via the
    # contextvars.copy_context() at the forward sites below.
    bind_live_emitter(state, asyncio.get_running_loop())

    # iowarp/clio-agent#8: snapshot LM history before the turn so we
    # can sum every call this turn made. ContextVars don't propagate
    # to asyncio executor threads (so dspy.settings.usage_tracker is
    # unreliable from worker threads), but ``lm.history`` IS shared
    # across threads — list.append under the GIL gives us a clean,
    # thread-safe ledger. We diff history[start:end] post-turn.
    state.history_start = _snapshot_lm_history_index(state.app)
    _pop_stream_fallback(state.app, state.sid)
    # #767 Phase B Slice 5: mint + register the cancel event and derive the
    # no-progress watchdog cadence onto ``state`` (formerly the inline setup
    # block); the watchdog itself is now the free functions
    # ``cancel_requested`` / ``await_turn_work`` in ``turn_watchdog.py`` that the
    # forward + delegation seams drive off ``state``.
    make_turn_cancel_event(state)

    try:
        if context_file_error is not None:
            raise _ContextFileAccessError(context_file_error)

        if state.sid in state.app.state.cancel_flags:
            state.app.state.cancel_flags.discard(state.sid)
            raise _TurnCancelled(
                _cancelled_error_info(
                    state.sid,
                    execution_cancellation="turn_boundary",
                    executor_work_may_continue=False,
                )
            )

        # #948 S6 [1]/[4]: commit-to-run seam — past the last abort/veto seam, the
        # turn will forward with the enriched input. Consume the staged observe-later
        # notifications once AND emit each delegation terminal (shared once-gate with
        # wait/check) into this turn's already-open transcript.
        consume_pending_agent_task_notifications(
            state.app, state.sid, state.pending_notification_task_ids
        )

        # #767 Phase B Slice 5: agent resolve -> module build -> streamed/sync
        # forward -> expert-pack delegation settle lives in ``turn_forward.py``.
        # It sets state.active_agent_id / invocation_agent_id (IN PLACE, TRICKY
        # #1) / agent_runtime / prompt_resolution / dynamic_agent_used /
        # expert_handoffs and returns the prediction (TRICKY #2: pred is a seam
        # return value, ``state.pred = forward_turn(state)``).
        state.pred = await forward_turn(state)
        # #1215 S5: bring-up as far as instrumented (fleet.mount not wired yet,
        # #1215 stays open) ends here on the success path; a failed first turn's
        # still-open timer settles later via the registry's LRU eviction.
        bringup_timing.finish_bringup(state.app, state.sid)

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
        if maybe_pause_for_user(state, state.pred, update_retry_attempt=_update_retry_attempt):
            # #767 Phase B: the ask_user pause exits the turn before the
            # finalize region — the seam mints the question, flips the
            # session to waiting_user, and settles the ledger (see
            # turn_finalize.py).
            return
        # P1.4 #1066: plan_exit is the SAME turn-ending yield — surface the pending
        # plan-exit as an N-way approval question and exit before finalize (owner
        # module gact/plan_mode.py; only the call site lands here).
        from clio_agent.gact.plan_mode import maybe_pause_for_plan_exit  # noqa: PLC0415

        if maybe_pause_for_plan_exit(state):
            return
        # iowarp/clio-agent#25: data branch reports which execution
        # path it took ("fast" or "expert_loop"). Empty when not
        # populated by ClioAgent.forward (older code paths, non-data
        # branches not yet migrated).
        state.execution_path = getattr(state.pred, "execution_path", "") or ""
        state.tools_called = _extract_tools_called(state.pred)
        top_level_workflow_state = _prediction_workflow_state(
            state.pred, schema=state.workflow_schema
        )
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
        state.tools_called = _drain_observed_tool_calls(state.tools_called)
        # iowarp/clio-agent#17 — surface DSPy reasoning as a
        # `thinking` Part. ChainOfThought predictions expose
        # ``.reasoning`` (single string); ReAct exposes
        # ``.trajectory`` (step-by-step trace). Fall back to the
        # generic `_trace` Prediction wraps either of them in.
        state.thinking_text = (
            getattr(state.pred, "reasoning", "")
            or _format_react_trajectory(getattr(state.pred, "trajectory", None))
            or ""
        )
        # cost + token rollup — mutate
        # state.turn_tokens / state.turn_cost from the prediction
        # or the per-turn LM history slice (see turn_usage.py).
        roll_up_usage(state, state.pred)
        state.proposed_diffs = list(getattr(state.pred, "file_diffs", None) or [])
        if not state.proposed_diffs:
            # Dynamic tool agents call fs_propose_edit as a TOOL and never set
            # pred.file_diffs; promote those results so they materialize as
            # file_diff parts + pending /diffs rows (iowarp/clio-agent#674).
            state.proposed_diffs = _propose_edit_diffs_from_pred(state.pred)
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
            _emit_semantic_event(
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
            state.error_info = _cancelled_error_info(
                state.sid,
                execution_cancellation="turn_boundary",
                executor_work_may_continue=False,
            )
            state.answer_text = ""
            state.tools_called = []
    except _TurnCancelled as exc:
        state.error_info = exc.error_info
        state.answer_text = ""
        state.tools_called = []
    except asyncio.CancelledError:
        settle_asyncio_cancellation(state)
    except _StreamingOutputError as exc:
        original = exc.__cause__ or exc
        partial_answer = state.transcript.raw_streamed_text()
        state.error_info = ErrorInfo(
            error="provider_error",
            message=str(exc),
            details={
                "original_error": type(original).__name__,
                "partial_output": bool(partial_answer),
                "stream_source": ("live" if partial_answer else "batch"),
            },
            recoverable=True,
        )
        state.answer_text = partial_answer
        state.tools_called = []
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
                "executor_work_may_continue": True,
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
    except _BlueprintRootDisabled as exc:
        # #948 S4: the active blueprint's declared root is disabled by validation.
        # Typed failure carrying the exact errors — never a substitute root,
        # never the legacy planner.
        state.selected_agent = exc.root_id
        state.rationale = "The active Agent Blueprint's root expert is disabled by validation."
        state.error_info = ErrorInfo(
            error="blueprint_root_disabled",
            message=(
                f"Active Agent Blueprint root expert {exc.root_id!r} is disabled "
                "by validation; the turn cannot run."
            ),
            details={
                "root_id": exc.root_id,
                "agent_blueprint_id": exc.blueprint_id,
                "validation_errors": exc.validation_errors,
                "recovery_actions": [
                    "update_agent_blueprint_install",
                    "fix_blueprint_declaration",
                    "activate_another_blueprint",
                ],
            },
            recoverable=True,
        )
        state.answer_text = ""
        state.tools_called = []
    except _NoResolvableAgent as exc:
        # #948 S4b: a default/main session resolved NO executable Agent Blueprint,
        # and the legacy Tier-1 planner that used to run here is deleted. Fail
        # TYPED — never fall through to a legacy pathway.
        state.selected_agent = exc.agent_id
        state.rationale = (
            "No Agent Blueprint resolved for this session, and the legacy planner "
            "is removed, so the turn has nothing to execute."
        )
        state.error_info = ErrorInfo(
            error="no_resolvable_agent",
            message=(
                "No resolvable Agent Blueprint for this session; install the "
                "default registry or activate an Agent Blueprint to run turns."
            ),
            details={
                "agent_id": exc.agent_id,
                "recovery_actions": [
                    "install_default_registry",
                    "activate_agent_blueprint",
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
                "supported_agent_ids": sorted(
                    agent_id for agent_id in _EXECUTABLE_SESSION_AGENT_IDS if agent_id
                ),
                "unsupported_tools": exc.tools,
                "mount_failures": mount_failures,
                "recovery_actions": [
                    "choose_builtin_agent",
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
    except SkillNotDelegatableError as exc:
        # #918: a session/turn bound to a skill id fails TYPED (skills are not
        # agents since the skill-semantics change), never as a generic
        # agent_error that hides the fix.
        state.error_info = ErrorInfo(
            error="skill_not_delegatable",
            message=str(exc),
            details={
                "skill_id": exc.skill_id,
                "skill_path": exc.path,
                "recovery_actions": ["choose_builtin_agent", "retry", "exit"],
            },
            recoverable=True,
        )
        state.answer_text = ""
        state.tools_called = []
    except Exception as exc:  # noqa: BLE001
        state.error_info = ErrorInfo(
            error="agent_error",
            message=f"agent.forward raised: {exc}",
            details={"original_error": type(exc).__name__},
            recoverable=True,
        )

    # #756: everything below (answer grounding, part assembly, diff indexing,
    # nanoagent spawn, publishes, persistence) is the ``finalize_turn`` seam
    # (#767 Phase B Slice 6). It runs inside a fire-and-forget task; an
    # exception escaping it used to vanish (the done-callback only pops
    # in_flight_turns) and wedge the session in 'running' with no completion
    # event. The ``try/except finalize_exc`` #756 envelope stays HERE in the
    # orchestrator so a finalize crash is settled by ``settle_failed_finalize``
    # (a visible error turn + terminal session status), never re-raised.
    try:
        finalize_turn(
            state,
            state.pred,
            drain_observed_tool_calls=_drain_observed_tool_calls,
            update_retry_attempt=_update_retry_attempt,
        )
    except Exception as finalize_exc:  # noqa: BLE001 - detached task: settle, no re-raise
        settle_failed_finalize(
            state.app,
            state.sid,
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            turn_tokens=state.turn_tokens,
            turn_cost=state.turn_cost,
            turn_cancel_event=state.turn_cancel_event,
            update_retry_attempt=_update_retry_attempt,
            exc=finalize_exc,
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
    replace_existing_user_message: bool = False,
) -> Message:
    """Stage a user turn and drive it off-thread.

    Persists the user message + parts, flips the session to ``running``, publishes
    ``session.status_changed`` + ``message.created``, then schedules
    :func:`_run_turn_in_background` as a tracked ``asyncio`` task (in
    ``app.state.in_flight_turns`` so cancellation reaches it). Returns the staged
    user :class:`Message`. Hoisted out of ``build_app`` (#714) so callers share it
    via ``GactDeps`` with ``app`` an explicit arg. ``user_msg_id`` overrides the
    minted message/turn id (empty ⇒ mint) so an idle steer re-drive reuses the id
    its ``202`` already returned. The two ``…_existing``/``…_created_at`` args
    promote an ALREADY persisted pending steer: acceptance wrote that identity
    durably, so staging replaces that row in place, its accepted stamp kept.
    """
    now = time.time()
    user_metadata = dict(metadata or {})
    user_parts = _user_message_parts(
        request_parts=list(request_parts or []),
        user_text=user_text,
    )
    from clio_agent.gact.context_references import (  # noqa: PLC0415
        context_reference_deliveries,
    )

    reference_deliveries = context_reference_deliveries(user_parts)
    if reference_deliveries:
        user_metadata["context_reference_deliveries"] = reference_deliveries
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
        role="user",
        created_at=user_created_at or _iso_from_epoch(now),
        updated_at=_iso_from_epoch(now),
        parts=user_parts,
        metadata=user_metadata,
    )

    stage_intent_user_message(app, sid, user_msg, replace_existing=replace_existing_user_message)
    app.state.sessions.update(sid, status="running")
    app.state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": "running",
                "prev_status": prev_status,
            },
        )
    )
    app.state.bus.publish(
        Event(
            type="message.created",
            session_id=sid,
            payload=user_msg.to_wire(),
        )
    )

    # #948 S1 (#662): route through the TurnRunner, the single owner of turn-task
    # lifetime. It holds a master strong ref (no GC-cancellation), anchors the
    # task to the app loop, records the busy-gate handle, and drops the
    # per-session slot on completion — replacing the raw create_task + manual
    # in_flight_turns bookkeeping that lived here.
    app.state.turn_runner.spawn(
        _run_turn_in_background(app, sid, user_text, user_msg, turn_agent_id),
        sid=sid,
        turn_id=user_msg_id,
    )
    return user_msg
