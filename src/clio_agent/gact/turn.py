"""Turn-orchestration engine for the GACT server (#714).

This module owns the *turn loop* — the off-request-thread machinery that drives
one agent turn end to end:

* :func:`_start_background_user_turn` stages a user message + parts, flips the
  session to ``running``, publishes the ``session.status_changed`` /
  ``message.created`` events, and schedules the turn as a tracked ``asyncio``
  task (so cancellation can reach it via ``app.state.in_flight_turns``).
* :func:`_run_turn_in_background` is the body of that task: it invokes
  ``agent.forward`` in an executor, streams/slices the result into Parts,
  settles dynamic-agent delegations (the nested
  ``_settle_dynamic_agent_delegations`` / ``_execute_delegated_experts``
  helpers), publishes every SSE event the TUI consumes, persists the assistant
  message, records the context frame + token/cost usage, and returns the
  session to ``idle`` (or ``error``).

It was carved verbatim out of ``clio_agent.gact.app.build_app`` so the route
factories (post-message, question-answer, retry-attempt, schedules) and the
scheduler tick can share the entrypoint via ``GactDeps`` without importing back
into the 24k-line app module. To keep the import graph acyclic, the many
app-resident turn helpers this engine calls are imported *lazily* (function-local
``from clio_agent.gact.app import ...``) — the same cycle-break pattern
``clio_agent.gact.agents.builders`` uses. The cross-concern funnel + id/exception
helpers come from :mod:`clio_agent.gact.runtime.globals`, which imports only
leaf modules and never ``app``.

Behavior is byte-for-byte identical to the in-``build_app`` original: the
threading/executor handoff, cooperative + hard cancellation, turn timeout, the
``_ctx`` contextvar set/copy_context semantics, and the trajectory/SSE emission
are all preserved unchanged.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.runtime.globals import (
    _coerce_error_info,
    _ContextFileAccessError,
    _emit_semantic_event,
    _gact_app_context,
    _iso_from_epoch,
    _llm_provider_payload,
    _new_message_id,
    _new_part_id,
    _new_question_id,
    _semantic_trace_id,
    _tool_session_context,
    _TurnCancelled,
    _TurnTimedOut,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.types import (
    ErrorInfo,
    Message,
    Part,
    Session,
    Tokens,
    UserQuestion,
)
from clio_agent.runtime import trace
from clio_agent.runtime.lm_activity import lm_call_in_flight as _lm_call_in_flight

# NOTE: The agent-builder / agent-resolution / provider-auth / workflow-state
# helpers this engine drives are imported *lazily* from
# :mod:`clio_agent.gact.app` (function-local, below) rather than from their owning
# modules. The turn loop originally lived in ``app.py`` and resolved them through
# the ``app`` module namespace (the re-export shims), so tests monkeypatch e.g.
# ``clio_agent.gact.app._build_tool_user_agent_module``. Reading them back through
# ``app`` at call time keeps that monkeypatch contract -- and behavior -- exactly
# as it was before the move. The funnel/id/exception helpers above come from
# ``runtime.globals`` (their single owner + the documented test-patch site).

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef  # noqa: F401


def _dedup_cross_agent_text(parts: list[Part]) -> list[Part]:
    """Drop a ``text`` part that verbatim-duplicates an earlier one by another author.

    iowarp/clio-agent#736: after a terminal child (e.g. ``synthesis``) returns its
    deliverable, the resumed orchestrator (``main``) frequently re-emits that child's
    answer **verbatim** as its own ``text`` part, so the final brief lands twice in the
    persisted transcript — once child-authored, once parent-authored. This drops the
    later, cross-agent byte-identical copy and keeps the original (child-authored) part.

    Narrow by design: only a ``text`` part whose *stripped* content exactly matches an
    EARLIER ``text`` part authored by a DIFFERENT ``agent_id`` is removed. The
    orchestrator's own distinct wrap-up (its ``thinking`` part — different type and
    content) and any legitimate same-author repetition are left untouched. Arrival
    order of the surviving parts is preserved.
    """
    seen_author_by_text: dict[str, str] = {}
    kept: list[Part] = []
    for part in parts:
        if part.type == "text":
            normalized = (part.text or "").strip()
            if normalized:
                prior_author = seen_author_by_text.get(normalized)
                if prior_author is not None and prior_author != part.agent_id:
                    continue
                seen_author_by_text.setdefault(normalized, part.agent_id)
        kept.append(part)
    return kept


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
    from clio_agent.gact.app import (  # noqa: PLC0415
        _EXECUTABLE_SESSION_AGENT_IDS,
        _agent_definition_uses_blueprint_runtime,
        _agent_forward_compat,
        _append_accumulated_workflow_state_context,
        _append_live_assistant_part,
        _append_session_message,
        _append_session_workflow_state_context,
        _ask_user_options_from_action,
        _blueprint_module_kind,
        _blueprint_runner_for_agent,
        _bubbled_child_evidence_output_summary,
        _build_blueprint_dspy_module,
        _build_prompt_user_agent_module,
        _build_tool_user_agent_module,
        _cancelled_error_info,
        _coerce_ask_user_action,
        _coerce_expert_handoff_rows,
        _compile_session_conversation_history,
        _context_file_turn_provenance,
        _current_lm_model_id,
        _delegated_expert_agent_id,
        _delegated_expert_prompt,
        _dspy_images_from_parts,
        _dynamic_agent_runtime_provenance,
        _dynamic_parent_resume_prompt,
        _enrich_cancellation_error_info,
        _enrich_with_context_files,
        _enrich_with_requested_memory_search,
        _estimate_cost_usd,
        _expert_handoff_fields,
        _expert_handoff_summary,
        _extend_session_messages,
        _extract_tools_called,
        _failed_child_delegation_output_summary,
        _failed_child_delegation_workflow_state,
        _fallback_answer_from_delegation,
        _finalize_context_frame,
        _format_react_trajectory,
        _format_subagent_input,
        _gact_turn_timeout_s,
        _ground_fabricated_local_artifact_paths,
        _keyword_routed_user_agent,
        _keyword_user_agent_routing_enabled,
        _latest_delegation_output_summary,
        _latest_parent_resumed_output_summary,
        _merge_tool_call_rows,
        _merge_workflow_state_mapping,
        _pop_stream_fallback,
        _prediction_summary,
        _prediction_workflow_state,
        _propose_edit_diffs_from_pred,
        _reasoning_records_from_history_slice,
        _record_context_frame,
        _refresh_argonne_lm_token,
        _resolve_runtime_dynamic_agent,
        _run_dynamic_agent_compat,
        _runtime_active_agent_blueprint_agent_ids,
        _runtime_active_agent_blueprint_id,
        _runtime_active_agent_blueprint_root_id,
        _runtime_declared_child_ids,
        _session_agent_id,
        _should_execute_delegated_handoff,
        _snapshot_lm_history_index,
        _stream_fallback_payload,
        _StreamingOutputError,
        _tool_agent_empty_answer_fallback,
        _tool_calls_from_handoff_rows,
        _tool_result_preview,
        _try_streamed_forward_compat,
        _usage_from_dspy_history,
        _usage_from_history_slice,
        _user_agent_bool_param,
        _user_agent_int_param,
        _workflow_state_from_handoff_rows,
        _workflow_state_from_outputs,
    )

    bus: EventBus = app.state.bus
    sess = app.state.sessions.get(sid)
    if sess is None:
        # Session evaporated between POST + background start; can't
        # do anything useful. Don't raise — the publishing path
        # would crash and pollute logs with no client to notify.
        return

    error_info: Optional[ErrorInfo] = None
    answer_text = ""
    selected_agent = ""
    rationale = ""
    route_source = ""
    route_reason = ""
    auto_routed_agent: "AgentDef | None" = None
    agent_runtime: dict[str, Any] = {}
    dynamic_agent_used: "AgentDef | None" = None
    execution_path = ""
    # The expert/agent generating this turn's assistant parts. Set once the active
    # agent is resolved inside the forward try-block; pre-seeded so the final
    # part-assembly always has a value even when forward errors early.
    invocation_agent_id = ""
    tools_called: list[dict[str, Any]] = []
    expert_handoffs: list[dict[str, Any]] = []
    prompt_resolution: dict[str, Any] = {}
    proposed_diffs: list[Any] = []
    nanoagents: list[Any] = []
    thinking_text = ""
    retry_attempt_id = ""
    if isinstance(user_msg.metadata, dict):
        retry_attempt_id = str(user_msg.metadata.get("retry_attempt_id") or "")
    turn_id = user_msg.id
    trace_id = _semantic_trace_id(turn_id)
    # Bare sets, no reset: turn_id/trace_id must stay live for every later
    # copy_context() snapshot taken during this turn (mirrors the original
    # turn-scoped leak). app/session are established independently. (#714)
    _ctx.set_turn_id(turn_id)
    _ctx.set_trace_id(trace_id)
    native_images = _dspy_images_from_parts(user_msg.parts)
    turn_tokens: dict[str, int] = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    turn_cost = 0.0

    def _drain_observed_tool_calls(
        current_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge completed live-observer tool calls into turn metadata rows."""

        ledger = getattr(app.state, "tool_call_ledger", None)
        if ledger is None:
            return current_rows
        observed = ledger.pop(sid, [])
        if not observed:
            return current_rows
        return _merge_tool_call_rows(current_rows, observed)

    def _update_retry_attempt(
        status: str,
        *,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> None:
        if not retry_attempt_id:
            return
        attempt = app.state.turn_attempts.get(retry_attempt_id)
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
        app.state.turn_attempts[retry_attempt_id] = updated
        app.state.bus.publish(
            Event(
                type=f"turn.retry_{status}",
                session_id=sid,
                payload=updated.model_dump(exclude_none=True),
            )
        )

    if retry_attempt_id:
        _update_retry_attempt(
            "running",
            metadata_patch={"executed_user_message_id": user_msg.id},
        )
    _emit_semantic_event(
        app,
        sid,
        "turn.started",
        turn_id=turn_id,
        trace_id=trace_id,
        status="running",
        summary="User turn accepted and CLIO runtime started.",
        actor={"role": "user"},
        subject={"message_id": user_msg.id},
        payload={"text": user_text, "retry_attempt_id": retry_attempt_id},
    )

    # iowarp/clio-agent#5: prepend any attached context files to the
    # user's text so the agent's forward() sees them as primed input.
    # Plain text concat — keeps the agent.py interface untouched and
    # works regardless of which expert handles the turn.
    context_file_error: ErrorInfo | None = None
    context_file_provenance = _context_file_turn_provenance(app, sid, status="prepared")
    memory_search_metadata: dict[str, Any] = {}
    try:
        enriched_text = _enrich_with_context_files(app, sid, user_text)
        enriched_text, memory_search_metadata = _enrich_with_requested_memory_search(
            app,
            sid,
            enriched_text,
            user_msg,
        )
        # Carry prior turns of this session so a follow-up ("now plot it") can reuse
        # the region/stations/paths already resolved. No-op on the first turn.
        enriched_text = _compile_session_conversation_history(app, sid, enriched_text)
    except _ContextFileAccessError as exc:
        enriched_text = user_text
        context_file_error = exc.error_info
        context_file_provenance = _context_file_turn_provenance(app, sid, status="error")
    context_frame = _record_context_frame(
        app,
        sid,
        sess,
        user_msg,
        user_text=user_text,
        enriched_text=enriched_text,
        context_error=context_file_error,
    )
    if memory_search_metadata:
        _emit_semantic_event(
            app,
            sid,
            "memory.search.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary="Requested memory search was injected into turn context.",
            actor={"role": "runtime", "component": "memory"},
            subject={"message_id": user_msg.id},
            payload=memory_search_metadata,
        )
    # iowarp/clio-agent#20: pre_message hook can transform the
    # input or veto the turn. PermissionError → cancelled-style
    # error_info; the caller sees the hook's reason.
    if context_file_error is None:
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _emit_semantic_event(
                app,
                sid,
                "hook.invocation.started",
                turn_id=turn_id,
                trace_id=trace_id,
                status="running",
                summary="pre_message hook dispatch started.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={"input": enriched_text},
            )
            hook_scope = {
                "session_id": sid,
                "workspace_id": getattr(sess, "workspace_id", ""),
                "blueprint_id": _runtime_active_agent_blueprint_id(app, sid),
            }
            _fire_hook("pre_message", sid, enriched_text, hook_scope=hook_scope)
            _emit_semantic_event(
                app,
                sid,
                "hook.invocation.completed",
                turn_id=turn_id,
                trace_id=trace_id,
                summary="pre_message hook dispatch completed.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={},
            )
        except PermissionError as exc:
            _emit_semantic_event(
                app,
                sid,
                "hook.pre_message.blocked",
                turn_id=turn_id,
                trace_id=trace_id,
                status="blocked",
                summary="pre_message hook blocked the turn.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={"error": str(exc)},
            )
            _emit_semantic_event(
                app,
                sid,
                "turn.failed",
                turn_id=turn_id,
                trace_id=trace_id,
                status="blocked",
                summary="CLIO turn was blocked by pre_message hook.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={"error": str(exc)},
            )
            bus.publish(
                Event(
                    type="message.completed",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": user_msg.id,
                        "stop_reason": "blocked",
                        "error_info": {
                            "error": "permission_error",
                            "message": str(exc),
                            "recoverable": True,
                        },
                    },
                )
            )
            app.state.sessions.update(sid, status="error")
            _update_retry_attempt(
                "failed",
                metadata_patch={
                    "execution_error": "permission_error",
                    "executed_user_message_id": user_msg.id,
                },
            )
            bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=sid,
                    payload={
                        "session_id": sid,
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
    streamed_assistant_part_id: Optional[str] = None
    streamed_assistant_buffer: list[str] = []
    streamed_assistant_msg_id: Optional[str] = None
    # Per-expert live authorship (WS3): the live text stream is split into one part
    # per generating expert so a child's streamed output is authored to the child,
    # not frozen to "main". ``streamed_last_agent`` is the author of the currently
    # open part; ``streamed_part_text`` accumulates each part's own text so it can be
    # finalized on close; ``closed_streamed_part_ids`` marks parts already completed
    # live (so the finalize loop neither re-adds nor re-completes them).
    streamed_last_agent: Optional[str] = None
    streamed_live_part_ids: set[str] = set()
    streamed_part_text: dict[str, list[str]] = {}
    closed_streamed_part_ids: set[str] = set()

    def _live_parts_store() -> dict[str, list[Part]]:
        live_parts = getattr(app.state, "live_assistant_parts", None)
        if live_parts is None:
            live_parts = {}
            app.state.live_assistant_parts = live_parts
        return live_parts

    def _close_streamed_part(part_id: str) -> None:
        """Finalize an open live text part: persist its accumulated text onto the
        Part and emit ``message.part.completed`` so a client can replace the
        buffered deltas with the clean text. Idempotent per part."""
        if part_id in closed_streamed_part_ids:
            return
        closed_streamed_part_ids.add(part_id)
        buffered = "".join(streamed_part_text.get(part_id, []))
        for part in _live_parts_store().get(sid, []):
            if part.id == part_id and part.type == "text":
                part.text = buffered
                break
        bus.publish(
            Event(
                type="message.part.completed",
                session_id=sid,
                payload={
                    "turn_id": turn_id,
                    "message_id": streamed_assistant_msg_id,
                    "part_id": part_id,
                    "stream_source": "live",
                    "final_text": buffered,
                },
            )
        )

    async def _emit_chunk(text: str, agent_id: Optional[str] = None) -> None:
        nonlocal streamed_assistant_part_id, streamed_assistant_msg_id, streamed_last_agent
        # The generating expert (passed by the LM token tap from its react scope);
        # falls back to the turn's selected/invocation agent for the chat path.
        chunk_agent = agent_id or active_agent_id or invocation_agent_id or "main"
        if streamed_assistant_msg_id is None:
            # Lazily invent the message id the moment the first chunk arrives;
            # the final assistant message will reuse it.
            live_ids = getattr(app.state, "live_assistant_message_ids", {}) or {}
            streamed_assistant_msg_id = str(live_ids.get(sid) or "")
            created_live_msg = bool(streamed_assistant_msg_id)
            if not streamed_assistant_msg_id:
                streamed_assistant_msg_id = _new_message_id("asst")
                live_ids[sid] = streamed_assistant_msg_id
                app.state.live_assistant_message_ids = live_ids
            if not created_live_msg:
                bus.publish(
                    Event(
                        type="message.created",
                        session_id=sid,
                        payload=Message(
                            id=streamed_assistant_msg_id,
                            turn_id=turn_id,
                            session_id=sid,
                            role="assistant",
                            created_at=_iso_from_epoch(time.time()),
                            updated_at=_iso_from_epoch(time.time()),
                            parts=[],
                        ).to_wire(),
                    )
                )
        # Open a fresh part whenever none is open or the generating expert changed,
        # closing the prior one so each expert's streamed text is its own authored
        # part instead of being mashed into a single "main"-frozen part.
        if streamed_assistant_part_id is None or chunk_agent != streamed_last_agent:
            if streamed_assistant_part_id is not None:
                _close_streamed_part(streamed_assistant_part_id)
            streamed_assistant_part_id = _new_part_id()
            streamed_last_agent = chunk_agent
            streamed_live_part_ids.add(streamed_assistant_part_id)
            streamed_part_text[streamed_assistant_part_id] = []
            text_part = Part(
                id=streamed_assistant_part_id,
                type="text",
                agent_id=chunk_agent,
                text="",
                metadata={"stream_source": "live"},
            )
            _live_parts_store().setdefault(sid, []).append(text_part)
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": streamed_assistant_msg_id,
                        "stream_source": "live",
                        "part": text_part.to_wire(),
                    },
                )
            )
        streamed_assistant_buffer.append(text)
        streamed_part_text[streamed_assistant_part_id].append(text)
        bus.publish(
            Event(
                type="message.part.delta",
                session_id=sid,
                payload={
                    "turn_id": turn_id,
                    "message_id": streamed_assistant_msg_id,
                    "part_id": streamed_assistant_part_id,
                    "stream_source": "live",
                    "delta": {"text_append": text},
                },
            )
        )

    # Unified LM token highway (#693): bind this turn's loop + chat publisher so a
    # blueprint/expert LM call streamed in an executor thread feeds the SAME
    # _emit_chunk — one streaming path for chat AND blueprint turns, instead of
    # the old executor drain-and-discard. The executor inherits this binding via
    # the contextvars.copy_context() at the forward sites below.
    try:
        from clio_agent.runtime.lm_activity import set_live_chunk_emitter  # noqa: PLC0415

        set_live_chunk_emitter(asyncio.get_running_loop(), _emit_chunk)
    except Exception:  # noqa: BLE001 - live-stream wiring is best-effort
        pass

    # iowarp/clio-agent#8: snapshot LM history before the turn so we
    # can sum every call this turn made. ContextVars don't propagate
    # to asyncio executor threads (so dspy.settings.usage_tracker is
    # unreliable from worker threads), but ``lm.history`` IS shared
    # across threads — list.append under the GIL gives us a clean,
    # thread-safe ledger. We diff history[start:end] post-turn.
    history_start = _snapshot_lm_history_index(app)
    _pop_stream_fallback(app, sid)
    turn_cancel_event = threading.Event()
    app.state.cancel_events[sid] = turn_cancel_event
    if sid in app.state.cancel_flags:
        turn_cancel_event.set()
    # No-progress watchdog, not a hard wall: CLIO_GACT_TURN_TIMEOUT_S bounds the
    # gap BETWEEN observable progress events, never the total turn duration. A
    # long-but-progressing turn (a multi-phase EarthScope pipeline: filter ->
    # stage -> profile -> plot, each emitting bus events) must run to completion;
    # only a turn that goes silent for the whole window is wedged and aborted.
    # See [[clio-no-session-timeout]].
    turn_progress_timeout_s = _gact_turn_timeout_s(app)
    # Poll the progress heartbeat on a short cadence so abort latency after the
    # turn truly wedges stays small without busy-waiting. Cap by the window so a
    # tiny configured timeout still polls at least as often.
    _watchdog_poll_s = min(2.0, turn_progress_timeout_s) if turn_progress_timeout_s > 0 else 2.0

    def cancel_requested() -> bool:
        return turn_cancel_event.is_set()

    async def _await_turn_work(awaitable: Any) -> Any:
        if turn_progress_timeout_s <= 0:
            return await awaitable
        # Drive the work as a task and poll for completion. asyncio.wait (unlike
        # wait_for) does NOT cancel the task when the poll interval elapses, so a
        # still-running turn is never disturbed by the watchdog tick. We seed the
        # no-progress clock at "now" so a turn that publishes nothing at all is
        # still bounded by one window; every bus publish for this session (or a
        # global "" event) refreshes it via EventBus.last_publish_monotonic.
        bus = app.state.bus
        task = asyncio.ensure_future(awaitable)
        last_progress = time.monotonic()
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=_watchdog_poll_s)
                if done:
                    return task.result()
                heartbeat = max(
                    bus.last_publish_monotonic(sid),
                    bus.last_publish_monotonic(""),
                )
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
                if _lm_call_in_flight():
                    last_progress = time.monotonic()
                if time.monotonic() - last_progress >= turn_progress_timeout_s:
                    turn_cancel_event.set()
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - swallow during abort
                        pass
                    raise _TurnTimedOut(turn_progress_timeout_s) from None
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

    async def _run_dynamic_agent_sync(agent_def: "AgentDef", prompt: str) -> Any:
        runner = _blueprint_runner_for_agent(agent_def)
        loop = asyncio.get_running_loop()
        with _gact_app_context(app), _tool_session_context(sid):
            # The signature is rebuilt inside the executor (via _build_blueprint_dspy_module);
            # its routing Literal[children, "finish"] resolves children from the active
            # blueprint keyed on _ACTIVE_GACT_SESSION_ID. Set it here so the copied context
            # carries it -- otherwise children resolve empty and next_expert collapses to
            # Literal["finish"], forcing the agent to finish immediately.
            _sid_tok = _ctx.set_session_id(sid)
            try:
                turn_context = contextvars.copy_context()
            finally:
                _ctx.reset(_sid_tok)
        _pred = await _await_turn_work(
            loop.run_in_executor(
                None,
                lambda: turn_context.run(
                    _run_dynamic_agent_compat,
                    runner,
                    app.state.agent,
                    agent_def,
                    prompt,
                    sid,
                    cancel_requested,
                ),
            ),
        )
        # RAW-ROUTE instrumentation: what did THIS agent's LM actually emit as
        # structured expert_handoffs (before any continuation-contract injection)?
        # Distinguishes agent-driven routing (model emits handoffs) from
        # contract-driven routing (model emits none; the when_child_completed state
        # machine injects the next expert). Answers whether we can move to the
        # minimal agent-routed loop or whether the blueprint must teach routing.
        trace.route(
            "RAW-ROUTE",
            "agent=%s next_expert=%s next_task_len=%d answer_len=%d",
            getattr(agent_def, "id", "?"),
            str(getattr(_pred, "next_expert", "") or "") or "<none>",
            len(str(getattr(_pred, "next_task", "") or "")),
            len(str(getattr(_pred, "answer", "") or "")),
        )
        # Per-expert capture: one expert.response.completed per dynamic-agent run
        # (child or parent-resume), carrying that expert's full reasoning +
        # trajectory via _prediction_summary. Closes the nested-expert capture
        # gap -- each expert's own LM output is recorded under the turn's trace,
        # correlated by actor agent_id and the parent_expert in blueprint.
        _agent_meta = getattr(agent_def, "metadata", {}) or {}
        _emit_semantic_event(
            app,
            sid,
            "expert.response.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"Expert {getattr(agent_def, 'id', '?')} produced a response.",
            actor={"agent_id": str(getattr(agent_def, "id", "") or "")},
            blueprint={
                "agent_blueprint_id": str(_agent_meta.get("agent_blueprint_id") or ""),
                "parent_expert": str(getattr(agent_def, "parent_id", "") or ""),
            },
            provider={
                "provider_id": str(getattr(agent_def, "default_provider", "") or ""),
                "model_id": str(getattr(agent_def, "default_model", "") or ""),
            },
            payload=_prediction_summary(_pred),
        )
        # #733: remember each TERMINAL expert's answer so the FINALIZE pass can emit
        # it as an ordered transcript ``text`` part IFF the expert's answer did not
        # already stream live (WS3). The dedup must happen at finalize (after the
        # per-expert streamed text parts close), not here — the streamed part is
        # still open/empty at this point, so an emit-time check would miss it and a
        # streaming provider would get the answer twice. Skip delegating rounds (the
        # deliverable is the delegation, not an answer).
        _expert_id = str(getattr(agent_def, "id", "") or "")
        _answer_text = str(getattr(_pred, "answer", "") or "").strip()
        _next_expert = str(getattr(_pred, "next_expert", "") or "").strip()
        _is_terminal = _next_expert not in _runtime_declared_child_ids(
            app, _expert_id, session_id=sid
        )
        if _answer_text and _is_terminal:
            _ta = getattr(app.state, "expert_terminal_answers", None)
            if _ta is None:
                _ta = {}
                app.state.expert_terminal_answers = _ta
            _ta.setdefault(sid, []).append((_expert_id, _answer_text))
        # WS2: capture a TERMINAL CoT/predict expert's reasoning + answer in its own
        # ARC scope. Delegating rounds are written by the settle loop's
        # _arc_write_orchestrator_route (route per round); ReAct leaves self-write.
        # The remaining gap -- synthesis and every expert's `finish` round (an answer,
        # no delegation, no tool loop) -- is filled here, where `app` is reliably bound
        # and `_pred` carries the full reasoning + answer.
        if _agent_definition_uses_blueprint_runtime(agent_def):
            _kind = _blueprint_module_kind(agent_def)
            if _kind and _kind != "react":
                _next = str(getattr(_pred, "next_expert", "") or "").strip()
                _declared = _runtime_declared_child_ids(app, agent_def.id, session_id=sid)
                if _next not in _declared:  # terminal round (finish/empty/non-route)
                    _arc_write_terminal_expert(app, sid, agent_def.id, _pred, turn_id)
        return _pred

    async def _execute_delegated_experts(
        parent_agent: "AgentDef",
        rows: list[dict[str, Any]],
        *,
        source_text: str,
        completed_child_ids: set[str] | None = None,
        completed_child_outputs: dict[str, str] | None = None,
        depth: int = 0,
        seen: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        if seen is None:
            seen = {parent_agent.id}
        completed_child_ids = completed_child_ids or set()
        completed_child_outputs = completed_child_outputs or {}
        if depth >= 3:
            return [
                {
                    **row,
                    "status": "skipped",
                    "skip_reason": "max_delegate_depth_reached",
                    "parent_id": parent_agent.id,
                    "depth": depth,
                }
                for row in rows
                if _should_execute_delegated_handoff(row)
            ]

        executed: list[dict[str, Any]] = []
        for row in rows:
            if not _should_execute_delegated_handoff(row):
                executed.append(row)
                continue
            target_id = _delegated_expert_agent_id(row)
            if not target_id:
                executed.append(
                    {
                        **row,
                        "status": "skipped",
                        "skip_reason": "missing_delegate_target",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue
            target = _resolve_runtime_dynamic_agent(app, target_id, session_id=sid)
            if target is None or target.source != "expert_pack" or not target.enabled:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_not_available",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue
            if target.parent_id != parent_agent.id:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_parent_mismatch",
                        "parent_id": parent_agent.id,
                        "target_parent_id": target.parent_id,
                        "depth": depth,
                    }
                )
                continue
            if target.id in seen:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_cycle_detected",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue

            prompt = _append_session_workflow_state_context(
                app,
                sid,
                _delegated_expert_prompt(row, source_text),
            )
            target_kind = (
                _blueprint_module_kind(target)
                if _agent_definition_uses_blueprint_runtime(target)
                else ""
            )
            execution_mode = (
                f"blueprint_{target_kind}"
                if target_kind
                else ("tool_agent" if target.tools else "prompt_agent")
            )
            is_blueprint_delegation = bool(target_kind)
            delegation_event_prefix = (
                "blueprint.delegation" if is_blueprint_delegation else "delegation"
            )
            delegation_blueprint = {
                "pack_id": str(target.metadata.get("pack_id") or ""),
                "pack_version": str(target.metadata.get("pack_version") or ""),
                "agent_blueprint_id": str(target.metadata.get("agent_blueprint_id") or ""),
                "parent_expert": parent_agent.id,
                "child_expert": target.id,
            }
            started_at = time.perf_counter()
            started_row = {
                **row,
                "agent_id": target.id,
                "parent_id": parent_agent.id,
                "pack_id": str(target.metadata.get("pack_id") or ""),
                "pack_version": str(target.metadata.get("pack_version") or ""),
                "status": "running",
                "stage": "delegate.started",
                "delegation_lifecycle": "sync",
                "depth": depth,
                "execution_mode": execution_mode,
            }
            _emit_semantic_event(
                app,
                sid,
                f"{delegation_event_prefix}.started",
                turn_id=turn_id,
                trace_id=trace_id,
                status="running",
                summary=f"{parent_agent.id} delegated sync work to {target.id}.",
                actor={"agent_id": parent_agent.id, "role": "parent_expert"},
                subject={"agent_id": target.id, "role": "child_expert"},
                blueprint=delegation_blueprint,
                provider={
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                },
                payload=started_row,
            )
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                    type="expert_handoff",
                    agent_id=parent_agent.id,
                    parent_agent=parent_agent.id,
                    child_agent=target.id,
                    stage=str(started_row.get("stage") or ""),
                    status=str(started_row.get("status") or ""),
                    # The orchestrator's reasoning rides the delegation atom (#732).
                    thought=str(started_row.get("thought") or ""),
                    text=f"{parent_agent.id} -> {target.id}",
                    metadata={**started_row, "stream_source": "live"},
                ),
            )
            ledger_start = 0
            ledger = getattr(app.state, "tool_call_ledger", None)
            if isinstance(ledger, dict):
                session_rows = ledger.get(sid)
                if isinstance(session_rows, list):
                    ledger_start = len(session_rows)
            try:
                pred_child = await _run_dynamic_agent_sync(target, prompt)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                local_tools_called = _extract_tools_called(pred_child)
                # Seed local state from the child's typed workflow_state output
                # field (structural twin of the removed prose append), then merge
                # any tool-row state. The state rides the completed_row's
                # ``workflow_state`` Mapping below; it is never serialized into the
                # output text.
                local_workflow_state = _prediction_workflow_state(pred_child)
                for tool_row in local_tools_called:
                    row_state = tool_row.get("workflow_state")
                    if isinstance(row_state, Mapping):
                        _merge_workflow_state_mapping(local_workflow_state, row_state)
                nested: list[dict[str, Any]] = []
                if target.source == "expert_pack":
                    pred_child, nested = await _settle_dynamic_agent_delegations(
                        target,
                        pred_child,
                        source_text=prompt,
                    )
                raw_answer = str(getattr(pred_child, "answer", "") or "").strip()
                # The child's typed workflow_state is carried structurally on the
                # completed_row below (NOT serialized into output text).
                output = raw_answer
                if not nested:
                    child_rows = _coerce_expert_handoff_rows(
                        getattr(pred_child, "expert_handoffs", None)
                    )
                    nested = await _execute_delegated_experts(
                        target,
                        child_rows,
                        source_text=prompt,
                        completed_child_ids=completed_child_ids,
                        completed_child_outputs=completed_child_outputs,
                        depth=depth + 1,
                        seen={*seen, target.id},
                    )
                # STRUCTURAL empty-answer fallback (no prose-keyword scanning): a
                # tool-driven child can produce real tool evidence but an EMPTY prose
                # answer. ONLY when the answer is genuinely empty do we surface the
                # tool-trajectory evidence or the latest nested-child summary -- a
                # non-empty answer IS the agent's deliverable and is left untouched.
                if not raw_answer:
                    fallback = (
                        _tool_agent_empty_answer_fallback(getattr(pred_child, "trajectory", None))
                        or _latest_parent_resumed_output_summary(nested, target.id)
                        or _latest_delegation_output_summary(nested)
                    )
                    if fallback:
                        output = fallback
                if nested and (
                    _user_agent_bool_param(
                        target,
                        "bubble_child_evidence_on_completion",
                    )
                    or _user_agent_bool_param(
                        target,
                        "return_child_evidence_on_completion",
                    )
                ):
                    declared_target_child_ids = _runtime_declared_child_ids(
                        app,
                        target.id,
                        session_id=sid,
                    )
                    output = (
                        _bubbled_child_evidence_output_summary(
                            nested,
                            target.id,
                            declared_target_child_ids,
                        )
                        or output
                    )
                # Nested child typed state is merged into the structured
                # ``workflow_state`` carrier below (via _workflow_state_from_handoff_rows);
                # it is NOT appended to output text anymore.
                child_tools_called = _extract_tools_called(pred_child)
                if isinstance(ledger, dict):
                    session_rows = ledger.get(sid)
                    if isinstance(session_rows, list) and len(session_rows) > ledger_start:
                        child_tools_called = _merge_tool_call_rows(
                            child_tools_called,
                            [
                                dict(row)
                                for row in session_rows[ledger_start:]
                                if isinstance(row, Mapping)
                            ],
                        )
                # The prompt may carry prior accumulated typed state (injected via
                # _append_accumulated_workflow_state_context); parse it so prior
                # state still bubbles. The child's OWN typed state comes from its
                # structured workflow_state field via local_workflow_state (the
                # structural twin of the removed prose append) -- never re-parsed
                # out of `output` text, which no longer carries a state block.
                workflow_state = _workflow_state_from_outputs([prompt])
                # Seed from this expert's own authoritative typed emission so its
                # state bubbles to the parent for continuation routing, even when
                # `output` was reassigned to a child-evidence summary. Generic for
                # all packs.
                if local_workflow_state:
                    _merge_workflow_state_mapping(workflow_state, local_workflow_state)
                if nested:
                    _merge_workflow_state_mapping(
                        workflow_state,
                        _workflow_state_from_handoff_rows(nested),
                    )
                for tool_row in child_tools_called:
                    row_state = tool_row.get("workflow_state")
                    if isinstance(row_state, Mapping):
                        _merge_workflow_state_mapping(workflow_state, row_state)
                completed_row = {
                    **row,
                    "agent_id": target.id,
                    "parent_id": parent_agent.id,
                    "pack_id": str(target.metadata.get("pack_id") or ""),
                    "pack_version": str(target.metadata.get("pack_version") or ""),
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                    "fallback_warnings": list(target.validation_errors),
                    "status": "completed",
                    "stage": "delegate.completed",
                    "delegation_lifecycle": "sync",
                    "return_to": parent_agent.id,
                    "depth": depth,
                    "duration_ms": duration_ms,
                    "execution_mode": execution_mode,
                    "input": prompt,
                    "output": output,
                    "workflow_state": workflow_state,
                    "tools_called": child_tools_called,
                    "children": nested,
                }
                # completed_row carries the child's GENUINE output (the typed
                # dspy.extract deliverable) verbatim — no heuristic compaction.
                # It flows to the parent resume prompt + live Part; capture
                # (trace/ARC) carries the same full output.
                _emit_semantic_event(
                    app,
                    sid,
                    f"{delegation_event_prefix}.completed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"{target.id} returned a compact result to {parent_agent.id}.",
                    actor={"agent_id": target.id, "role": "child_expert"},
                    subject={"agent_id": parent_agent.id, "role": "parent_expert"},
                    blueprint=delegation_blueprint,
                    provider={
                        "provider_id": target.default_provider,
                        "model_id": target.default_model,
                    },
                    payload=dict(completed_row),
                )
                _append_live_assistant_part(
                    app,
                    sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        agent_id=parent_agent.id,
                        parent_agent=parent_agent.id,
                        child_agent=target.id,
                        stage=str(completed_row.get("stage") or ""),
                        status=str(completed_row.get("status") or ""),
                        text=f"{parent_agent.id} <- {target.id}",
                        metadata={**completed_row, "stream_source": "live"},
                    ),
                )
                executed.append(completed_row)
                resumed_row = {
                    "agent_id": parent_agent.id,
                    "parent_id": parent_agent.parent_id,
                    "dispatch_target": parent_agent.id,
                    "status": "completed",
                    "stage": "parent.resumed",
                    "delegation_lifecycle": "sync",
                    "resumed_from": target.id,
                    "depth": depth,
                    "output": output,
                    "workflow_state": workflow_state,
                }
                _emit_semantic_event(
                    app,
                    sid,
                    f"{delegation_event_prefix}.parent_resumed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"{parent_agent.id} resumed after {target.id}.",
                    actor={"agent_id": parent_agent.id, "role": "parent_expert"},
                    subject={"agent_id": target.id, "role": "child_expert"},
                    blueprint=delegation_blueprint,
                    payload=resumed_row,
                )
                _append_live_assistant_part(
                    app,
                    sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        agent_id=parent_agent.id,
                        parent_agent=str(parent_agent.parent_id or ""),
                        child_agent=parent_agent.id,
                        stage=str(resumed_row.get("stage") or ""),
                        status=str(resumed_row.get("status") or ""),
                        text=f"{parent_agent.id} resumed (from {target.id})",
                        metadata={**resumed_row, "stream_source": "live"},
                    ),
                )
                executed.append(resumed_row)
            except (_TurnCancelled, _TurnTimedOut):
                raise
            except Exception as exc:  # noqa: BLE001
                child_tools_called = []
                if isinstance(ledger, dict):
                    session_rows = ledger.get(sid)
                    if isinstance(session_rows, list) and len(session_rows) > ledger_start:
                        child_tools_called = _merge_tool_call_rows(
                            child_tools_called,
                            [
                                dict(row)
                                for row in session_rows[ledger_start:]
                                if isinstance(row, Mapping)
                            ],
                        )
                error_name = type(exc).__name__
                error_message = str(exc)
                workflow_state = _failed_child_delegation_workflow_state(
                    prompt=prompt,
                    child_agent_id=target.id,
                    parent_agent_id=parent_agent.id,
                    error=error_name,
                    message=error_message,
                    tools_called=child_tools_called,
                )
                output = _failed_child_delegation_output_summary(
                    child_agent_id=target.id,
                    parent_agent_id=parent_agent.id,
                    error=error_name,
                    message=error_message,
                )
                failed_row = {
                    **row,
                    "agent_id": target.id,
                    "parent_id": parent_agent.id,
                    "pack_id": str(target.metadata.get("pack_id") or ""),
                    "pack_version": str(target.metadata.get("pack_version") or ""),
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                    "fallback_warnings": list(target.validation_errors),
                    "status": "failed",
                    "stage": "delegate.failed",
                    "depth": depth,
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    "execution_mode": execution_mode,
                    "error": error_name,
                    "message": error_message,
                    "output": output,
                    "workflow_state": workflow_state,
                    "tools_called": child_tools_called,
                }
                _emit_semantic_event(
                    app,
                    sid,
                    f"{delegation_event_prefix}.failed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    status="failed",
                    summary=f"{target.id} failed during sync delegation.",
                    actor={"agent_id": target.id, "role": "child_expert"},
                    subject={"agent_id": parent_agent.id, "role": "parent_expert"},
                    blueprint=delegation_blueprint,
                    provider={
                        "provider_id": target.default_provider,
                        "model_id": target.default_model,
                    },
                    payload=failed_row,
                )
                _append_live_assistant_part(
                    app,
                    sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        agent_id=parent_agent.id,
                        parent_agent=parent_agent.id,
                        child_agent=target.id,
                        stage=str(failed_row.get("stage") or ""),
                        status=str(failed_row.get("status") or ""),
                        text=f"{parent_agent.id} -> {target.id} (failed)",
                        metadata={**failed_row, "stream_source": "live"},
                    ),
                )
                executed.append(failed_row)
        return executed

    async def _settle_dynamic_agent_delegations(
        parent_agent: "AgentDef",
        initial_pred: Any,
        *,
        source_text: str,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Run sync child delegations and re-enter the parent with compact returns."""

        latest_pred = initial_pred
        if trace.ROUTE_ON:
            import traceback as _tb_enter  # noqa: PLC0415

            _clio_frames = [
                ln.strip().split("\n")[0] for ln in _tb_enter.format_stack() if "gact/app.py" in ln
            ]
            trace.route(
                "SETTLE-ENTER",
                "parent=%s caller=%s",
                parent_agent.id,
                " <- ".join(reversed(_clio_frames[-6:])),
            )
        all_rows: list[dict[str, Any]] = []
        max_rounds = _user_agent_int_param(parent_agent, "max_sync_delegation_rounds", 12)
        max_rounds = max(1, min(max_rounds, 16))
        completed_child_ids: set[str] = set()
        completed_child_outputs: dict[str, str] = {}
        declared_child_ids = _runtime_declared_child_ids(app, parent_agent.id, session_id=sid)

        for _round in range(max_rounds):
            # AGENT-DRIVEN ROUTING. The parent emitted, at the end of its run, a typed
            # ``next_expert``: the ONE child to descend into, or "finish" to return to
            # ITS parent. No contracts, no prose heuristics -- the structured field IS
            # the routing decision (built in _blueprint_runtime_signature as
            # Literal[children + "finish"]). "finish"/missing/unknown-id => this expert
            # is done; finalize with its ``answer``. main's parent is the user, so main's
            # answer on "finish" is the deliverable.
            next_expert = str(getattr(latest_pred, "next_expert", "") or "").strip()
            next_task = str(getattr(latest_pred, "next_task", "") or "").strip()
            if trace.ROUTE_ON:
                trace.route(
                    "SETTLE",
                    "parent=%s round=%d completed=%s next_expert=%s next_task=%r reasoning=%r answer=%r",
                    parent_agent.id,
                    _round,
                    sorted(completed_child_ids),
                    next_expert or "<none>",
                    next_task[:160],
                    str(getattr(latest_pred, "reasoning", "") or "")[:400],
                    str(getattr(latest_pred, "answer", "") or "")[:200],
                )
            if (
                next_expert in ("", "finish", "none", "done", "stop")
                or next_expert not in declared_child_ids
            ):
                break
            # WS2: stream this orchestrator's routing decision (its reasoning + the
            # delegation) into its OWN ARC scope so ARC internally holds the complete
            # spine -- the ReAct *leaves* already write thought/tool_call/observation,
            # but the predict/CoT orchestrators (main/data/analysis/synthesis) wrote
            # nothing to their scope, so it was empty. Captured here on the MAIN loop
            # (where ``app.state.arc`` is reliably bound and ``latest_pred`` carries the
            # reasoning), not in the streamed executor forward (no app contextvar).
            _arc_write_orchestrator_route(
                app, sid, parent_agent.id, latest_pred, next_expert, next_task, turn_id
            )
            requested_rows = [
                {
                    "delegate_to": next_expert,
                    "agent_id": next_expert,
                    "question": next_task or source_text,
                    # The orchestrator's reasoning that produced THIS delegation —
                    # carried onto the delegate.started handoff part as its thought
                    # so a delegation turn renders as text + the delegation, one
                    # ordered event, just like a tool turn (#732).
                    "thought": str(getattr(latest_pred, "reasoning", "") or ""),
                    "status": "requested",
                    "execute": True,
                    "source": "agent_next_expert",
                }
            ]
            # Forward the parent's CURRENT accumulated state (the typed workflow_state it
            # is holding right now, e.g. station_catalog.station_ids produced by an
            # earlier sibling) as the child's parent-evidence. The static `source_text`
            # is the parent's ORIGINAL input, captured before earlier children ran -- so a
            # later child (e.g. the resolver) otherwise never sees the ranked list it is
            # documented to consume, and falls back to inventing candidate ids.
            # State travels STRUCTURALLY: read the parent's typed workflow_state field
            # and inject it via the clean structured prompt formatter, rather than
            # appending a prose state block to the parent's answer.
            current_evidence = str(getattr(latest_pred, "answer", "") or "").strip()
            parent_state = _prediction_workflow_state(latest_pred)
            if parent_state:
                current_evidence = _append_accumulated_workflow_state_context(
                    current_evidence, parent_state
                ).strip()
            executed_rows = await _execute_delegated_experts(
                parent_agent,
                requested_rows,
                source_text=current_evidence or source_text,
                completed_child_ids=completed_child_ids,
                completed_child_outputs=completed_child_outputs,
            )
            all_rows.extend(executed_rows)
            completed_this_round = [
                row
                for row in executed_rows
                if row.get("status") == "completed" and row.get("stage") == "delegate.completed"
            ]
            for row in completed_this_round:
                cid = str(row.get("agent_id") or row.get("delegate_to") or "").strip()
                if cid:
                    completed_child_ids.add(cid)
                    completed_child_outputs[cid] = str(
                        row.get("output") or row.get("output_summary") or row.get("summary") or ""
                    ).strip()
            if not completed_this_round:
                # Child could not run (unavailable / cycle / error). Stop instead of
                # looping; the parent's current answer carries whatever evidence exists.
                break
            # Re-invoke the parent with the child's returned evidence so IT emits the
            # next route (descend again, or finish).
            resume_prompt = _dynamic_parent_resume_prompt(
                source_text, parent_agent, all_rows, declared_child_ids=declared_child_ids
            )
            latest_pred = await _run_dynamic_agent_sync(parent_agent, resume_prompt)

        # The genuine final answer flows to the parent verbatim; the heuristic
        # evidence-scaffolding scrubber has been removed.
        return latest_pred, all_rows

    try:
        if context_file_error is not None:
            raise _ContextFileAccessError(context_file_error)

        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            raise _TurnCancelled(
                _cancelled_error_info(
                    sid,
                    execution_cancellation="turn_boundary",
                    executor_work_may_continue=False,
                )
            )

        session_agent_id = _session_agent_id(sess)
        active_agent_id = turn_agent_id or session_agent_id
        active_blueprint_root_id = _runtime_active_agent_blueprint_root_id(app, sid)
        active_blueprint_agent_ids = _runtime_active_agent_blueprint_agent_ids(app, sid)
        if (
            not turn_agent_id
            and active_blueprint_root_id
            and active_agent_id in {"", "main", "default"}
        ):
            active_agent_id = active_blueprint_root_id
        routing_mode = getattr(sess, "routing_mode", "auto") or "auto"
        auto_routed_agent = None
        if (
            _keyword_user_agent_routing_enabled()
            and not turn_agent_id
            and not active_blueprint_agent_ids
            and active_agent_id in {"", "main", "default"}
            and routing_mode in {"auto", "experts"}
        ):
            auto_routed_agent = _keyword_routed_user_agent(app, user_text)
            if auto_routed_agent is not None:
                active_agent_id = auto_routed_agent.id
        invocation_agent_id = active_agent_id or "orchestrator"
        _emit_semantic_event(
            app,
            sid,
            "agent.invocation.started",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary=f"Invoking {invocation_agent_id}.",
            actor={"agent_id": invocation_agent_id},
            subject={"message_id": user_msg.id},
            payload={
                "routing_mode": routing_mode,
                "session_agent_id": session_agent_id,
                "turn_agent_id": turn_agent_id,
                "active_blueprint_root_id": active_blueprint_root_id,
                "active_blueprint_agent_ids": active_blueprint_agent_ids,
            },
        )
        from clio_agent.agent import cancellation_checker as _cancellation_checker  # noqa: PLC0415

        _refresh_argonne_lm_token(app.state.agent)

        if (
            active_agent_id not in _EXECUTABLE_SESSION_AGENT_IDS
            or active_agent_id in active_blueprint_agent_ids
        ):
            prompt_registry_factory = getattr(app.state, "prompt_registry_for_request", None)
            prompt_registry = (
                prompt_registry_factory(session_id=sid)
                if callable(prompt_registry_factory)
                else None
            )
            dynamic_agent = _resolve_runtime_dynamic_agent(
                app,
                active_agent_id,
                session_id=sid,
                prompt_registry=prompt_registry,
            )
            if dynamic_agent is None:
                raise _UnsupportedSessionAgent(active_agent_id)
            prompt_resolution = dict(dynamic_agent.metadata.get("prompt_resolution") or {})
            dynamic_agent_used = dynamic_agent
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
            agent_runtime = _dynamic_agent_runtime_provenance(
                app,
                dynamic_agent,
                execution_mode=execution_mode,
            )
            with _gact_app_context(app):
                session_token = _ctx.set_session_id(sid)
                try:
                    module = (
                        _build_blueprint_dspy_module(app.state.agent, dynamic_agent)
                        if _agent_definition_uses_blueprint_runtime(dynamic_agent)
                        else (
                            _build_tool_user_agent_module(app.state.agent, dynamic_agent)
                            if dynamic_agent.tools
                            else _build_prompt_user_agent_module(app.state.agent, dynamic_agent)
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
                "message_id": user_msg.id,
            }
            _emit_semantic_event(
                app,
                sid,
                "llm.request.started",
                turn_id=turn_id,
                trace_id=trace_id,
                status="running",
                summary=f"LLM request started for {dynamic_agent.id}.",
                actor=llm_actor,
                subject=llm_subject,
                blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                provider=_llm_provider_payload(app, dynamic_agent.id),
                payload={
                    "request_mode": "streamed",
                    "input": enriched_text,
                    "prompt_resolution": prompt_resolution,
                    "agent_runtime": agent_runtime,
                    "native_image_count": len(native_images),
                },
            )
            with _cancellation_checker(cancel_requested), _tool_session_context(sid):
                pred = await _await_turn_work(
                    _try_streamed_forward_compat(
                        app,
                        enriched_text,
                        sid,
                        _emit_chunk,
                        session_mode=getattr(sess, "mode", "chat"),
                        session_edit_mode=getattr(sess, "edit_mode", "diff"),
                        agent_override=module,
                        images=native_images,
                        cancel_requested=cancel_requested,
                    )
                )
            if pred is not None:
                _emit_semantic_event(
                    app,
                    sid,
                    "llm.response.completed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"LLM response completed for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(app, dynamic_agent.id),
                    payload=_prediction_summary(pred),
                )
            if pred is None:
                _emit_semantic_event(
                    app,
                    sid,
                    "llm.request.started",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    status="running",
                    summary=f"Synchronous LLM request started for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(app, dynamic_agent.id),
                    payload={
                        "request_mode": "sync",
                        "input": enriched_text,
                        "prompt_resolution": prompt_resolution,
                        "agent_runtime": agent_runtime,
                        "native_image_count": len(native_images),
                    },
                )
                with _cancellation_checker(cancel_requested), _tool_session_context(sid):
                    loop = asyncio.get_running_loop()
                    turn_context = contextvars.copy_context()
                    pred = await _await_turn_work(
                        loop.run_in_executor(
                            None,
                            lambda: turn_context.run(
                                _run_dynamic_agent_compat,
                                runner,
                                app.state.agent,
                                dynamic_agent,
                                enriched_text,
                                sid,
                                cancel_requested,
                            ),
                        ),
                    )
                _emit_semantic_event(
                    app,
                    sid,
                    "llm.response.completed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"Synchronous LLM response completed for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(app, dynamic_agent.id),
                    payload=_prediction_summary(pred),
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
                with _tool_session_context(sid):
                    llm_actor = {
                        "agent_id": active_agent_id or "orchestrator",
                        "source": "builtin",
                        "execution_mode": "clio_agent_forward",
                    }
                    llm_subject = {"message_id": user_msg.id}
                    _emit_semantic_event(
                        app,
                        sid,
                        "llm.request.started",
                        turn_id=turn_id,
                        trace_id=trace_id,
                        status="running",
                        summary="LLM request started for CLIO orchestrator.",
                        actor=llm_actor,
                        subject=llm_subject,
                        provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                        payload={
                            "request_mode": "streamed",
                            "routing_mode": routing_override,
                            "session_mode": getattr(sess, "mode", "chat"),
                            "edit_mode": getattr(sess, "edit_mode", "diff"),
                            "input": enriched_text,
                            "native_image_count": len(native_images),
                        },
                    )
                    pred = await _await_turn_work(
                        _try_streamed_forward_compat(
                            app,
                            enriched_text,
                            sid,
                            _emit_chunk,
                            session_mode=getattr(sess, "mode", "chat"),
                            session_edit_mode=getattr(sess, "edit_mode", "diff"),
                            images=native_images,
                            cancel_requested=cancel_requested,
                        )
                    )
                    if pred is not None:
                        _emit_semantic_event(
                            app,
                            sid,
                            "llm.response.completed",
                            turn_id=turn_id,
                            trace_id=trace_id,
                            summary="LLM response completed for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                            payload=_prediction_summary(pred),
                        )
                    if pred is None:
                        _emit_semantic_event(
                            app,
                            sid,
                            "llm.request.started",
                            turn_id=turn_id,
                            trace_id=trace_id,
                            status="running",
                            summary="Synchronous LLM request started for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                            payload={
                                "request_mode": "sync",
                                "routing_mode": routing_override,
                                "session_mode": getattr(sess, "mode", "chat"),
                                "edit_mode": getattr(sess, "edit_mode", "diff"),
                                "input": enriched_text,
                                "native_image_count": len(native_images),
                            },
                        )
                        loop = asyncio.get_running_loop()
                        turn_context = contextvars.copy_context()
                        pred = await _await_turn_work(
                            loop.run_in_executor(
                                None,
                                lambda: turn_context.run(
                                    _agent_forward_compat,
                                    app.state.agent,
                                    enriched_text,
                                    sid,
                                    getattr(sess, "mode", "chat"),
                                    getattr(sess, "edit_mode", "diff"),
                                    cancel_requested,
                                    native_images,
                                ),
                            ),
                        )
                        _emit_semantic_event(
                            app,
                            sid,
                            "llm.response.completed",
                            turn_id=turn_id,
                            trace_id=trace_id,
                            summary="Synchronous LLM response completed for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                            payload=_prediction_summary(pred),
                        )
        if dynamic_agent_used is not None and dynamic_agent_used.source == "expert_pack":
            pred, expert_handoffs = await _settle_dynamic_agent_delegations(
                dynamic_agent_used,
                pred,
                source_text=enriched_text,
            )
        _emit_semantic_event(
            app,
            sid,
            "agent.invocation.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"{invocation_agent_id} returned a prediction.",
            actor={"agent_id": invocation_agent_id},
            subject={"message_id": user_msg.id},
            payload={
                "selected_expert": getattr(pred, "selected_expert", "") or "",
                "route_source": getattr(pred, "route_source", "") or "",
                "has_answer": bool(getattr(pred, "answer", "") or ""),
                "has_error_info": bool(getattr(pred, "error_info", None)),
            },
        )

        answer_text = getattr(pred, "answer", "")
        selected_agent = getattr(pred, "selected_expert", "") or ""
        rationale = getattr(pred, "routing_rationale", "")
        route_source = getattr(pred, "route_source", "") or ""
        route_reason = getattr(pred, "route_reason", "") or rationale
        if auto_routed_agent is not None:
            selected_agent = selected_agent or auto_routed_agent.id
            keyword_reason = f"Matched registered user agent {auto_routed_agent.id!r} by keyword."
            route_source = "user_agent_keyword"
            rationale = rationale or keyword_reason
            route_reason = keyword_reason
        pred_error_info = _coerce_error_info(getattr(pred, "error_info", None))
        if pred_error_info is not None:
            if pred_error_info.error == "cancelled":
                pred_error_info.details.setdefault("session_id", sid)
            error_info = pred_error_info
            if not error_info.details.get("partial", False):
                answer_text = ""
        ask_user_action = _coerce_ask_user_action(pred)
        if error_info is None and ask_user_action:
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
                session_id=sid,
                prompt=str(ask_user_action["question"]),
                status="pending",
                kind=kind,  # type: ignore[arg-type]
                options=options,
                created_at=now_iso,
                updated_at=now_iso,
                source="orchestrator_action",
                turn_id=user_msg.id,
                attempt_id=retry_attempt_id,
                metadata={
                    **dict(ask_user_action.get("metadata") or {}),
                    "reason": ask_user_action.get("reason", ""),
                    "caller": ask_user_action.get("caller", {}),
                    "resume_on_answer": True,
                    "source_user_message_id": user_msg.id,
                    "source_user_text": user_text,
                    "selected_agent": selected_agent,
                    "route_source": route_source,
                    "route_reason": route_reason,
                },
            )
            app.state.user_questions[question.id] = question
            _emit_semantic_event(
                app,
                sid,
                "user_question.created",
                turn_id=turn_id,
                trace_id=trace_id,
                status="waiting_user",
                summary="Agent requested user input before continuing.",
                actor={"agent_id": selected_agent or invocation_agent_id},
                subject={"question_id": question.id},
                payload=question.model_dump(exclude_none=True),
            )
            updated = app.state.sessions.update(
                sid,
                status="waiting_user",
                message_count=len(app.state.messages.get(sid, [])),
                metadata_patch={"pending_user_question_id": question.id},
            )
            _finalize_context_frame(
                app,
                sid,
                context_frame["id"],
                "",
                "completed",
                error_info=None,
            )
            bus.publish(
                Event(
                    type="user_question.created",
                    session_id=sid,
                    payload=question.model_dump(exclude_none=True),
                )
            )
            bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "status": "waiting_user",
                        "prev_status": "running",
                        "updated_at": updated.updated_at if updated is not None else "",
                        "pending_user_question_id": question.id,
                    },
                )
            )
            if retry_attempt_id:
                _update_retry_attempt(
                    "completed",
                    metadata_patch={
                        "ask_user_question_id": question.id,
                        "stop_reason": "waiting_user",
                    },
                )
            return
        # iowarp/clio-agent#25: data branch reports which execution
        # path it took ("fast" or "expert_loop"). Empty when not
        # populated by ClioAgent.forward (older code paths, non-data
        # branches not yet migrated).
        execution_path = getattr(pred, "execution_path", "") or ""
        tools_called = _extract_tools_called(pred)
        raw_handoffs = getattr(pred, "expert_handoffs", None) or []
        if not expert_handoffs:
            expert_handoffs = _coerce_expert_handoff_rows(raw_handoffs)
        tools_called = _merge_tool_call_rows(
            tools_called,
            _tool_calls_from_handoff_rows(expert_handoffs),
        )
        # Drain the per-session observer ledger so direct-tool short-
        # circuits (HDF5/Parquet/fs experts that bypass ReAct) still
        # report tools_called on the assistant message metadata.
        tools_called = _drain_observed_tool_calls(tools_called)
        # iowarp/clio-agent#17 — surface DSPy reasoning as a
        # `thinking` Part. ChainOfThought predictions expose
        # ``.reasoning`` (single string); ReAct exposes
        # ``.trajectory`` (step-by-step trace). Fall back to the
        # generic `_trace` Prediction wraps either of them in.
        thinking_text = (
            getattr(pred, "reasoning", "")
            or _format_react_trajectory(getattr(pred, "trajectory", None))
            or ""
        )
        # CLIO-BBBBBBBBBB24: cost + token rollup. Real DSPy
        # predictions don't always populate .tokens / .cost_usd
        # directly — pull from the per-turn UsageTracker first
        # (works across threads + streaming), then LM history.
        raw_tokens = getattr(pred, "tokens", None)
        if raw_tokens is not None:
            for key in turn_tokens:
                if isinstance(raw_tokens, dict):
                    v = raw_tokens.get(key, 0)
                else:
                    v = getattr(raw_tokens, key, 0)
                turn_tokens[key] = int(v or 0)
        else:
            # Diff the LM history slice for this turn first — captures
            # planner + expert + chat calls cleanly. Falls back to
            # ``last entry only`` for older code paths, then to a
            # character-based estimate when the upstream proxy
            # reports zero (some OpenAI-compatible proxies don't
            # populate usage on chunked replies).
            history_end = _snapshot_lm_history_index(app)
            history_made_calls = any(
                history_end.get(k, 0) > history_start.get(k, 0)
                for k in {*history_start.keys(), *history_end.keys()}
            )
            usage = _usage_from_history_slice(history_start, app)
            if not usage.get("output"):
                usage = _usage_from_dspy_history()
            for key in turn_tokens:
                turn_tokens[key] = int(usage.get(key, 0) or 0)
            turn_cost = float(usage.get("cost_usd", 0.0) or 0.0)
            # Char-based fallback only when the LM actually fired
            # this turn (history grew) but the upstream proxy
            # reported zero usage. Don't synthesize numbers when
            # there was no real call (e.g. unit tests with a fake
            # agent that bypasses dspy.LM entirely).
            if history_made_calls:
                if turn_tokens["output"] == 0 and answer_text:
                    turn_tokens["output"] = max(1, len(answer_text) // 4)
                if turn_tokens["input"] == 0 and enriched_text:
                    turn_tokens["input"] = max(1, len(enriched_text) // 4)
                if turn_cost == 0.0:
                    turn_cost = _estimate_cost_usd(
                        _current_lm_model_id(),
                        turn_tokens["input"],
                        turn_tokens["output"],
                    )
        if not turn_cost:
            turn_cost = float(getattr(pred, "cost_usd", 0.0) or 0.0)
        proposed_diffs = list(getattr(pred, "file_diffs", None) or [])
        if not proposed_diffs:
            # Dynamic tool agents call fs_propose_edit as a TOOL and never set
            # pred.file_diffs; promote those results so they materialize as
            # file_diff parts + pending /diffs rows (iowarp/clio-agent#674).
            proposed_diffs = _propose_edit_diffs_from_pred(pred)
        nanoagents = list(getattr(pred, "nanoagents_spawned", None) or [])
        for req in getattr(pred, "permissions_requested", None) or []:
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
                "session_id": sid,
                "tool_call": src.get("tool_call") or {},
                "summary": src.get("summary", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            }
            app.state.permissions[pid] = row
            _emit_semantic_event(
                app,
                sid,
                "permission.requested",
                turn_id=turn_id,
                trace_id=trace_id,
                status="pending",
                summary="Tool execution requested user permission.",
                actor={"agent_id": selected_agent or invocation_agent_id},
                subject={"permission_id": pid},
                payload=row,
            )
            bus.publish(
                Event(
                    type="permission.requested",
                    session_id=sid,
                    payload=row,
                )
            )
        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            error_info = _cancelled_error_info(
                sid,
                execution_cancellation="turn_boundary",
                executor_work_may_continue=False,
            )
            answer_text = ""
            tools_called = []
    except _TurnCancelled as exc:
        error_info = exc.error_info
        answer_text = ""
        tools_called = []
    except asyncio.CancelledError:
        error_info = _cancelled_error_info(
            sid,
            execution_cancellation="best_effort",
            executor_work_may_continue=True,
        )
        answer_text = ""
        tools_called = []
    except _StreamingOutputError as exc:
        original = exc.__cause__ or exc
        error_info = ErrorInfo(
            error="provider_error",
            message=str(exc),
            details={
                "original_error": type(original).__name__,
                "partial_output": bool(streamed_assistant_buffer),
                "stream_source": ("live" if streamed_assistant_buffer else "batch"),
            },
            recoverable=True,
        )
        answer_text = "".join(streamed_assistant_buffer)
        tools_called = []
    except _TurnTimedOut as exc:
        partial_output = bool(streamed_assistant_buffer)
        error_info = ErrorInfo(
            error="provider_timeout",
            message=f"agent turn made no progress for {exc.timeout_s:g}s",
            details={
                "session_id": sid,
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
        answer_text = "".join(streamed_assistant_buffer)
        tools_called = []
    except _UnsupportedSessionAgent as exc:
        selected_agent = exc.agent_id
        rationale = (
            "Session selected an agent that is registered but not executable "
            "by CLIO's current runtime."
        )
        error_info = ErrorInfo(
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
        answer_text = ""
        tools_called = []
    except _ContextFileAccessError as exc:
        error_info = exc.error_info
        answer_text = ""
        tools_called = []
    except Exception as exc:  # noqa: BLE001
        error_info = ErrorInfo(
            error="agent_error",
            message=f"agent.forward raised: {exc}",
            details={"original_error": type(exc).__name__},
            recoverable=True,
        )

    if error_info is None and not answer_text and expert_handoffs:
        answer_text = _fallback_answer_from_delegation(expert_handoffs)

    # Final user-facing text only: correct any fabricated local artifact (csv/png)
    # path the answer presents as produced — whether the synthesizing expert
    # composed a plausible-but-wrong filename or the delegation-fallback text
    # carried a model-requested ``output_path`` that the tool never wrote — by
    # grounding it against the run's verified on-disk artifacts in the merged
    # typed workflow_state. Generic (typed state + filesystem only), applied once
    # on the assembled answer, never on intermediate child rows.
    if answer_text and expert_handoffs:
        answer_text = _ground_fabricated_local_artifact_paths(
            answer_text,
            _workflow_state_from_handoff_rows(expert_handoffs),
        )

    # Build assistant parts — routing_decision (v0.2) first when we
    # got a selected_agent, then optional thinking trace, then the
    # text answer, then any file_diffs.
    if (
        error_info is None
        and not answer_text
        and not thinking_text
        and not proposed_diffs
        and not nanoagents
    ):
        error_info = ErrorInfo(
            error="empty_response",
            message="Agent completed without user-visible output.",
            details={
                "session_id": sid,
                "routing_mode": getattr(sess, "routing_mode", "auto"),
                "selected_agent": selected_agent,
            },
            recoverable=True,
        )

    live_parts_by_session = getattr(app.state, "live_assistant_parts", {}) or {}
    live_assistant_parts = list(live_parts_by_session.get(sid, []))
    # #731: the PERSISTED assistant message is the live spine IN ARRIVAL ORDER —
    # every part type (routing_decision / expert_handoff / tool_call / tool_result /
    # text), NOT a text-only subset regrouped by type. This single-sources the live
    # stream and the reloaded message (Principle 6) and retains the delegate.started
    # handoffs the old text-only rebuild silently dropped. The dedup sets below scan
    # ALL live parts so the finalize pass never re-adds a part that already streamed.
    live_routing_agents = {
        p.selected_agent
        for p in live_assistant_parts
        if p.type == "routing_decision" and p.selected_agent
    }
    live_has_expert_handoff = any(p.type == "expert_handoff" for p in live_assistant_parts)
    # Delegation atoms already streamed live as expert_handoff parts (delegate.started/
    # completed/parent_resumed). The finalize pass rebuilds the same handoffs from the
    # expert_handoffs rows for the PERSISTED message; suppress re-publishing those on
    # the bus when a live equivalent already went out, so the UI sees each delegation
    # lifecycle transition once (single-source, Principle 6). Keyed by the lifecycle
    # signature, not the (fresh) part id.
    live_handoff_sigs = {
        (p.stage, p.parent_agent, p.child_agent)
        for p in live_assistant_parts
        if p.type == "expert_handoff"
    }
    live_tool_calls = {
        p.call_id: p for p in live_assistant_parts if p.type == "tool_call" and p.call_id
    }
    enriched_live_part_ids: set[str] = set()
    for part in live_assistant_parts:
        if part.type != "tool_result" or not part.call_id:
            continue
        call_part = live_tool_calls.get(part.call_id)
        if call_part is None:
            continue
        for row in tools_called:
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
            enriched_live_part_ids.add(part.id)
            break

    assistant_parts: list[Part] = []
    if selected_agent and selected_agent not in live_routing_agents:
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="routing_decision",
                # The decision is MADE by the orchestrator; ``selected_agent`` is the
                # CHOSEN expert.
                agent_id=invocation_agent_id or "main",
                metadata={
                    k: v
                    for k, v in {
                        "route_source": route_source,
                        "route_reason": route_reason,
                    }.items()
                    if v
                },
                selected_agent=selected_agent,
                rationale=rationale,
                confidence=0.0,
                heuristic=False,
                execution_path=execution_path,
            )
        )
    for handoff in [] if live_has_expert_handoff else expert_handoffs:
        handoff_fields = _expert_handoff_fields(handoff)
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="expert_handoff",
                # The parent (decider) generates the handoff; structured fields are
                # the contract, ``text`` is a short label only.
                agent_id=handoff_fields["parent_agent"] or selected_agent or invocation_agent_id,
                parent_agent=handoff_fields["parent_agent"],
                child_agent=handoff_fields["child_agent"],
                stage=handoff_fields["stage"],
                status=handoff_fields["status"],
                metadata=handoff,
                text=_expert_handoff_summary(handoff),
            )
        )
    # The expert that produced this turn's thinking/answer/diff parts: the routed
    # expert when one was selected, else the active orchestrator.
    responder_agent_id = selected_agent or invocation_agent_id or "main"
    # Reconcile the per-expert live streamed parts (WS3) with the canonical answer.
    # Earlier experts' parts were already closed live (text persisted + completed).
    # The still-open part (the last streamer) becomes the canonical answer part when
    # its author is the responding expert; otherwise it's closed as-is and the
    # canonical answer lands as its own authored part below.
    reuse_streamed_part_id: Optional[str] = None
    if (
        streamed_assistant_part_id is not None
        and streamed_assistant_part_id not in closed_streamed_part_ids
    ):
        if answer_text and streamed_last_agent == responder_agent_id:
            reuse_streamed_part_id = streamed_assistant_part_id
        else:
            _close_streamed_part(streamed_assistant_part_id)
    assistant_parts.extend(live_assistant_parts)
    # #733 fallback: a TERMINAL expert whose answer did NOT stream live (a
    # non-streaming provider / blocking path leaves its WS3 text part empty) gets its
    # answer emitted as an authored text part now. When WS3 DID stream the answer the
    # expert already has a non-empty text part (closed by this point), so this adds
    # nothing — no duplicate. Author = the expert; this is its deliverable.
    answered_agents = {
        p.agent_id for p in assistant_parts if p.type == "text" and (p.text or "").strip()
    }
    for fb_expert, fb_answer in (getattr(app.state, "expert_terminal_answers", {}) or {}).get(
        sid, []
    ):
        if fb_expert in answered_agents:
            continue
        answered_agents.add(fb_expert)
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="text",
                agent_id=fb_expert,
                text=fb_answer,
                metadata={"stream_source": "expert_answer"},
            )
        )
    if thinking_text:
        # iowarp/clio-agent#17: surface DSPy reasoning as a thinking Part so the TUI
        # can collapse + render it (gated on capabilities.thinking_blocks). Appended
        # AFTER the live spine — the responder's wrap-up reasoning is produced at the
        # END of the turn and published at finalize, so its persisted ``sequence``
        # must follow the spine, not be hoisted above it (#731: no reorder on reload).
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="thinking",
                agent_id=responder_agent_id,
                text=thinking_text,
            )
        )
    # The canonical turn answer is the last terminal expert's deliverable; only append
    # a main-authored copy when it is NOT already present as an authored text part
    # (the WS3 stream or the #733 fallback above) — otherwise it duplicates.
    answer_already_present = bool(answer_text) and any(
        p.type == "text" and (p.text or "").strip() == answer_text.strip() for p in assistant_parts
    )
    if answer_text and reuse_streamed_part_id is None and not answer_already_present:
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="text",
                agent_id=responder_agent_id,
                text=answer_text,
            )
        )
    for row in proposed_diffs:
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
        assistant_parts.append(diff_part)
        _emit_semantic_event(
            app,
            sid,
            "artifact.proposed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"Agent proposed a file diff for {path}.",
            actor={"agent_id": selected_agent or invocation_agent_id},
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

    error_info = _enrich_cancellation_error_info(app, sid, error_info)
    cancelled_turn = error_info is not None and error_info.error == "cancelled"
    if cancelled_turn:
        app.state.cancel_flags.discard(sid)
        ledger = getattr(app.state, "tool_call_ledger", None)
        if ledger is not None:
            ledger.pop(sid, None)

    assistant_metadata: dict[str, Any] = {}
    if turn_agent_id:
        assistant_metadata["agent_override"] = {
            "requested_agent_id": turn_agent_id,
            "session_agent_id": _session_agent_id(sess),
            "effective_agent_id": selected_agent or turn_agent_id,
            "scope": "turn",
        }
    should_report_stream_provenance = bool(answer_text) or error_info is not None
    text_stream_source = (
        ("live" if streamed_assistant_part_id is not None else "batch")
        if should_report_stream_provenance
        else ""
    )
    if text_stream_source:
        assistant_metadata["stream_source"] = text_stream_source
    stream_fallback = _pop_stream_fallback(app, sid)
    if text_stream_source == "batch":
        if not stream_fallback:
            stream_fallback = _stream_fallback_payload("sync_execution_path")
        assistant_metadata["stream_fallback"] = stream_fallback
    if text_stream_source:
        for part in assistant_parts:
            if part.type != "text" or not part.text:
                continue
            part.metadata = {
                **part.metadata,
                "stream_source": text_stream_source,
            }
            if text_stream_source == "batch" and stream_fallback:
                part.metadata["stream_fallback"] = stream_fallback
    # A live observer completion can arrive after the immediate post-forward drain
    # but before the assistant message is persisted. Reconcile once more at the
    # final metadata boundary so reloads retain the same tool facts as the live bus.
    if not cancelled_turn:
        tools_called = _drain_observed_tool_calls(tools_called)
    if tools_called:
        assistant_metadata["tools_called"] = tools_called
    if expert_handoffs:
        assistant_metadata["expert_handoffs"] = expert_handoffs
    if context_file_provenance["files"]:
        assistant_metadata["context_files"] = context_file_provenance
    if memory_search_metadata:
        assistant_metadata["memory_search"] = memory_search_metadata
    if agent_runtime:
        assistant_metadata["agent_runtime"] = agent_runtime
    if prompt_resolution:
        assistant_metadata["prompt_resolution"] = prompt_resolution
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
            _reasoning_log = _reasoning_records_from_history_slice(history_start, app)
        except Exception:  # noqa: BLE001 - reasoning capture is best-effort, never fail a turn
            _reasoning_log = []
        if _reasoning_log:
            assistant_metadata["reasoning_log"] = _reasoning_log
            trace.event(
                "REASONING",
                "captured %d call(s): %s",
                len(_reasoning_log),
                "; ".join(
                    f"{(r['model'] or '?').split('/')[-1]}={r['reasoning_chars']}c"
                    for r in _reasoning_log
                ),
            )
    # iowarp/clio-agent#6: when streaming actually emitted chunks,
    # reuse its message_id + part_id so the deltas + final
    # message line up. Otherwise mint a fresh id (existing path).
    live_ids = getattr(app.state, "live_assistant_message_ids", {}) or {}
    live_assistant_msg_id = str(live_ids.get(sid) or "")
    asst_id = streamed_assistant_msg_id or live_assistant_msg_id or _new_message_id("asst")
    if reuse_streamed_part_id is not None and answer_text:
        # Replace the reused (still-open, responder-authored) live part with a stub
        # carrying its streamed part_id + the canonical answer, so the final message
        # references the same id the deltas used. Match by id (not "first text part")
        # so per-expert child text parts streamed earlier are left untouched.
        for i, p in enumerate(assistant_parts):
            if p.type == "text" and p.id == reuse_streamed_part_id:
                assistant_parts[i] = Part(
                    id=reuse_streamed_part_id,
                    type="text",
                    agent_id=p.agent_id or responder_agent_id,
                    text=answer_text,
                    metadata=p.metadata,
                )
                break
    # #736: drop the resumed orchestrator's verbatim echo of a terminal child's answer
    # from the authored (persisted/reloaded) transcript, keeping the child-authored
    # original. Serving-layer dedup — the live stream still carries main's echo (it
    # genuinely streamed) and the client dedups it defensively. See helper docstring.
    assistant_parts = _dedup_cross_agent_text(assistant_parts)
    # #731: stamp a monotonic 1-based arrival-order key on every persisted part so a
    # reloaded conversation can be restored to the exact order it streamed even if a
    # client re-sorts. The list is already in arrival order; ``sequence`` makes the
    # ordering explicit and survives the slim ``to_wire`` projection.
    for _seq, _part in enumerate(assistant_parts, start=1):
        _part.sequence = _seq
    assistant_msg = Message(
        id=asst_id,
        # Correlate the assistant reply to the user-turn that produced it (#711).
        turn_id=turn_id,
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(time.time()),
        updated_at=_iso_from_epoch(time.time()),
        parts=assistant_parts,
        tokens=Tokens(**turn_tokens),
        cost_usd=turn_cost,
        stop_reason="cancelled" if cancelled_turn else ("error" if error_info else "end_turn"),
        error_info=error_info,
        metadata=assistant_metadata,
    )
    _finalize_context_frame(
        app,
        sid,
        context_frame["id"],
        assistant_msg.id,
        "cancelled" if cancelled_turn else ("error" if error_info else "completed"),
        error_info=error_info,
    )

    # Index file_diff parts so /diffs/apply + /diffs/reject find them.
    bucket = app.state.pending_diffs.setdefault(sid, [])
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

    # Materialise nanoagent spawns + publish their lifecycle events.
    for spawn in nanoagents:
        get = (
            spawn.get
            if isinstance(spawn, dict)
            else (lambda k, default=None, _s=spawn: getattr(_s, k, default))
        )
        agent_id = get("agent_id") or get("agent") or "nanoagent"
        spawn_input = get("input") or {}
        answer = get("answer") or ""
        tools_called = get("tools_called") or get("tools") or []
        subsess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=f"{agent_id} subagent",
            parent_session_id=sid,
            agent={"id": str(agent_id), "mode": "subagent"},
            metadata={
                "session_type": "nanoagent",
                "agent_id": str(agent_id),
                "parent_session_id": sid,
                "spawned_by_message_id": assistant_msg.id,
                "spawned_by_agent": selected_agent,
                "tool_count": len(tools_called) if isinstance(tools_called, list) else 0,
            },
        )
        sub_now = time.time()
        sub_user = Message(
            id=_new_message_id("user"),
            session_id=subsess.id,
            role="user",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[
                Part(
                    id=_new_part_id(),
                    type="text",
                    text=_format_subagent_input(spawn_input),
                )
            ],
            metadata={
                "subagent_input": spawn_input,
                "parent_session_id": sid,
                "spawned_by_message_id": assistant_msg.id,
            },
        )
        sub_asst = Message(
            id=_new_message_id("asst"),
            session_id=subsess.id,
            role="assistant",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=(
                [Part(id=_new_part_id(), type="text", agent_id=str(agent_id), text=answer)]
                if answer
                else []
            ),
            stop_reason="end_turn",
            metadata={"tools_called": tools_called} if tools_called else {},
        )
        _extend_session_messages(app, subsess.id, [sub_user, sub_asst])
        app.state.sessions.update(subsess.id, message_count=2, status="idle")
        _emit_semantic_event(
            app,
            sid,
            "subagent.started",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary=f"Spawned subagent {agent_id}.",
            actor={"agent_id": selected_agent or "orchestrator"},
            subject={"agent_id": str(agent_id), "session_id": subsess.id},
            payload={
                "parent_session_id": sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "spawned_by_message_id": assistant_msg.id,
            },
        )
        bus.publish(
            Event(
                type="subagent.started",
                session_id=sid,
                payload={
                    "parent_session_id": sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "spawned_by_message_id": assistant_msg.id,
                },
            )
        )
        _emit_semantic_event(
            app,
            sid,
            "subagent.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"Subagent {agent_id} completed.",
            actor={"agent_id": str(agent_id), "session_id": subsess.id},
            subject={"session_id": sid},
            payload={
                "parent_session_id": sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                "tokens": get("tokens") or {},
                "cost_usd": float(get("cost_usd", 0.0) or 0.0),
            },
        )
        bus.publish(
            Event(
                type="subagent.completed",
                session_id=sid,
                payload={
                    "parent_session_id": sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                    "tokens": get("tokens") or {},
                    "cost_usd": float(get("cost_usd", 0.0) or 0.0),
                },
            )
        )

    # message.created for the assistant message (empty body — parts
    # arrive via subsequent message.part.added/delta events).
    # When real streaming already fired the message.created +
    # message.part.added + N deltas (#6), skip re-issuing them so we
    # don't duplicate.
    if streamed_assistant_msg_id is None and not live_assistant_msg_id:
        bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=Message(
                    id=assistant_msg.id,
                    turn_id=turn_id,
                    session_id=sid,
                    role="assistant",
                    created_at=assistant_msg.created_at,
                    updated_at=assistant_msg.updated_at,
                    parts=[],
                ).to_wire(),
            )
        )
    # Stream live text parts via message.part.delta. When a turn only has
    # post-hoc text, publish the completed text as a normal part instead
    # of chunking it into synthetic deltas that could be mistaken for live
    # provider tokens.
    for part in assistant_parts:
        if (
            part.metadata.get("stream_source") == "live"
            and part.id not in enriched_live_part_ids
            and part.type != "text"
        ):
            continue
        # Per-expert child text parts were already streamed AND completed live
        # (WS3); skip them here so they are neither re-added nor re-completed.
        if part.type == "text" and part.id in closed_streamed_part_ids:
            continue
        # Delegation handoffs already streamed live: keep them in the persisted
        # message but don't re-publish on the bus (single-source, Principle 6).
        if (
            part.type == "expert_handoff"
            and (part.stage, part.parent_agent, part.child_agent) in live_handoff_sigs
        ):
            continue
        if part.type == "text" and part.text:
            if part.id == reuse_streamed_part_id:
                # Real streaming already pumped deltas — but those
                # carry raw LM output that includes ChatAdapter format
                # markers ([[ ## answer ## ]] etc). The final ``part.text``
                # is the parsed clean answer; ship it on the completed
                # event so the TUI can replace the buffered text.
                bus.publish(
                    Event(
                        type="message.part.completed",
                        session_id=sid,
                        payload={
                            "turn_id": turn_id,
                            "message_id": assistant_msg.id,
                            "part_id": part.id,
                            "stream_source": "live",
                            "final_text": part.text,
                        },
                    )
                )
                continue
            delivered = part.model_copy(deep=True)
            delivered.metadata = {
                **delivered.metadata,
                "stream_source": "batch",
            }
            if stream_fallback:
                delivered.metadata["stream_fallback"] = stream_fallback
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": assistant_msg.id,
                        "stream_source": "batch",
                        "part": delivered.to_wire(),
                    },
                )
            )
            bus.publish(
                Event(
                    type="message.part.completed",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": assistant_msg.id,
                        "part_id": part.id,
                        "stream_source": "batch",
                        "stream_fallback": stream_fallback,
                        "final_text": part.text,
                    },
                )
            )
        else:
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": assistant_msg.id,
                        "stream_source": str(part.metadata.get("stream_source") or "batch"),
                        "part": part.to_wire(),
                    },
                )
            )
    # Tool lifecycle events are only emitted by the live observer at the
    # execution boundary. Prediction.tools_called remains summary metadata;
    # do not reconstruct started/completed events after the turn, because
    # that makes post-hoc facts look like live tool timing.
    completed_payload: dict[str, Any] = {
        "turn_id": turn_id,
        "message_id": assistant_msg.id,
        "stop_reason": "cancelled" if cancelled_turn else ("error" if error_info else "end_turn"),
        "tokens": dict(turn_tokens),
        "cost_usd": turn_cost,
    }
    if error_info is not None:
        completed_payload["error_info"] = error_info.model_dump(exclude_none=True)
    if assistant_metadata:
        completed_payload["metadata"] = assistant_metadata
    # Embed the full final assistant message in the DURABLE turn.completed so the
    # messages store is derivable from the canonical trace (the trace is the
    # source of truth). final_message is in SENSITIVE_KEYS, so the SSE projection
    # strips it -- the message already streams to clients via message.* events.
    semantic_completed_payload = {
        **completed_payload,
        "final_message": assistant_msg.model_dump(exclude_none=True),
    }
    _emit_semantic_event(
        app,
        sid,
        "turn.completed" if error_info is None else "turn.failed",
        turn_id=turn_id,
        trace_id=trace_id,
        status="completed" if error_info is None else "failed",
        summary=(
            "CLIO turn completed."
            if error_info is None
            else f"CLIO turn failed: {error_info.error}."
        ),
        actor={"agent_id": selected_agent or "orchestrator"},
        subject={"message_id": assistant_msg.id},
        payload=semantic_completed_payload,
    )
    bus.publish(
        Event(
            type="message.completed",
            session_id=sid,
            payload=completed_payload,
        )
    )

    # Persist + settle.
    final_status = "cancelled" if cancelled_turn else ("error" if error_info else "idle")
    retry_status = "cancelled" if cancelled_turn else ("failed" if error_info else "completed")
    _append_session_message(app, sid, assistant_msg)
    getattr(app.state, "live_assistant_message_ids", {}).pop(sid, None)
    getattr(app.state, "live_assistant_parts", {}).pop(sid, None)
    getattr(app.state, "live_assistant_part_keys", {}).pop(sid, None)
    getattr(app.state, "expert_terminal_answers", {}).pop(sid, None)
    _update_retry_attempt(
        retry_status,
        metadata_patch={
            "executed_user_message_id": user_msg.id,
            "assistant_message_id": assistant_msg.id,
            "stop_reason": completed_payload["stop_reason"],
        },
    )
    app.state.sessions.update(
        sid,
        status=final_status,
        message_count=sess.message_count + 2,
        add_tokens_input=turn_tokens["input"],
        add_tokens_output=turn_tokens["output"],
        add_cost_usd=turn_cost,
    )
    cancellation_status: dict[str, Any] = {}
    if cancelled_turn and error_info is not None:
        cancellation_status = {
            "execution_cancellation": error_info.details.get("execution_cancellation"),
            "executor_work_may_continue": error_info.details.get("executor_work_may_continue"),
            "cancellation_attempt": error_info.details.get("cancellation_attempt", {}),
        }
    bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
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
            app,
            sid,
            "hook.invocation.started",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary="post_message hook dispatch started.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={"assistant": assistant_msg.model_dump(exclude_none=True)},
        )
        _fire_hook(
            "post_message",
            sid,
            assistant_msg.model_dump(exclude_none=True),
            hook_scope={
                "session_id": sid,
                "workspace_id": getattr(sess, "workspace_id", ""),
                "blueprint_id": _runtime_active_agent_blueprint_id(app, sid),
            },
        )
        _emit_semantic_event(
            app,
            sid,
            "hook.invocation.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary="post_message hook dispatch completed.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={},
        )
    except Exception:  # noqa: BLE001
        _emit_semantic_event(
            app,
            sid,
            "hook.invocation.failed",
            turn_id=turn_id,
            trace_id=trace_id,
            status="failed",
            summary="post_message hook dispatch failed and was swallowed by policy.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={},
        )
        pass
    if not (
        cancelled_turn
        and error_info is not None
        and error_info.details.get("execution_cancellation") == "best_effort"
    ):
        if app.state.cancel_events.get(sid) is turn_cancel_event:
            app.state.cancel_events.pop(sid, None)


def _arc_write_terminal_expert(
    app: "FastAPI",
    sid: str,
    scope: str,
    pred: Any,
    turn_id: str,
) -> None:
    """WS2: append a TERMINAL CoT/predict expert's reasoning (``thought``) + final
    answer (``answer`` kind) to its OWN ARC scope.

    ReAct leaves already stream their own thought/tool_call/observation, and a
    *delegating* orchestrator's per-round reasoning + route is written by
    :func:`_arc_write_orchestrator_route` from the settle loop. The remaining gap is
    the expert that produces an answer WITHOUT delegating and WITHOUT a tool loop --
    the synthesis stage, and every orchestrator's own ``finish`` round. Without this,
    those scopes stay empty in ARC even though the answer reached the wire (Principle
    1: ARC must hold the complete trajectory). Best-effort; never breaks a turn."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is None or not sid or not scope:
        return
    reasoning = str(getattr(pred, "reasoning", "") or "").strip()
    answer = str(getattr(pred, "answer", "") or "").strip()
    if not reasoning and not answer:
        return
    expert_span_id = f"{turn_id}:{scope}" if turn_id else scope
    try:
        step = 0
        if reasoning:
            arc.append_segment(
                sid,
                scope,
                "thought",
                {"text": reasoning},
                step=step,
                token_count=max(1, len(reasoning) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
            step += 1
        if answer:
            # The deliverable rides the dedicated ``answer`` kind (an expert/turn final
            # message) -- substrate-complete but outside the working-set/prompt render,
            # so it never re-enters a downstream prompt.
            arc.append_segment(
                sid,
                scope,
                "answer",
                {"text": answer},
                step=step,
                token_count=max(1, len(answer) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
    except Exception:  # noqa: BLE001 - ARC capture is best-effort, never break a turn
        if trace.HF_ON:
            trace.hot("ARC-TERMINAL-WRITE-FAIL", "%s", scope)


def _arc_write_orchestrator_route(
    app: "FastAPI",
    sid: str,
    scope: str,
    pred: Any,
    next_expert: str,
    next_task: str,
    turn_id: str,
) -> None:
    """WS2: append an orchestrator's reasoning (thought) + delegation (tool_call) to
    its OWN ARC scope, so a predict/CoT orchestrator's working-set trajectory is no
    longer empty (the ReAct leaves already cover themselves). Best-effort; never
    breaks a turn."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is None or not sid or not scope:
        return
    expert_span_id = f"{turn_id}:{scope}" if turn_id else scope
    try:
        import json as _json  # noqa: PLC0415

        step = 0
        reasoning = str(getattr(pred, "reasoning", "") or "").strip()
        if reasoning:
            arc.append_segment(
                sid,
                scope,
                "thought",
                {"text": reasoning},
                step=step,
                token_count=max(1, len(reasoning) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
            step += 1
        # A delegation IS a call to a child expert (the ReAct path already models
        # children as tools), so it rides the ``tool_call`` kind.
        delegation = {"name": next_expert, "args": {"task": next_task}}
        arc.append_segment(
            sid,
            scope,
            "tool_call",
            delegation,
            step=step,
            token_count=max(1, len(_json.dumps(delegation, default=str)) // 4),
            turn_id=turn_id,
            expert_span_id=expert_span_id,
        )
    except Exception:  # noqa: BLE001 - ARC capture is best-effort, never break a turn
        if trace.HF_ON:
            trace.hot("ARC-ROUTE-WRITE-FAIL", "%s", scope)


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
    from clio_agent.gact.app import (  # noqa: PLC0415
        _agent_accepts_images,
        _append_session_message,
        _image_part_summaries,
        _session_agent_id,
        _user_message_parts,
    )

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
