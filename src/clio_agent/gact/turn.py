"""Turn-orchestration engine for the GACT server (#714).

This module owns the *turn loop* — the off-request-thread machinery that drives
one agent turn end to end:

* :func:`_start_background_user_turn` stages a user message + parts, flips the
  session to ``running``, publishes the ``session.status_changed`` /
  ``message.created`` events, and schedules the turn as a tracked ``asyncio``
  task (so cancellation can reach it via ``app.state.in_flight_turns``).
* :func:`_run_turn_in_background` is the body of that task: it invokes
  ``agent.forward`` in an executor, streams/slices the result into Parts,
  settles dynamic-agent delegations (via the
  :mod:`clio_agent.gact.turn_delegation` settle engine —
  ``settle_dynamic_agent_delegations`` / ``execute_delegated_experts`` /
  ``run_dynamic_agent_sync``), publishes every SSE event the TUI consumes,
  persists the assistant message, records the context frame + token/cost usage,
  and returns the session to ``idle`` (or ``error``).

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
import contextvars
import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact._params import (
    _gact_turn_timeout_s,
)
from clio_agent.gact.agents.resolution import (
    _agent_definition_uses_blueprint_runtime,
    _resolve_runtime_dynamic_agent,
    _runtime_active_agent_blueprint_agent_ids,
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_root_id,
)
from clio_agent.gact.delegation import (
    _coerce_expert_handoff_rows,
    _fallback_answer_from_delegation,
    _looks_like_structured_answer,
    _prediction_workflow_state,
    _workflow_state_from_handoff_rows,
)
from clio_agent.gact.enrichment import (
    _context_file_turn_provenance,
    _enrich_with_context_files,
    _enrich_with_requested_memory_search,
    _finalize_context_frame,
    _record_context_frame,
)
from clio_agent.gact.events import Event, EventBus, _publish_transcript_event
from clio_agent.gact.evidence import (
    _dynamic_agent_runtime_provenance,
    _ground_fabricated_local_artifact_paths,
    _propose_edit_diffs_from_pred,
    _tool_result_preview,
)
from clio_agent.gact.messaging import (
    _agent_accepts_images,
    _ask_user_options_from_action,
    _coerce_ask_user_action,
    _image_part_summaries,
    _prediction_summary,
    _user_message_parts,
)
from clio_agent.gact.providers.auth import _refresh_argonne_lm_token
from clio_agent.gact.runtime.globals import (
    _cancelled_error_info,
    _coerce_error_info,
    _ContextFileAccessError,
    _emit_semantic_event,
    _iso_from_epoch,
    _llm_provider_payload,
    _new_message_id,
    _new_part_id,
    _new_question_id,
    _session_agent_id,
    _tool_session_context,
    _TurnCancelled,
    _TurnTimedOut,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.runtime.retention import enforce_dict_bound, enforce_list_bound
from clio_agent.gact.runtime.type_parsing import _blueprint_module_kind
from clio_agent.gact.session_store import (
    _compile_session_conversation_history,
)
from clio_agent.gact.streaming import (
    _agent_forward_compat,
    _clear_live_streamed_field_text,
    _extract_tools_called,
    _format_react_trajectory,
    _pop_stream_fallback,
    _run_dynamic_agent_compat,
    _stream_fallback_payload,
    _StreamingOutputError,
    _try_streamed_forward_compat,
)
from clio_agent.gact.tool_observer import (
    _merge_tool_call_rows,
    _sanitize_handoff_tool_metadata,
    _sanitize_tools_called_metadata,
    _tool_calls_from_handoff_rows,
)
from clio_agent.gact.turn_delegation import settle_dynamic_agent_delegations
from clio_agent.gact.turn_nanoagents import spawn_nanoagents
from clio_agent.gact.turn_state import new_turn_state
from clio_agent.gact.turn_stream import (
    bind_live_emitter,
    emit_chunk,
    settle_turn_transcript,
)
from clio_agent.gact.turn_usage import roll_up_usage
from clio_agent.gact.types import (
    ErrorInfo,
    Message,
    Part,
    Session,
    Tokens,
    UserQuestion,
)
from clio_agent.gact.usage import (
    _reasoning_records_from_history_slice,
    _snapshot_lm_history_index,
)
from clio_agent.runtime import trace
from clio_agent.runtime.lm_activity import lm_call_in_flight as _lm_call_in_flight

# NOTE (#714): every turn helper above is imported from its true *leaf* owner,
# not from ``clio_agent.gact.app``. The turn loop originally lived in ``app.py``
# and resolved these through the ``app`` re-export shims; only the agent-builder /
# blueprint-runner seams + ``_enrich_cancellation_error_info`` (the "danger set"
# that tests monkeypatch as e.g. ``clio_agent.gact.app._build_tool_user_agent_module``)
# are still read back through ``app`` at call time — via a function-local import at
# their single call site — to preserve that monkeypatch contract with zero test
# edits. That is the ONLY ``app`` import in this module, and it is function-local,
# so the import graph stays acyclic.

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef  # noqa: F401

logger = logging.getLogger(__name__)


def _settle_failed_finalize(
    app: "FastAPI",
    sid: str,
    *,
    turn_id: str,
    trace_id: str,
    turn_tokens: Mapping[str, int],
    turn_cost: float,
    turn_cancel_event: threading.Event,
    update_retry_attempt: "Callable[..., None]",
    exc: BaseException,
) -> None:
    """#756: the turn's error envelope for a finalize-region crash.

    Everything after :func:`_run_turn_in_background`'s forward except-chain
    (answer grounding, part assembly, diff indexing, publishes, persistence)
    runs inside a fire-and-forget task. An exception escaping there used to
    die silently -- no ``message.completed``, no ``session.status_changed``,
    session wedged in ``running`` forever. This settles the turn instead:
    structured log, ``turn.failed`` semantic event, ``message.completed`` with
    ``stop_reason=error`` + ``error_info``, a persisted assistant error message
    (so the failure is visible in the reloaded transcript, not just live), and
    a terminal ``session.status_changed``. Nothing degrades silently: every
    best-effort step below logs its reason when it fails.
    """

    # #714 danger set: bind through app at call time so test monkeypatches of
    # clio_agent.gact.app._append_session_message (e.g. the live==reload
    # property fixture) keep intercepting assistant persistence.
    from clio_agent.gact.app import _append_session_message  # noqa: PLC0415

    logger.error(
        "turn finalize failed: reason=turn_finalize_error session=%s turn=%s error=%s",
        sid,
        turn_id,
        type(exc).__name__,
        exc_info=exc,
    )
    if trace.HF_ON:
        trace.hot("TURN-FINALIZE-FAIL", "%s %s: %s", sid, type(exc).__name__, exc)

    # #767 PR2: a failed finalize must still settle the ledger — freeze it
    # (late producer ops are rejected + audited) and retire it from the
    # registry so it can never poison the next turn. Runs unconditionally,
    # before the already-settled early return below.
    registry = getattr(app.state, "turn_transcripts", None)
    if registry is not None:
        transcript = registry.get(sid)
        if transcript is not None:
            transcript.abandon()
        registry.close(sid)

    sess = app.state.sessions.get(sid)
    if sess is not None and getattr(sess, "status", "") != "running":
        # Finalize already settled the turn (the exception escaped after the
        # terminal publishes); re-running the envelope would double-publish
        # completion. The failure stays visible via the log above.
        return

    error_info = ErrorInfo(
        error="finalize_error",
        message=f"turn finalize raised: {exc}",
        details={
            "reason": "turn_finalize_error",
            "session_id": sid,
            "turn_id": turn_id,
            "original_error": type(exc).__name__,
            "stage": "finalize",
        },
        recoverable=True,
    )
    now = time.time()
    assistant_msg = Message(
        id=_new_message_id("asst"),
        turn_id=turn_id,
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(now),
        updated_at=_iso_from_epoch(now),
        parts=[],
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
    bus: EventBus = app.state.bus
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
                "final_message": assistant_msg.model_dump(exclude_none=True),
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
    try:
        _append_session_message(app, sid, assistant_msg)
    except Exception:  # noqa: BLE001 - persistence degraded; the status flip must still happen
        logger.exception(
            "assistant error-message persistence failed during finalize settle: session=%s turn=%s",
            sid,
            turn_id,
        )
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
    # #757: the streamed-field buffer is per-turn; a failed finalize must
    # drop it exactly like the happy-path cleanup or later turns'
    # suppression matchers eat legitimate thinking parts.
    _clear_live_streamed_field_text(app, sid)
    if sess is not None:
        app.state.sessions.update(
            sid,
            status="error",
            message_count=sess.message_count + 2,
        )
    bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": "error",
                "prev_status": "running",
            },
        )
    )
    if app.state.cancel_events.get(sid) is turn_cancel_event:
        app.state.cancel_events.pop(sid, None)


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
    # #714 DANGER SET: the agent-builder + blueprint-runner seams and the
    # cancellation-enricher are resolved through ``app`` via a *function-local*
    # import so the ~83 ``app._X`` test monkeypatches (which retarget these at
    # call time) keep working with zero test edits. ``_EXECUTABLE_SESSION_AGENT_IDS``
    # is an ``app``-owned module constant kept here too (not relocated). Every
    # other former app helper is now imported at module top from its true leaf
    # owner (#714), so turn.py has ZERO top-level ``app`` imports.
    from clio_agent.gact.app import (  # noqa: PLC0415
        _EXECUTABLE_SESSION_AGENT_IDS,
        _append_session_message,
        _blueprint_runner_for_agent,
        _build_blueprint_dspy_module,
        _build_prompt_user_agent_module,
        _build_tool_user_agent_module,
        _enrich_cancellation_error_info,
    )

    bus: EventBus = app.state.bus
    sess = app.state.sessions.get(sid)
    if sess is None:
        # Session evaporated between POST + background start; can't
        # do anything useful. Don't raise — the publishing path
        # would crash and pollute logs with no client to notify.
        return

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
        enforce_dict_bound(state.app, state.app.state.turn_attempts, "turn_attempts", session_id=state.sid)
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

    # iowarp/clio-agent#5: prepend any attached context files to the
    # user's text so the agent's forward() sees them as primed input.
    # Plain text concat — keeps the agent.py interface untouched and
    # works regardless of which expert handles the turn.
    context_file_error: ErrorInfo | None = None
    state.context_file_provenance = _context_file_turn_provenance(state.app, state.sid, status="prepared")
    state.memory_search_metadata = {}
    try:
        state.enriched_text = _enrich_with_context_files(state.app, state.sid, state.user_text)
        state.enriched_text, state.memory_search_metadata = _enrich_with_requested_memory_search(
            state.app,
            state.sid,
            state.enriched_text,
            state.user_msg,
        )
        # Carry prior turns of this session so a follow-up ("now plot it") can reuse
        # the region/stations/paths already resolved. No-op on the first turn.
        state.enriched_text = _compile_session_conversation_history(state.app, state.sid, state.enriched_text)
    except _ContextFileAccessError as exc:
        state.enriched_text = state.user_text
        context_file_error = exc.error_info
        state.context_file_provenance = _context_file_turn_provenance(state.app, state.sid, status="error")
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
    # iowarp/clio-agent#20: pre_message hook can transform the
    # input or veto the turn. PermissionError → cancelled-style
    # error_info; the caller sees the hook's reason.
    if context_file_error is None:
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _emit_semantic_event(
                state.app,
                state.sid,
                "hook.invocation.started",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="running",
                summary="pre_message hook dispatch started.",
                actor={"hook": "pre_message"},
                subject={"message_id": state.user_msg.id},
                payload={"input": state.enriched_text},
            )
            hook_scope = {
                "session_id": state.sid,
                "workspace_id": getattr(state.sess, "workspace_id", ""),
                "blueprint_id": _runtime_active_agent_blueprint_id(state.app, state.sid),
            }
            _fire_hook("pre_message", state.sid, state.enriched_text, hook_scope=hook_scope)
            _emit_semantic_event(
                state.app,
                state.sid,
                "hook.invocation.completed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary="pre_message hook dispatch completed.",
                actor={"hook": "pre_message"},
                subject={"message_id": state.user_msg.id},
                payload={},
            )
        except PermissionError as exc:
            _emit_semantic_event(
                state.app,
                state.sid,
                "hook.pre_message.blocked",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="blocked",
                summary="pre_message hook blocked the turn.",
                actor={"hook": "pre_message"},
                subject={"message_id": state.user_msg.id},
                payload={"error": str(exc)},
            )
            _emit_semantic_event(
                state.app,
                state.sid,
                "turn.failed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="blocked",
                summary="CLIO turn was blocked by pre_message hook.",
                actor={"hook": "pre_message"},
                subject={"message_id": state.user_msg.id},
                payload={"error": str(exc)},
            )
            state.bus.publish(
                Event(
                    type="message.completed",
                    session_id=state.sid,
                    payload={
                        "turn_id": state.turn_id,
                        "message_id": state.user_msg.id,
                        "stop_reason": "blocked",
                        "error_info": {
                            "error": "permission_error",
                            "message": str(exc),
                            "recoverable": True,
                        },
                    },
                )
            )
            state.app.state.sessions.update(state.sid, status="error")
            _update_retry_attempt(
                "failed",
                metadata_patch={
                    "execution_error": "permission_error",
                    "executed_user_message_id": state.user_msg.id,
                },
            )
            state.bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=state.sid,
                    payload={
                        "session_id": state.sid,
                        "status": "error",
                        "prev_status": "running",
                        "reason": "pre_message hook blocked turn",
                    },
                )
            )
            return

    # iowarp/clio-agent#6: try real per-token streaming via
    # dspy.streamify when the LM supports it; fall back to the
    # synchronous executor path otherwise. Streaming produces
    # message.part.delta events as chunks arrive — without it the
    # text part lands as one big delta after forward returns.
    #
    # #767 PR2: the TurnTranscript ledger owns the streamed-part state machine
    # (lazy message mint, per-(agent, field) part open/close, per-part buffers,
    # whole-buffer clean at close, the runtime boundary) that used to live here
    # as ~10 closure vars + _close_streamed_part + the cross-module boundary
    # hook. The turn loop owns the ledger's LIFECYCLE: opened here, settled on
    # every exit path (success, the #756 finalize error envelope, the ask_user
    # early return). :func:`~clio_agent.gact.turn_stream.emit_chunk` (extracted to
    # turn_stream.py, #767 Phase B Slice 3) is now a thin adapter: semantic
    # lm.token.delta + the parent-resume suppression gate (PR4 retires it) +
    # stream_audit, then one transcript call.
    from clio_agent.gact.tool_observer import _open_turn_transcript  # noqa: PLC0415

    state.transcript = _open_turn_transcript(state.app, state.sid, state.turn_id)
    state.suppressed_parent_resume_offsets = {}
    # TRICKY #1 (Phase B spec): bind the emitter over ``state`` so its LATE reads
    # of state.active_agent_id / state.invocation_agent_id see the forward seam's
    # IN-PLACE mutations. The same bound callable feeds the streamed-forward sites
    # below, so both paths resolve the generating agent identically.
    live_emit = partial(emit_chunk, state)

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
    state.turn_cancel_event = threading.Event()
    state.app.state.cancel_events[state.sid] = state.turn_cancel_event
    if state.sid in state.app.state.cancel_flags:
        state.turn_cancel_event.set()
    # No-progress watchdog, not a hard wall: CLIO_GACT_TURN_TIMEOUT_S bounds the
    # gap BETWEEN observable progress events, never the total turn duration. A
    # long-but-progressing turn (a multi-phase EarthScope pipeline: filter ->
    # stage -> profile -> plot, each emitting bus events) must run to completion;
    # only a turn that goes silent for the whole window is wedged and aborted.
    # See [[clio-no-session-timeout]].
    state.turn_progress_timeout_s = _gact_turn_timeout_s(state.app)
    # Poll the progress heartbeat on a short cadence so abort latency after the
    # turn truly wedges stays small without busy-waiting. Cap by the window so a
    # tiny configured timeout still polls at least as often.
    state._watchdog_poll_s = min(2.0, state.turn_progress_timeout_s) if state.turn_progress_timeout_s > 0 else 2.0

    def cancel_requested() -> bool:
        return state.turn_cancel_event.is_set()

    async def _await_turn_work(awaitable: Any) -> Any:
        if state.turn_progress_timeout_s <= 0:
            return await awaitable
        # Drive the work as a task and poll for completion. asyncio.wait (unlike
        # wait_for) does NOT cancel the task when the poll interval elapses, so a
        # still-running turn is never disturbed by the watchdog tick. We seed the
        # no-progress clock at "now" so a turn that publishes nothing at all is
        # still bounded by one window; every bus publish for THIS session
        # refreshes it via EventBus.last_publish_monotonic. Progress is
        # attributed per-session on purpose: folding other sessions' publishes
        # in (the old global "" stamp) kept a genuinely wedged session alive as
        # long as any other session was busy (iowarp/clio-agent#761).
        state.bus = state.app.state.bus
        task = asyncio.ensure_future(awaitable)
        last_progress = time.monotonic()
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=state._watchdog_poll_s)
                if done:
                    return task.result()
                heartbeat = state.bus.last_publish_monotonic(state.sid)
                if heartbeat > last_progress:
                    last_progress = heartbeat
                # An LM call that is actively generating IS progress, even when it
                # publishes no bus events for the watchdog to see -- a deep-
                # reasoning model streams its chain-of-thought on a separate
                # channel (invisible to DSPy's answer-content listeners) and an
                # expert child runs the call synchronously in an executor (no live
                # deltas at all). Treating an in-flight LM call as progress stops
                # the watchdog from killing a working model mid-think; a per-call
                # ceiling inside lm_call_in_flight() still lets it abort a truly
                # wedged provider. See clio_agent.runtime.lm_activity.
                #
                # Scoped to THIS session (like the bus-progress stamp above): only
                # an LM call owned by this turn's session counts as its progress,
                # so a busy neighbor session's in-flight call can no longer keep a
                # genuinely wedged session alive (iowarp/clio-agent#761 defect 2).
                if _lm_call_in_flight(state.sid):
                    last_progress = time.monotonic()
                if time.monotonic() - last_progress >= state.turn_progress_timeout_s:
                    state.turn_cancel_event.set()
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - swallow during abort
                        pass
                    raise _TurnTimedOut(state.turn_progress_timeout_s) from None
        except asyncio.CancelledError:
            # If the work already finished, the cancellation targeted *us* (the
            # watchdog wrapper) after the result was ready -- e.g. event-loop
            # teardown cancelling pending tasks. Surface the completed result
            # rather than masking a finished turn as a cancellation.
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is None:
                    return task.result()
            task.cancel()
            raise

    # #767 Phase B (Slice 4->5 bridge): the delegation settle engine lives in
    # ``turn_delegation.py`` but the no-progress watchdog is still owned by these
    # closures until Slice 5 extracts ``turn_watchdog.py``. Publish the two
    # closures onto ``state`` so the extracted free functions reach them off the
    # threaded state (they close over only ``state``, so this is aliasing-safe).
    state.cancel_requested = cancel_requested
    state.await_turn_work = _await_turn_work

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

        session_agent_id = _session_agent_id(state.sess)
        state.active_agent_id = state.turn_agent_id or session_agent_id
        active_blueprint_root_id = _runtime_active_agent_blueprint_root_id(state.app, state.sid)
        active_blueprint_agent_ids = _runtime_active_agent_blueprint_agent_ids(state.app, state.sid)
        if (
            not state.turn_agent_id
            and active_blueprint_root_id
            and state.active_agent_id in {"", "main", "default"}
        ):
            state.active_agent_id = active_blueprint_root_id
        routing_mode = getattr(state.sess, "routing_mode", "auto") or "auto"
        state.invocation_agent_id = state.active_agent_id or "orchestrator"
        _emit_semantic_event(
            state.app,
            state.sid,
            "agent.invocation.started",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="running",
            summary=f"Invoking {state.invocation_agent_id}.",
            actor={"agent_id": state.invocation_agent_id},
            subject={"message_id": state.user_msg.id},
            payload={
                "routing_mode": routing_mode,
                "session_agent_id": session_agent_id,
                "turn_agent_id": state.turn_agent_id,
                "active_blueprint_root_id": active_blueprint_root_id,
                "active_blueprint_agent_ids": active_blueprint_agent_ids,
            },
        )
        from clio_agent.agent import cancellation_checker as _cancellation_checker  # noqa: PLC0415

        _refresh_argonne_lm_token(state.app.state.agent)

        if (
            state.active_agent_id not in _EXECUTABLE_SESSION_AGENT_IDS
            or state.active_agent_id in active_blueprint_agent_ids
        ):
            prompt_registry_factory = getattr(state.app.state, "prompt_registry_for_request", None)
            prompt_registry = (
                prompt_registry_factory(session_id=state.sid)
                if callable(prompt_registry_factory)
                else None
            )
            dynamic_agent = _resolve_runtime_dynamic_agent(
                state.app,
                state.active_agent_id,
                session_id=state.sid,
                prompt_registry=prompt_registry,
            )
            if dynamic_agent is None:
                raise _UnsupportedSessionAgent(state.active_agent_id)
            state.prompt_resolution = dict(dynamic_agent.metadata.get("prompt_resolution") or {})
            state.dynamic_agent_used = dynamic_agent
            runner = _blueprint_runner_for_agent(dynamic_agent)
            dynamic_kind = (
                _blueprint_module_kind(dynamic_agent)
                if _agent_definition_uses_blueprint_runtime(dynamic_agent)
                else ""
            )
            execution_mode = (
                f"blueprint_{dynamic_kind}"
                if dynamic_kind
                else ("tool_agent" if dynamic_agent.tools else "prompt_agent")
            )
            state.agent_runtime = _dynamic_agent_runtime_provenance(
                state.app,
                dynamic_agent,
                execution_mode=execution_mode,
            )
            # The keystone (set_turn_identity) already binds active_app() for the
            # whole turn, so no _gact_app_context wrapper is needed here.
            session_token = _ctx.set_session_id(state.sid)
            try:
                module = (
                    _build_blueprint_dspy_module(state.app.state.agent, dynamic_agent)
                    if _agent_definition_uses_blueprint_runtime(dynamic_agent)
                    else (
                        _build_tool_user_agent_module(state.app.state.agent, dynamic_agent)
                        if dynamic_agent.tools
                        else _build_prompt_user_agent_module(state.app.state.agent, dynamic_agent)
                    )
                )
            finally:
                _ctx.reset(session_token)
            llm_actor = {
                "agent_id": dynamic_agent.id,
                "agent_title": dynamic_agent.title,
                "source": dynamic_agent.source,
                "execution_mode": execution_mode,
            }
            llm_subject = {
                "prompt_id": dynamic_agent.prompt_id,
                "prompt_profile": dynamic_agent.prompt_profile,
                "message_id": state.user_msg.id,
            }
            _emit_semantic_event(
                state.app,
                state.sid,
                "llm.request.started",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="running",
                summary=f"LLM request started for {dynamic_agent.id}.",
                actor=llm_actor,
                subject=llm_subject,
                blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
                provider=_llm_provider_payload(state.app, dynamic_agent.id),
                payload={
                    "request_mode": "streamed",
                    "input": state.enriched_text,
                    "prompt_resolution": state.prompt_resolution,
                    "agent_runtime": state.agent_runtime,
                    "native_image_count": len(state.native_images),
                },
            )
            with _cancellation_checker(cancel_requested), _tool_session_context(state.sid):
                state.pred = await _await_turn_work(
                    _try_streamed_forward_compat(
                        state.app,
                        state.enriched_text,
                        state.sid,
                        live_emit,
                        session_mode=getattr(state.sess, "mode", "chat"),
                        session_edit_mode=getattr(state.sess, "edit_mode", "diff"),
                        agent_override=module,
                        images=state.native_images,
                        cancel_requested=cancel_requested,
                    )
                )
            if state.pred is not None:
                _emit_semantic_event(
                    state.app,
                    state.sid,
                    "llm.response.completed",
                    turn_id=state.turn_id,
                    trace_id=state.trace_id,
                    summary=f"LLM response completed for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(state.app, dynamic_agent.id),
                    payload=_prediction_summary(state.pred),
                )
            if state.pred is None:
                _emit_semantic_event(
                    state.app,
                    state.sid,
                    "llm.request.started",
                    turn_id=state.turn_id,
                    trace_id=state.trace_id,
                    status="running",
                    summary=f"Synchronous LLM request started for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(state.app, dynamic_agent.id),
                    payload={
                        "request_mode": "sync",
                        "input": state.enriched_text,
                        "prompt_resolution": state.prompt_resolution,
                        "agent_runtime": state.agent_runtime,
                        "native_image_count": len(state.native_images),
                    },
                )
                with _cancellation_checker(cancel_requested), _tool_session_context(state.sid):
                    loop = asyncio.get_running_loop()
                    turn_context = contextvars.copy_context()
                    state.pred = await _await_turn_work(
                        loop.run_in_executor(
                            None,
                            lambda: turn_context.run(
                                _run_dynamic_agent_compat,
                                runner,
                                state.app.state.agent,
                                dynamic_agent,
                                state.enriched_text,
                                state.sid,
                                cancel_requested,
                            ),
                        ),
                    )
                _emit_semantic_event(
                    state.app,
                    state.sid,
                    "llm.response.completed",
                    turn_id=state.turn_id,
                    trace_id=state.trace_id,
                    summary=f"Synchronous LLM response completed for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(state.app, dynamic_agent.id),
                    payload=_prediction_summary(state.pred),
                )
        else:
            # Honour the session's routing override. routing_mode "chat"
            # forces the chat path (no /chat prefix needed); "experts"
            # rejects chat/none classifications. Keep the override scoped
            # to this turn context so concurrent sessions do not mutate the
            # shared ClioAgent instance.
            routing_override = routing_mode
            from clio_agent.agent import routing_mode_override as _routing_override  # noqa: PLC0415

            with _routing_override(routing_override), _cancellation_checker(cancel_requested):
                with _tool_session_context(state.sid):
                    llm_actor = {
                        "agent_id": state.active_agent_id or "orchestrator",
                        "source": "builtin",
                        "execution_mode": "clio_agent_forward",
                    }
                    llm_subject = {"message_id": state.user_msg.id}
                    _emit_semantic_event(
                        state.app,
                        state.sid,
                        "llm.request.started",
                        turn_id=state.turn_id,
                        trace_id=state.trace_id,
                        status="running",
                        summary="LLM request started for CLIO orchestrator.",
                        actor=llm_actor,
                        subject=llm_subject,
                        provider=_llm_provider_payload(state.app, state.active_agent_id or "orchestrator"),
                        payload={
                            "request_mode": "streamed",
                            "routing_mode": routing_override,
                            "session_mode": getattr(state.sess, "mode", "chat"),
                            "edit_mode": getattr(state.sess, "edit_mode", "diff"),
                            "input": state.enriched_text,
                            "native_image_count": len(state.native_images),
                        },
                    )
                    state.pred = await _await_turn_work(
                        _try_streamed_forward_compat(
                            state.app,
                            state.enriched_text,
                            state.sid,
                            live_emit,
                            session_mode=getattr(state.sess, "mode", "chat"),
                            session_edit_mode=getattr(state.sess, "edit_mode", "diff"),
                            images=state.native_images,
                            cancel_requested=cancel_requested,
                        )
                    )
                    if state.pred is not None:
                        _emit_semantic_event(
                            state.app,
                            state.sid,
                            "llm.response.completed",
                            turn_id=state.turn_id,
                            trace_id=state.trace_id,
                            summary="LLM response completed for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(state.app, state.active_agent_id or "orchestrator"),
                            payload=_prediction_summary(state.pred),
                        )
                    if state.pred is None:
                        _emit_semantic_event(
                            state.app,
                            state.sid,
                            "llm.request.started",
                            turn_id=state.turn_id,
                            trace_id=state.trace_id,
                            status="running",
                            summary="Synchronous LLM request started for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(state.app, state.active_agent_id or "orchestrator"),
                            payload={
                                "request_mode": "sync",
                                "routing_mode": routing_override,
                                "session_mode": getattr(state.sess, "mode", "chat"),
                                "edit_mode": getattr(state.sess, "edit_mode", "diff"),
                                "input": state.enriched_text,
                                "native_image_count": len(state.native_images),
                            },
                        )
                        loop = asyncio.get_running_loop()
                        turn_context = contextvars.copy_context()
                        state.pred = await _await_turn_work(
                            loop.run_in_executor(
                                None,
                                lambda: turn_context.run(
                                    _agent_forward_compat,
                                    state.app.state.agent,
                                    state.enriched_text,
                                    state.sid,
                                    getattr(state.sess, "mode", "chat"),
                                    getattr(state.sess, "edit_mode", "diff"),
                                    cancel_requested,
                                    state.native_images,
                                ),
                            ),
                        )
                        _emit_semantic_event(
                            state.app,
                            state.sid,
                            "llm.response.completed",
                            turn_id=state.turn_id,
                            trace_id=state.trace_id,
                            summary="Synchronous LLM response completed for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(state.app, state.active_agent_id or "orchestrator"),
                            payload=_prediction_summary(state.pred),
                        )
        if state.dynamic_agent_used is not None and state.dynamic_agent_used.source == "expert_pack":
            state.pred, state.expert_handoffs = await settle_dynamic_agent_delegations(
                state,
                state.dynamic_agent_used,
                state.pred,
                source_text=state.enriched_text,
            )
        _emit_semantic_event(
            state.app,
            state.sid,
            "agent.invocation.completed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            summary=f"{state.invocation_agent_id} returned a prediction.",
            actor={"agent_id": state.invocation_agent_id},
            subject={"message_id": state.user_msg.id},
            payload={
                "selected_expert": getattr(state.pred, "selected_expert", "") or "",
                "route_source": getattr(state.pred, "route_source", "") or "",
                "has_answer": bool(getattr(state.pred, "answer", "") or ""),
                "has_error_info": bool(getattr(state.pred, "error_info", None)),
            },
        )

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
        ask_user_action = _coerce_ask_user_action(state.pred)
        if state.error_info is None and ask_user_action:
            now_iso = datetime.now(timezone.utc).isoformat()
            options = _ask_user_options_from_action(ask_user_action)
            kind_raw = str(ask_user_action.get("kind") or "").strip()
            kind = kind_raw if kind_raw in {"freeform", "choice", "confirmation"} else ""
            if not kind:
                kind = (
                    "choice"
                    if options and not ask_user_action.get("allow_freeform")
                    else "freeform"
                )
            question = UserQuestion(
                id=_new_question_id(),
                session_id=state.sid,
                prompt=str(ask_user_action["question"]),
                status="pending",
                kind=kind,  # type: ignore[arg-type]
                options=options,
                created_at=now_iso,
                updated_at=now_iso,
                source="orchestrator_action",
                turn_id=state.user_msg.id,
                attempt_id=state.retry_attempt_id,
                metadata={
                    **dict(ask_user_action.get("metadata") or {}),
                    "reason": ask_user_action.get("reason", ""),
                    "caller": ask_user_action.get("caller", {}),
                    "resume_on_answer": True,
                    "source_user_message_id": state.user_msg.id,
                    "source_user_text": state.user_text,
                    "selected_agent": state.selected_agent,
                    "route_source": state.route_source,
                    "route_reason": state.route_reason,
                },
            )
            state.app.state.user_questions[question.id] = question
            _emit_semantic_event(
                state.app,
                state.sid,
                "user_question.created",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="waiting_user",
                summary="Agent requested user input before continuing.",
                actor={"agent_id": state.selected_agent or state.invocation_agent_id},
                subject={"question_id": question.id},
                payload=question.model_dump(exclude_none=True),
            )
            updated = state.app.state.sessions.update(
                state.sid,
                status="waiting_user",
                message_count=len(state.app.state.messages.get(state.sid, [])),
                metadata_patch={"pending_user_question_id": question.id},
            )
            _finalize_context_frame(
                state.app,
                state.sid,
                state.context_frame["id"],
                "",
                "completed",
                error_info=None,
            )
            state.bus.publish(
                Event(
                    type="user_question.created",
                    session_id=state.sid,
                    payload=question.model_dump(exclude_none=True),
                )
            )
            state.bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=state.sid,
                    payload={
                        "session_id": state.sid,
                        "status": "waiting_user",
                        "prev_status": "running",
                        "updated_at": updated.updated_at if updated is not None else "",
                        "pending_user_question_id": question.id,
                    },
                )
            )
            if state.retry_attempt_id:
                _update_retry_attempt(
                    "completed",
                    metadata_patch={
                        "ask_user_question_id": question.id,
                        "stop_reason": "waiting_user",
                    },
                )
            # #767 PR2: the ask_user pause exits the turn before the finalize
            # region — settle the ledger so it can never poison a later turn.
            # The in-flight assistant identity/parts stay in the legacy dicts
            # (deliberately NOT popped here, exactly as before) and the resume
            # turn re-adopts them via _open_turn_transcript's carried-state
            # adoption, so the resumed turn continues the same assistant
            # message without a second message.created.
            settle_turn_transcript(state)
            return
        # iowarp/clio-agent#25: data branch reports which execution
        # path it took ("fast" or "expert_loop"). Empty when not
        # populated by ClioAgent.forward (older code paths, non-data
        # branches not yet migrated).
        state.execution_path = getattr(state.pred, "execution_path", "") or ""
        state.tools_called = _extract_tools_called(state.pred)
        top_level_workflow_state = _prediction_workflow_state(state.pred)
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
        # CLIO-BBBBBBBBBB24: cost + token rollup — mutate
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
            enforce_dict_bound(state.app, state.app.state.permissions, "permissions", session_id=state.sid)
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
            state.bus.publish(
                Event(
                    type="permission.requested",
                    session_id=state.sid,
                    payload=row,
                )
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
        state.error_info = _cancelled_error_info(
            state.sid,
            execution_cancellation="best_effort",
            executor_work_may_continue=True,
        )
        state.answer_text = ""
        state.tools_called = []
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
    except _UnsupportedSessionAgent as exc:
        state.selected_agent = exc.agent_id
        state.rationale = (
            "Session selected an agent that is registered but not executable "
            "by CLIO's current runtime."
        )
        state.error_info = ErrorInfo(
            error="not_implemented",
            message=(f"Session agent {exc.agent_id!r} cannot be executed yet."),
            details={
                "agent_id": exc.agent_id,
                "reason": exc.reason,
                "supported_agent_ids": sorted(
                    agent_id for agent_id in _EXECUTABLE_SESSION_AGENT_IDS if agent_id
                ),
                "unsupported_tools": exc.tools,
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
    except Exception as exc:  # noqa: BLE001
        state.error_info = ErrorInfo(
            error="agent_error",
            message=f"agent.forward raised: {exc}",
            details={"original_error": type(exc).__name__},
            recoverable=True,
        )

    # #756: everything below (answer grounding, part assembly, diff indexing,
    # nanoagent spawn, publishes, persistence) runs inside a fire-and-forget
    # task; an exception escaping it used to vanish (the done-callback only
    # pops in_flight_turns) and wedge the session in 'running' with no
    # completion event. Run it under the turn's error envelope instead.
    try:
        if state.error_info is None and not state.answer_text and state.expert_handoffs:
            state.answer_text = _fallback_answer_from_delegation(state.expert_handoffs)

        # Final user-facing text only: correct any fabricated local artifact (csv/png)
        # path the answer presents as produced — whether the synthesizing expert
        # composed a plausible-but-wrong filename or the delegation-fallback text
        # carried a model-requested ``output_path`` that the tool never wrote — by
        # grounding it against the run's verified on-disk artifacts in the merged
        # typed workflow_state. Generic (typed state + filesystem only), applied once
        # on the assembled answer, never on intermediate child rows.
        if state.answer_text and state.expert_handoffs:
            state.answer_text = _ground_fabricated_local_artifact_paths(
                state.answer_text,
                _workflow_state_from_handoff_rows(state.expert_handoffs),
            )

        # Build assistant parts — routing_decision (v0.2) first when we
        # got a selected_agent, then optional thinking trace, then the
        # text answer, then any file_diffs.
        if (
            state.error_info is None
            and not state.answer_text
            and not state.thinking_text
            and not state.proposed_diffs
            and not state.nanoagents
        ):
            state.error_info = ErrorInfo(
                error="empty_response",
                message="Agent completed without user-visible output.",
                details={
                    "session_id": state.sid,
                    "routing_mode": getattr(state.sess, "routing_mode", "auto"),
                    "selected_agent": state.selected_agent,
                },
                recoverable=True,
            )

        # ---- #767 PR3: finalize is a READER of the TurnTranscript ledger. ----
        # Live parts already streamed at the moment they happened; finalize only
        # appends ITS OWN parts (route banner, wrap-up thinking, the canonical
        # answer channel, file diffs) through the same producer API and then
        # persists the ledger verbatim. No live-parts scans, no rebuild-from-rows,
        # no text swap, no dedup, no re-publish.
        #
        # Capture stream provenance BEFORE any finalize-time append: an atomic
        # append is the runtime boundary and clears ``current_stream_part_id``
        # (the legacy closure var was only reset by mid-turn boundaries).
        current_stream_part_id = state.transcript.current_stream_part_id
        live_assistant_parts = state.transcript.snapshot()
        has_live_parts = bool(live_assistant_parts or current_stream_part_id)
        live_tool_calls = {
            p.call_id: p for p in live_assistant_parts if p.type == "tool_call" and p.call_id
        }
        for part in live_assistant_parts:
            if part.type != "tool_result" or not part.call_id:
                continue
            call_part = live_tool_calls.get(part.call_id)
            if call_part is None:
                continue
            for row in state.tools_called:
                if str(row.get("name") or "") != call_part.tool_name:
                    continue
                if row.get("args") != call_part.input:
                    continue
                if "result" not in row:
                    continue
                part.content = [
                    Part(
                        id=f"{part.id}_final_text",
                        type="text",
                        text=_tool_result_preview(row.get("result")),
                    )
                ]
                break

        # The expert that produced this turn's thinking/answer/diff parts: the routed
        # expert when one was selected, else the active orchestrator.
        responder_agent_id = state.selected_agent or state.invocation_agent_id or "main"
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
        # Mechanism 1 replaced: the once-key IS the identity — the same
        # ``route:{agent}`` key the live tool observer uses, so the banner lands
        # exactly once whether it streamed live or lands here.
        if state.selected_agent:
            state.transcript.append_part_once(
                f"route:{state.selected_agent}",
                Part(
                    id=_new_part_id(),
                    type="routing_decision",
                    # The decision is MADE by the orchestrator; ``selected_agent`` is the
                    # CHOSEN expert.
                    agent_id=state.invocation_agent_id or "main",
                    metadata={
                        k: v
                        for k, v in {
                            "route_source": state.route_source,
                            "route_reason": state.route_reason,
                        }.items()
                        if v
                    },
                    selected_agent=state.selected_agent,
                    rationale=state.rationale,
                    confidence=0.0,
                    heuristic=False,
                    execution_path=state.execution_path,
                ),
                stream_source="batch",
            )
        # Mechanism 2 replaced: there is no finalize rebuild-from-rows — delegation
        # appended its expert_handoff parts once, at emit time; the
        # ``expert_handoffs`` rows stay message METADATA only (design §4 row 6).
        #
        # iowarp/clio-agent#17: surface DSPy reasoning as a thinking Part so the TUI
        # can collapse + render it. Gated by op identity (has_closed_text replaces
        # the suppressed_thinking_part substring matching): when the responder's
        # contract reasoning already streamed live as a text part this turn, the
        # wrap-up copy is that same channel and must not land twice.
        #
        # Close the still-open streamed part FIRST: ``has_closed_text`` reads
        # closed state only, and on the chat path (no selected_agent -> the
        # routing-banner append above never ran to close it) a turn that
        # streamed ``reasoning`` and returned a batch-only answer still holds
        # that reasoning part OPEN here — the gate saw "nothing landed" and
        # appended a verbatim batch ``thinking`` twin (the #732 duplicate
        # class). On routed turns the banner's ``append_part`` already closed
        # it, so this is a no-op there. An explicit close deliberately does
        # NOT reset ``current_stream_part_id`` (captured above), so the
        # live-vs-batch stream provenance below is unchanged; the canonical
        # answer channel was taken above, while its part could still be open.
        state.transcript.close_open_text()
        if state.thinking_text and not state.transcript.has_closed_text(responder_agent_id, "reasoning"):
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
        # final_text — there is nothing to swap). Structured (JSON) answers stay
        # out of the visible transcript, as before.
        stream_fallback = _pop_stream_fallback(state.app, state.sid)
        batch_turn_text = current_stream_part_id is None
        if (
            batch_turn_text
            and (bool(state.answer_text) or state.error_info is not None)
            and not stream_fallback
        ):
            stream_fallback = _stream_fallback_payload("sync_execution_path")
        answer_channel.finish(
            fallback_text=(
                "" if _looks_like_structured_answer(state.answer_text) else str(state.answer_text or "")
            ),
            fallback_metadata=(
                {"stream_fallback": stream_fallback} if stream_fallback and batch_turn_text else {}
            ),
        )
        for row in state.proposed_diffs:
            if isinstance(row, dict):
                getf = row.get
            else:

                def getf(k, default=None, _r=row):
                    return getattr(_r, k, default)

            path = getf("path", "") or ""
            udiff = getf("unified_diff", "") or ""
            new_content = getf("new_content", "") or ""
            edit_mode = getf("edit_mode", "") or ""
            lines_added = int(getf("lines_added", 0) or 0)
            lines_removed = int(getf("lines_removed", 0) or 0)
            if not path:
                continue
            # In "whole" mode the unified_diff may be empty by design;
            # the new_content carries the full replacement. Accept either
            # so the Part lands instead of being dropped.
            if not udiff and not new_content:
                continue
            diff_part = Part(
                id=_new_part_id(),
                type="file_diff",
                agent_id=responder_agent_id,
                path=path,
                unified_diff=udiff,
                new_content=new_content,
                status="pending",
                edit_mode=edit_mode,
                lines_added=lines_added,
                lines_removed=lines_removed,
            )
            state.transcript.append_part(diff_part, stream_source="batch")
            _emit_semantic_event(
                state.app,
                state.sid,
                "artifact.proposed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary=f"Agent proposed a file diff for {path}.",
                actor={"agent_id": state.selected_agent or state.invocation_agent_id},
                subject={"path": path, "part_id": diff_part.id, "artifact_type": "file_diff"},
                payload={
                    "path": path,
                    "unified_diff": udiff,
                    "new_content": new_content,
                    "edit_mode": edit_mode,
                    "lines_added": lines_added,
                    "lines_removed": lines_removed,
                },
            )

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
        # ``current_stream_part_id`` (captured BEFORE the finalize appends above)
        # keeps the legacy semantic: a text part opened since the last mid-turn
        # runtime boundary marks the turn's text as live-streamed even after it
        # closed. Per-part ``stream_source`` is no longer restamped here — every
        # part carries the provenance its producer appended it with (#767 PR3:
        # finalize never rewrites the ledger).
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
        # A live observer completion can arrive after the immediate post-forward drain
        # but before the assistant message is persisted. Reconcile once more at the
        # final metadata boundary so reloads retain the same tool facts as the live bus.
        if not state.cancelled_turn:
            state.tools_called = _drain_observed_tool_calls(state.tools_called)
        state.tools_called = _sanitize_tools_called_metadata(state.tools_called)
        if state.tools_called:
            state.assistant_metadata["tools_called"] = state.tools_called
        if state.expert_handoffs:
            state.expert_handoffs = [
                _sanitize_handoff_tool_metadata(row) if isinstance(row, Mapping) else row
                for row in state.expert_handoffs
            ]
            state.assistant_metadata["expert_handoffs"] = state.expert_handoffs
        if state.context_file_provenance["files"]:
            state.assistant_metadata["context_files"] = state.context_file_provenance
        if state.memory_search_metadata:
            state.assistant_metadata["memory_search"] = state.memory_search_metadata
        if state.agent_runtime:
            state.assistant_metadata["agent_runtime"] = state.agent_runtime
        if state.prompt_resolution:
            state.assistant_metadata["prompt_resolution"] = state.prompt_resolution
        # Reasoning capture: log the chain-of-thought tokens for EVERY LM call this
        # turn (planner + each expert + chat), extracted from dspy.lm.history. Most
        # stacks discard reasoning_content; we persist (question, reasoning, response)
        # on the assistant message metadata because the reasoning has scientific
        # value for analysing how the model reached its answer. Gated by
        # CLIO_CAPTURE_REASONING (default on); set to 0 to avoid the metadata growth.
        if os.environ.get("CLIO_CAPTURE_REASONING", "").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }:
            try:
                _reasoning_log = _reasoning_records_from_history_slice(state.history_start, state.app)
            except Exception:  # noqa: BLE001 - reasoning capture is best-effort, never fail a turn
                _reasoning_log = []
            if _reasoning_log:
                state.assistant_metadata["reasoning_log"] = _reasoning_log
                trace.event(
                    "REASONING",
                    "captured %d call(s): %s",
                    len(_reasoning_log),
                    "; ".join(
                        f"{(r['model'] or '?').split('/')[-1]}={r['reasoning_chars']}c"
                        for r in _reasoning_log
                    ),
                )
        # iowarp/clio-agent#6: the transcript is the sole minter of the assistant
        # message id — reuse it when a producer already minted it (stream tap /
        # tool observer / the finalize appends above); a turn with no parts at
        # all mints + publishes message.created here, exactly once.
        asst_id = state.transcript.ensure_message()
        # #767 PR3: persist the ledger VERBATIM. finalize() closes any still-open
        # streamed part (publishing its completed event with the cleaned buffer),
        # stamps the 1-based arrival-order ``sequence`` (#731: reload order IS
        # stream order, by construction), freezes the ledger against late
        # producers, and returns the parts. No text rewriting, no dedup, no
        # re-publish — live and reload are two projections of this one ledger.
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
            cost_usd=state.turn_cost,
            stop_reason="cancelled" if state.cancelled_turn else ("error" if state.error_info else "end_turn"),
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

        # Index file_diff parts so /diffs/apply + /diffs/reject find them.
        bucket = state.app.state.pending_diffs.setdefault(state.sid, [])
        for p in assistant_parts:
            if p.type != "file_diff":
                continue
            write_content = (
                p.new_content if p.new_content or p.edit_mode in {"whole", "patch"} else None
            )
            bucket.append(
                {
                    "path": p.path,
                    "unified_diff": p.unified_diff,
                    "new_content": write_content,
                    "status": "pending",
                    "part_id": p.id,
                    "message_id": assistant_msg.id,
                }
            )
        enforce_list_bound(state.app, bucket, "pending_diffs", session_id=state.sid)

        # Materialise nanoagent spawns + publish their lifecycle events.
        spawn_nanoagents(state, state.nanoagents, assistant_msg, state.sess)

        # #767 PR3: finalize re-publishes NOTHING — every part's message.created /
        # part.added / part.delta / part.completed already went out at append
        # time, from the one producer API. Tool lifecycle events are only emitted
        # by the live observer at the execution boundary. Prediction.tools_called
        # remains summary metadata; do not reconstruct started/completed events
        # after the turn, because that makes post-hoc facts look like live tool
        # timing.
        completed_payload: dict[str, Any] = {
            "turn_id": state.turn_id,
            "message_id": assistant_msg.id,
            "stop_reason": "cancelled"
            if state.cancelled_turn
            else ("error" if state.error_info else "end_turn"),
            "tokens": dict(state.turn_tokens),
            "cost_usd": state.turn_cost,
        }
        if state.error_info is not None:
            completed_payload["error_info"] = state.error_info.model_dump(exclude_none=True)
        if state.assistant_metadata:
            completed_payload["metadata"] = state.assistant_metadata
        # Embed the full final assistant message in the DURABLE turn.completed so the
        # messages store is derivable from the canonical trace (the trace is the
        # source of truth). final_message is in SENSITIVE_KEYS, so the SSE projection
        # strips it -- the message already streams to clients via message.* events.
        semantic_completed_payload = {
            **completed_payload,
            "final_message": assistant_msg.model_dump(exclude_none=True),
        }
        _emit_semantic_event(
            state.app,
            state.sid,
            "turn.completed" if state.error_info is None else "turn.failed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="completed" if state.error_info is None else "failed",
            summary=(
                "CLIO turn completed."
                if state.error_info is None
                else f"CLIO turn failed: {state.error_info.error}."
            ),
            actor={"agent_id": state.selected_agent or "orchestrator"},
            subject={"message_id": assistant_msg.id},
            payload=semantic_completed_payload,
        )
        _publish_transcript_event(
            state.bus,
            state.sid,
            "turn.completed",
            {"turn_id": state.turn_id},
        )
        state.bus.publish(
            Event(
                type="message.completed",
                session_id=state.sid,
                payload=completed_payload,
            )
        )

        # Persist + settle.
        final_status = "cancelled" if state.cancelled_turn else ("error" if state.error_info else "idle")
        retry_status = "cancelled" if state.cancelled_turn else ("failed" if state.error_info else "completed")
        _append_session_message(state.app, state.sid, assistant_msg)
        # #767 PR3: the ledger is already frozen by transcript.finalize(); settle
        # retires it from the registry so a late producer op is rejected +
        # audited, never absorbed silently.
        settle_turn_transcript(state)
        getattr(state.app.state, "live_assistant_message_ids", {}).pop(state.sid, None)
        getattr(state.app.state, "live_assistant_parts", {}).pop(state.sid, None)
        getattr(state.app.state, "live_assistant_part_keys", {}).pop(state.sid, None)
        # #757: the streamed-field buffer is per-turn; leaving it grows without bound
        # and makes later turns' suppression matchers eat legitimate thinking parts.
        _clear_live_streamed_field_text(state.app, state.sid)
        _update_retry_attempt(
            retry_status,
            metadata_patch={
                "executed_user_message_id": state.user_msg.id,
                "assistant_message_id": assistant_msg.id,
                "stop_reason": completed_payload["stop_reason"],
            },
        )
        state.app.state.sessions.update(
            state.sid,
            status=final_status,
            message_count=state.sess.message_count + 2,
            add_tokens_input=state.turn_tokens["input"],
            add_tokens_output=state.turn_tokens["output"],
            add_cost_usd=state.turn_cost,
        )
        cancellation_status: dict[str, Any] = {}
        if state.cancelled_turn and state.error_info is not None:
            cancellation_status = {
                "execution_cancellation": state.error_info.details.get("execution_cancellation"),
                "executor_work_may_continue": state.error_info.details.get("executor_work_may_continue"),
                "cancellation_attempt": state.error_info.details.get("cancellation_attempt", {}),
            }
        state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=state.sid,
                payload={
                    "session_id": state.sid,
                    "status": final_status,
                    "prev_status": "running",
                    **cancellation_status,
                },
            )
        )
        # iowarp/clio-agent#20: post_message hook runs AFTER persistence
        # so user audit code sees the settled assistant + can ship to
        # external systems. Errors are swallowed (post_* contract).
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _emit_semantic_event(
                state.app,
                state.sid,
                "hook.invocation.started",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="running",
                summary="post_message hook dispatch started.",
                actor={"hook": "post_message"},
                subject={"message_id": assistant_msg.id},
                payload={"assistant": assistant_msg.model_dump(exclude_none=True)},
            )
            _fire_hook(
                "post_message",
                state.sid,
                assistant_msg.model_dump(exclude_none=True),
                hook_scope={
                    "session_id": state.sid,
                    "workspace_id": getattr(state.sess, "workspace_id", ""),
                    "blueprint_id": _runtime_active_agent_blueprint_id(state.app, state.sid),
                },
            )
            _emit_semantic_event(
                state.app,
                state.sid,
                "hook.invocation.completed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary="post_message hook dispatch completed.",
                actor={"hook": "post_message"},
                subject={"message_id": assistant_msg.id},
                payload={},
            )
        except Exception:  # noqa: BLE001
            _emit_semantic_event(
                state.app,
                state.sid,
                "hook.invocation.failed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="failed",
                summary="post_message hook dispatch failed and was swallowed by policy.",
                actor={"hook": "post_message"},
                subject={"message_id": assistant_msg.id},
                payload={},
            )
            pass
        if not (
            state.cancelled_turn
            and state.error_info is not None
            and state.error_info.details.get("execution_cancellation") == "best_effort"
        ):
            if state.app.state.cancel_events.get(state.sid) is state.turn_cancel_event:
                state.app.state.cancel_events.pop(state.sid, None)
    except Exception as finalize_exc:  # noqa: BLE001 - detached task: settle, no re-raise
        _settle_failed_finalize(
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
) -> Message:
    """Stage a user turn and drive it off-thread.

    Persists the user message + parts, flips the session to ``running``,
    publishes ``session.status_changed`` + ``message.created``, then schedules
    :func:`_run_turn_in_background` as a tracked ``asyncio`` task (registered in
    ``app.state.in_flight_turns`` so cancellation can reach it). Returns the
    staged user :class:`Message`.

    Hoisted out of ``build_app`` (#714) so the POST-message / question-answer /
    retry-attempt / scheduler callers can share it via ``GactDeps`` without
    importing back into :mod:`clio_agent.gact.app`; ``app`` is now an explicit
    first argument instead of a closure capture.
    """
    # #714 danger set: bind through app at call time so test monkeypatches of
    # clio_agent.gact.app._append_session_message keep intercepting persistence.
    from clio_agent.gact.app import _append_session_message  # noqa: PLC0415

    now = time.time()
    user_metadata = dict(metadata or {})
    user_parts = _user_message_parts(
        request_parts=list(request_parts or []),
        user_text=user_text,
    )
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
    user_msg_id = _new_message_id("user")
    user_msg = Message(
        id=user_msg_id,
        # The turn id IS the user message id (#711); a user message correlates to
        # its own turn.
        turn_id=user_msg_id,
        session_id=sid,
        role="user",
        created_at=_iso_from_epoch(now),
        updated_at=_iso_from_epoch(now),
        parts=user_parts,
        metadata=user_metadata,
    )

    _append_session_message(app, sid, user_msg)
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

    task = asyncio.create_task(
        _run_turn_in_background(app, sid, user_text, user_msg, turn_agent_id)
    )
    app.state.in_flight_turns[sid] = task

    def _drop_task(_t, _sid=sid) -> None:
        cur = app.state.in_flight_turns.get(_sid)
        if cur is _t:
            app.state.in_flight_turns.pop(_sid, None)

    task.add_done_callback(_drop_task)
    return user_msg
