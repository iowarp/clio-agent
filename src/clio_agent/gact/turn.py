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
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact._params import (
    _gact_turn_timeout_s,
    _user_agent_bool_param,
    _user_agent_int_param,
)
from clio_agent.gact.agents.resolution import (
    _agent_definition_uses_blueprint_runtime,
    _resolve_runtime_dynamic_agent,
    _runtime_active_agent_blueprint_agent_ids,
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_root_id,
    _runtime_declared_child_ids,
)
from clio_agent.gact.delegation import (
    _append_accumulated_workflow_state_context,
    _append_session_workflow_state_context,
    _bubbled_child_evidence_output_summary,
    _clean_public_transcript_text,
    _coerce_expert_handoff_rows,
    _delegated_expert_agent_id,
    _delegated_expert_prompt,
    _dynamic_parent_resume_prompt,
    _failed_child_delegation_output_summary,
    _failed_child_delegation_workflow_state,
    _fallback_answer_from_delegation,
    _latest_delegation_output_summary,
    _latest_parent_resumed_output_summary,
    _prediction_workflow_state,
    _should_execute_delegated_handoff,
    _workflow_state_from_handoff_rows,
    _workflow_state_from_outputs,
)
from clio_agent.gact.enrichment import (
    _context_file_turn_provenance,
    _enrich_with_context_files,
    _enrich_with_requested_memory_search,
    _finalize_context_frame,
    _record_context_frame,
)
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.evidence import (
    _dynamic_agent_runtime_provenance,
    _ground_fabricated_local_artifact_paths,
    _propose_edit_diffs_from_pred,
    _tool_agent_empty_answer_fallback,
    _tool_result_preview,
)
from clio_agent.gact.messaging import (
    _agent_accepts_images,
    _ask_user_options_from_action,
    _coerce_ask_user_action,
    _format_subagent_input,
    _image_part_summaries,
    _prediction_summary,
    _user_message_parts,
)
from clio_agent.gact.providers.auth import _refresh_argonne_lm_token
from clio_agent.gact.providers.config import _current_lm_model_id
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
    _extend_session_messages,
)
from clio_agent.gact.streaming import (
    _agent_forward_compat,
    _clear_live_streamed_field_text,
    _extract_tools_called,
    _format_react_trajectory,
    _pop_stream_fallback,
    _record_live_streamed_field_text,
    _run_dynamic_agent_compat,
    _stream_fallback_payload,
    _StreamingOutputError,
    _try_streamed_forward_compat,
)
from clio_agent.gact.tool_observer import (
    _append_live_assistant_part,
    _merge_tool_call_rows,
    _tool_calls_from_handoff_rows,
)
from clio_agent.gact.transcript import _transcript_text_field
from clio_agent.gact.turn_state import new_turn_state
from clio_agent.gact.types import (
    ErrorInfo,
    Message,
    Part,
    Session,
    Tokens,
    UserQuestion,
)
from clio_agent.gact.usage import (
    _estimate_cost_usd,
    _reasoning_records_from_history_slice,
    _snapshot_lm_history_index,
    _usage_from_dspy_history,
    _usage_from_history_slice,
)
from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping
from clio_agent.runtime import trace
from clio_agent.runtime.lm_activity import lm_call_in_flight as _lm_call_in_flight
from clio_agent.runtime.stream_audit import stream_audit

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


def _latest_parent_resume_output(parts: list[Part], agent_id: str) -> str:
    """Return the latest child output already handed back to ``agent_id``."""

    if not agent_id:
        return ""
    for part in reversed(parts):
        if part.type != "expert_handoff" or part.stage != "parent.resumed":
            continue
        if part.agent_id != agent_id:
            continue
        metadata = part.metadata if isinstance(part.metadata, Mapping) else {}
        output = str(metadata.get("output") or "").strip()
        if output:
            return output
    return ""


def _looks_like_structured_answer(text: str) -> bool:
    """True when an expert answer is machine-readable state, not prose."""

    stripped = (text or "").lstrip()
    if not stripped:
        return False
    return stripped[0] in "{[" or stripped.startswith("```json") or stripped.startswith("```JSON")


def _render_return_summary(output: str) -> str:
    """A human-readable one-liner for a child's return, from its GENUINE answer.

    Prose answers pass through unchanged. Structured (JSON) answers — the typed
    ``dspy.extract`` deliverable — are rendered into a compact, grounded summary
    (a ``summary``/``description`` field if present, else the top-level scalar
    fields) so the transcript shows the real result instead of a generic
    "returned a compact result" placeholder. Returns "" when there is nothing
    meaningful to show (caller supplies the fallback)."""

    text = (output or "").strip()
    if not text or not _looks_like_structured_answer(text):
        return text
    body = text
    if body.startswith("```"):
        body = body.strip("`")
        body = body.split("\n", 1)[-1].strip() if "\n" in body else ""
    try:
        data = json.loads(body)
    except Exception:
        return text
    if isinstance(data, Mapping):
        node: Mapping[str, Any] = data
        # Unwrap a single-key namespace wrapper (e.g. {"geospatial": {...}}) so the
        # salient fields one level down are summarised, not just "{namespace}".
        for _ in range(2):
            if len(node) == 1:
                only = next(iter(node.values()))
                if isinstance(only, Mapping):
                    node = only
                    continue
            break
        for key in ("summary", "description", "answer", "result"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        scalars = []
        for key, value in node.items():
            if isinstance(value, bool) or isinstance(value, (str, int, float)):
                text_value = str(value)
                if len(text_value) > 60:
                    text_value = text_value[:57] + "..."
                scalars.append(f"{key}: {text_value}")
            if len(scalars) >= 6:
                break
        if scalars:
            return "; ".join(scalars)
    if isinstance(data, list):
        return f"{len(data)} item(s)"
    # Structured but unrenderable (e.g. empty object): no meaningful one-liner.
    return ""


_INTERNAL_METADATA_TOOL_NAMES = frozenset(
    {
        "clio_prior_workflow_state",
        "finish",
    }
)


def _tool_metadata_name(row: Mapping[str, Any]) -> str:
    """Return the display tool name for a metadata row."""

    return str(row.get("name") or row.get("tool") or "").strip()


def _tool_metadata_name_args_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """Return a user-visible identity for metadata-level tool summaries."""

    args = row.get("args")
    if args is None:
        args = row.get("arguments")
    if args is None:
        args = row.get("params")
    try:
        encoded_args = json.dumps(args or {}, sort_keys=True, default=str)
    except TypeError:
        encoded_args = str(args or {})
    return _tool_metadata_name(row), encoded_args


def _tool_metadata_has_result(row: Mapping[str, Any]) -> bool:
    """Return whether a metadata row has result evidence worth preserving."""

    for key in ("result", "observation", "output", "response", "result_preview"):
        if row.get(key) not in (None, "", [], {}):
            return True
    return False


def _sanitize_tools_called_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop internal tool-summary rows and de-duplicate equivalent public rows."""

    cleaned: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], int] = {}
    dropped_internal = 0
    merged_duplicates = 0
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            continue
        name = _tool_metadata_name(raw_row)
        if not name or name in _INTERNAL_METADATA_TOOL_NAMES:
            dropped_internal += 1
            continue
        row = dict(raw_row)
        key = _tool_metadata_name_args_key(row)
        existing_index = by_key.get(key)
        if existing_index is None:
            by_key[key] = len(cleaned)
            cleaned.append(row)
            continue
        merged_duplicates += 1
        existing = cleaned[existing_index]
        for field_name, value in row.items():
            if value in (None, "", [], {}):
                continue
            if field_name in {"result", "observation", "output", "response", "result_preview"}:
                if not _tool_metadata_has_result(existing):
                    existing[field_name] = value
                continue
            if field_name not in existing or existing[field_name] in (None, "", [], {}):
                existing[field_name] = value
            elif field_name in {"duration_ms", "cached", "ok", "error"}:
                existing[field_name] = value
    if rows and (dropped_internal or merged_duplicates or len(cleaned) != len(rows)):
        trace.HF_ON and trace.hot(
            "STREAM-SSE",
            "sanitized_tools_called input=%d output=%d dropped_internal=%d merged_duplicates=%d",
            len(rows),
            len(cleaned),
            dropped_internal,
            merged_duplicates,
        )
    return cleaned


def _sanitize_handoff_tool_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a handoff row with public ``tools_called`` metadata normalized."""

    cleaned = dict(row)
    tools = cleaned.get("tools_called")
    if isinstance(tools, list):
        public_tools = _sanitize_tools_called_metadata(
            [dict(tool) for tool in tools if isinstance(tool, Mapping)]
        )
        if public_tools:
            cleaned["tools_called"] = public_tools
        else:
            cleaned.pop("tools_called", None)
    children = cleaned.get("children")
    if isinstance(children, list):
        cleaned["children"] = [
            _sanitize_handoff_tool_metadata(child) if isinstance(child, Mapping) else child
            for child in children
        ]
    return cleaned


def _handoff_part_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return metadata for a display handoff part.

    Tool calls/results are emitted as explicit ordered parts. Keeping the same
    rows inside handoff metadata makes the UI render duplicate tools, so handoff
    parts carry delegation state only.
    """

    cleaned = _sanitize_handoff_tool_metadata(row)
    cleaned.pop("tools_called", None)
    children = cleaned.get("children")
    if isinstance(children, list):
        display_children: list[Any] = []
        for child in children:
            if isinstance(child, Mapping):
                child_clean = _handoff_part_metadata(child)
                display_children.append(child_clean)
            else:
                display_children.append(child)
        cleaned["children"] = display_children
    return cleaned


def _publish_transcript_event(
    bus: EventBus,
    sid: str,
    event_type: str,
    payload: Mapping[str, Any],
) -> None:
    """Publish one normalized transcript event alongside legacy message events."""

    bus.publish(Event(type=event_type, session_id=sid, payload=dict(payload)))


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
    # early return). ``_emit_chunk`` below is now a thin adapter: semantic
    # lm.token.delta + the parent-resume suppression gate (PR4 retires it) +
    # stream_audit, then one transcript call.
    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _mirror_transcript_state,
        _open_turn_transcript,
    )

    state.transcript = _open_turn_transcript(state.app, state.sid, state.turn_id)
    state.suppressed_parent_resume_offsets = {}

    def _record_streamed_field_text(agent: str, field: str, chunk: str) -> None:
        # Turn-scoped buffer (#757): stamped with THIS turn's id and cleared at
        # turn end, so the tool observer's thought dedup never matches a
        # previous turn's streamed text. Retires in PR4 (#767) in favor of
        # ``transcript.streamed_text``.
        _record_live_streamed_field_text(state.app, state.sid, state.turn_id, agent, field, chunk)

    def _settle_turn_transcript() -> None:
        """Retire the turn's ledger: freeze (no-op after finalize), close.

        ``abandon()`` freezes without publishing so late producer ops are
        rejected + audited instead of silently absorbed into the next turn; on
        the success path ``transcript.finalize()`` already froze the ledger.
        Runs on EVERY turn exit path (success, ask_user early return; the #756
        error envelope settles through ``_settle_failed_finalize``).
        """

        state.transcript.abandon()
        state.app.state.turn_transcripts.close(state.sid)

    async def _emit_chunk(
        text: str,
        agent_id: Optional[str] = None,
        field_name: str = "answer",
    ) -> None:
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
        except Exception:  # noqa: BLE001 - transcript streaming must not fail a turn
            pass
        if not text:
            trace.HF_ON and trace.hot(
                "STREAM-SSE",
                "ignored_empty_delta agent=%s field=%s",
                chunk_agent,
                stream_field,
            )
            return
        resume_output = _latest_parent_resume_output(state.transcript.snapshot(), chunk_agent)
        if stream_field == "answer" and resume_output:
            offset = state.suppressed_parent_resume_offsets.get(chunk_agent, 0)
            after = resume_output[offset + len(text) :]
            # Only suppress a duplicated chunk when it ends on a WORD BOUNDARY in
            # the resume output. Otherwise, when the parent's text diverges from
            # the child's mid-word (e.g. parent paraphrases after "Los An|geles"),
            # we'd drop "Los An" and emit "geles" — a corrupted mid-word fragment
            # that also gets stored and breaks reload. Emitting the chunk instead
            # keeps the text intact (any true full-line duplication is deduped by
            # the client's dedupeRepeatedText).
            chunk_ends_word = (not after) or after[:1].isspace() or text[-1:].isspace()
            if resume_output[offset:].startswith(text) and chunk_ends_word:
                state.suppressed_parent_resume_offsets[chunk_agent] = offset + len(text)
                trace.HF_ON and trace.hot(
                    "STREAM-SSE",
                    "suppressed_parent_resume_duplicate agent=%s len=%d head=%r",
                    chunk_agent,
                    len(text),
                    text[:80],
                )
                stream_audit(
                    "sse.normalized_emit",
                    session_id=state.sid,
                    turn_id=state.turn_id,
                    agent_id=chunk_agent,
                    field=stream_field,
                    normalized_event="turn.text.delta",
                    chunk_len=len(text),
                    duplicate_suppressed=True,
                    duplicate_reason="parent_resume_duplicate",
                    head=text[:120],
                    full_text=text[:12000],
                )
                return
        if not is_provider_thinking:
            _record_streamed_field_text(chunk_agent, stream_field, text)
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

    async def _run_dynamic_agent_sync(agent_def: "AgentDef", prompt: str) -> Any:
        runner = _blueprint_runner_for_agent(agent_def)
        loop = asyncio.get_running_loop()
        with _tool_session_context(state.sid):
            # The signature is rebuilt inside the executor (via _build_blueprint_dspy_module);
            # its routing Literal[children, "finish"] resolves children from the active
            # blueprint keyed on _ACTIVE_GACT_SESSION_ID. Set it here so the copied context
            # carries it -- otherwise children resolve empty and next_expert collapses to
            # Literal["finish"], forcing the agent to finish immediately. The keystone
            # (set_turn_identity) already binds active_app() for the whole turn, so no
            # _gact_app_context wrapper is needed here.
            _sid_tok = _ctx.set_session_id(state.sid)
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
                    state.app.state.agent,
                    agent_def,
                    prompt,
                    state.sid,
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
        trace.HF_ON and trace.hot(
            "TURN-SEMANTICS",
            "agent=%s reasoning=%r answer=%r next_expert=%r next_task=%r workflow_state_type=%s",
            getattr(agent_def, "id", "?"),
            str(getattr(_pred, "reasoning", "") or "")[:500],
            str(getattr(_pred, "answer", "") or "")[:300],
            str(getattr(_pred, "next_expert", "") or ""),
            str(getattr(_pred, "next_task", "") or "")[:300],
            type(getattr(_pred, "workflow_state", None)).__name__,
        )
        # Per-expert capture: one expert.response.completed per dynamic-agent run
        # (child or parent-resume), carrying that expert's full reasoning +
        # trajectory via _prediction_summary. Closes the nested-expert capture
        # gap -- each expert's own LM output is recorded under the turn's trace,
        # correlated by actor agent_id and the parent_expert in blueprint.
        _agent_meta = getattr(agent_def, "metadata", {}) or {}
        _emit_semantic_event(
            state.app,
            state.sid,
            "expert.response.completed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
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
        _workflow_state = _prediction_workflow_state(_pred)
        if _workflow_state:
            _publish_transcript_event(
                state.bus,
                state.sid,
                "state.updated",
                {
                    "turn_id": state.turn_id,
                    "value": _workflow_state,
                    "visibility": "hidden",
                },
            )
        # #733 (#767 PR3, mechanism 4's replacement): a TERMINAL expert's answer
        # settles its exactly-once (agent, "answer") channel HERE, at the LM-call
        # site — when the answer already streamed live the fallback is audited +
        # ignored by op identity; when it did not stream, ONE batch added+completed
        # burst lands now, in arrival order. This replaces the
        # ``expert_terminal_answers`` side-ledger + finalize ``answered_agents``
        # scan. The TURN-level responder's own terminal answer is deliberately
        # left to finalize's canonical-answer channel (its fallback text is
        # grounded against on-disk artifacts there). Delegating rounds settle
        # nothing (the deliverable is the delegation, not an answer); structured
        # (JSON) answers stay out of the visible transcript, as before.
        _expert_id = str(getattr(agent_def, "id", "") or "")
        _answer_text = str(getattr(_pred, "answer", "") or "").strip()
        _next_expert = str(getattr(_pred, "next_expert", "") or "").strip()
        _is_terminal = _next_expert not in _runtime_declared_child_ids(
            state.app, _expert_id, session_id=state.sid
        )
        if (
            _answer_text
            and _is_terminal
            and _expert_id
            and _expert_id != state.invocation_agent_id
            and not _looks_like_structured_answer(_answer_text)
        ):
            state.transcript.field_stream(_expert_id, "answer").finish(fallback_text=_answer_text)
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
                _declared = _runtime_declared_child_ids(state.app, agent_def.id, session_id=state.sid)
                if _next not in _declared:  # terminal round (finish/empty/non-route)
                    _arc_write_terminal_expert(state.app, state.sid, agent_def.id, _pred, state.turn_id)
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
            target = _resolve_runtime_dynamic_agent(state.app, target_id, session_id=state.sid)
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
                state.app,
                state.sid,
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
                state.app,
                state.sid,
                f"{delegation_event_prefix}.started",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
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
                state.app,
                state.sid,
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
                    metadata={**_handoff_part_metadata(started_row), "stream_source": "live"},
                ),
            )
            ledger_start = 0
            ledger = getattr(state.app.state, "tool_call_ledger", None)
            if isinstance(ledger, dict):
                session_rows = ledger.get(state.sid)
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
                        state.app,
                        target.id,
                        session_id=state.sid,
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
                    session_rows = ledger.get(state.sid)
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
                child_tools_called = _sanitize_tools_called_metadata(child_tools_called)
                handoff_output = "" if _looks_like_structured_answer(output) else output
                # The PUBLIC return summary is derived from the child's GENUINE
                # answer (structured answers rendered to a readable one-liner) so
                # the transcript shows the real result, not a generic placeholder.
                # handoff_output stays blanked for structured answers because it
                # feeds the parent RESUME PROMPT (which receives the typed
                # workflow_state separately, not raw JSON).
                public_return_summary = (
                    _clean_public_transcript_text(_render_return_summary(output))
                    or f"{target.id} returned to {parent_agent.id}."
                )
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
                    "output": handoff_output,
                    # Real, human-readable return summary — the same string the
                    # live delegation-return render shows, so the reload (/messages)
                    # render matches the live render (no change-on-reload).
                    "output_summary": public_return_summary,
                    # The GENUINE structured output for the "details" disclosure on
                    # reload (mirrors the live return's `response`). Empty for prose
                    # (the body already is the answer). Distinct from `output`, which
                    # stays blanked to keep the parent resume prompt clean.
                    "output_raw": output if _looks_like_structured_answer(output) else "",
                    "workflow_state": workflow_state,
                    "tools_called": child_tools_called,
                    "children": [
                        _sanitize_handoff_tool_metadata(child)
                        if isinstance(child, Mapping)
                        else child
                        for child in nested
                    ],
                }
                # completed_row carries the child's GENUINE output (the typed
                # dspy.extract deliverable) verbatim — no heuristic compaction.
                # It flows to the parent resume prompt + live Part; capture
                # (trace/ARC) carries the same full output.
                _emit_semantic_event(
                    state.app,
                    state.sid,
                    f"{delegation_event_prefix}.completed",
                    turn_id=state.turn_id,
                    trace_id=state.trace_id,
                    summary=public_return_summary,
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
                    state.app,
                    state.sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        agent_id=parent_agent.id,
                        parent_agent=parent_agent.id,
                        child_agent=target.id,
                        stage=str(completed_row.get("stage") or ""),
                        status=str(completed_row.get("status") or ""),
                        text=f"{parent_agent.id} <- {target.id}",
                        metadata={
                            **_handoff_part_metadata(completed_row),
                            "stream_source": "live",
                        },
                    ),
                )
                if workflow_state:
                    _publish_transcript_event(
                        state.bus,
                        state.sid,
                        "state.updated",
                        {
                            "turn_id": state.turn_id,
                            "value": workflow_state,
                            "visibility": "hidden",
                        },
                    )
                completed_row = _sanitize_handoff_tool_metadata(completed_row)
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
                    "output": handoff_output,
                    "workflow_state": workflow_state,
                }
                _emit_semantic_event(
                    state.app,
                    state.sid,
                    f"{delegation_event_prefix}.parent_resumed",
                    turn_id=state.turn_id,
                    trace_id=state.trace_id,
                    summary=f"{parent_agent.id} resumed after {target.id}.",
                    actor={"agent_id": parent_agent.id, "role": "parent_expert"},
                    subject={"agent_id": target.id, "role": "child_expert"},
                    blueprint=delegation_blueprint,
                    payload=resumed_row,
                )
                _append_live_assistant_part(
                    state.app,
                    state.sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        agent_id=parent_agent.id,
                        parent_agent=str(parent_agent.parent_id or ""),
                        child_agent=parent_agent.id,
                        stage=str(resumed_row.get("stage") or ""),
                        status=str(resumed_row.get("status") or ""),
                        text=f"{parent_agent.id} resumed (from {target.id})",
                        metadata={
                            **_handoff_part_metadata(resumed_row),
                            "stream_source": "live",
                        },
                    ),
                )
                executed.append(resumed_row)
            except (_TurnCancelled, _TurnTimedOut):
                raise
            except Exception as exc:  # noqa: BLE001
                child_tools_called = []
                if isinstance(ledger, dict):
                    session_rows = ledger.get(state.sid)
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
                child_tools_called = _sanitize_tools_called_metadata(child_tools_called)
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
                    state.app,
                    state.sid,
                    f"{delegation_event_prefix}.failed",
                    turn_id=state.turn_id,
                    trace_id=state.trace_id,
                    status="failed",
                    summary=f"{target.id} failed during sync delegation.",
                    actor={"agent_id": target.id, "role": "child_expert"},
                    subject={"agent_id": parent_agent.id, "role": "parent_expert"},
                    blueprint=delegation_blueprint,
                    provider={
                        "provider_id": target.default_provider,
                        "model_id": target.default_model,
                    },
                    payload=_sanitize_handoff_tool_metadata(failed_row),
                )
                _append_live_assistant_part(
                    state.app,
                    state.sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        agent_id=parent_agent.id,
                        parent_agent=parent_agent.id,
                        child_agent=target.id,
                        stage=str(failed_row.get("stage") or ""),
                        status=str(failed_row.get("status") or ""),
                        text=f"{parent_agent.id} -> {target.id} (failed)",
                        metadata={
                            **_handoff_part_metadata(failed_row),
                            "stream_source": "live",
                        },
                    ),
                )
                executed.append(_sanitize_handoff_tool_metadata(failed_row))
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
        declared_child_ids = _runtime_declared_child_ids(state.app, parent_agent.id, session_id=state.sid)

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
                state.app, state.sid, parent_agent.id, latest_pred, next_expert, next_task, state.turn_id
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
                        _emit_chunk,
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
                            _emit_chunk,
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
            state.pred, state.expert_handoffs = await _settle_dynamic_agent_delegations(
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
            _settle_turn_transcript()
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
        # CLIO-BBBBBBBBBB24: cost + token rollup. Real DSPy
        # predictions don't always populate .tokens / .cost_usd
        # directly — pull from the per-turn UsageTracker first
        # (works across threads + streaming), then LM history.
        raw_tokens = getattr(state.pred, "tokens", None)
        if raw_tokens is not None:
            for key in state.turn_tokens:
                if isinstance(raw_tokens, dict):
                    v = raw_tokens.get(key, 0)
                else:
                    v = getattr(raw_tokens, key, 0)
                state.turn_tokens[key] = int(v or 0)
        else:
            # Diff the LM history slice for this turn first — captures
            # planner + expert + chat calls cleanly. Falls back to
            # ``last entry only`` for older code paths, then to a
            # character-based estimate when the upstream proxy
            # reports zero (some OpenAI-compatible proxies don't
            # populate usage on chunked replies).
            history_end = _snapshot_lm_history_index(state.app)
            history_made_calls = any(
                history_end.get(k, 0) > state.history_start.get(k, 0)
                for k in {*state.history_start.keys(), *history_end.keys()}
            )
            usage = _usage_from_history_slice(state.history_start, state.app)
            if not usage.get("output"):
                usage = _usage_from_dspy_history()
            for key in state.turn_tokens:
                state.turn_tokens[key] = int(usage.get(key, 0) or 0)
            state.turn_cost = float(usage.get("cost_usd", 0.0) or 0.0)
            # Char-based fallback only when the LM actually fired
            # this turn (history grew) but the upstream proxy
            # reported zero usage. Don't synthesize numbers when
            # there was no real call (e.g. unit tests with a fake
            # agent that bypasses dspy.LM entirely).
            if history_made_calls:
                if state.turn_tokens["output"] == 0 and state.answer_text:
                    state.turn_tokens["output"] = max(1, len(state.answer_text) // 4)
                if state.turn_tokens["input"] == 0 and state.enriched_text:
                    state.turn_tokens["input"] = max(1, len(state.enriched_text) // 4)
                if state.turn_cost == 0.0:
                    state.turn_cost = _estimate_cost_usd(
                        _current_lm_model_id(),
                        state.turn_tokens["input"],
                        state.turn_tokens["output"],
                    )
        if not state.turn_cost:
            state.turn_cost = float(getattr(state.pred, "cost_usd", 0.0) or 0.0)
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
        # (``_emit_chunk``'s chat-path default) — the same top-level LM call's
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
        for spawn in state.nanoagents:
            get = (
                spawn.get
                if isinstance(spawn, dict)
                else (lambda k, default=None, _s=spawn: getattr(_s, k, default))
            )
            agent_id = get("agent_id") or get("agent") or "nanoagent"
            spawn_input = get("input") or {}
            answer = get("answer") or ""
            state.tools_called = get("tools_called") or get("tools") or []
            subsess = state.app.state.sessions.create(
                workspace_id=state.sess.workspace_id,
                title=f"{agent_id} subagent",
                parent_session_id=state.sid,
                agent={"id": str(agent_id), "mode": "subagent"},
                metadata={
                    "session_type": "nanoagent",
                    "agent_id": str(agent_id),
                    "parent_session_id": state.sid,
                    "spawned_by_message_id": assistant_msg.id,
                    "spawned_by_agent": state.selected_agent,
                    "tool_count": len(state.tools_called) if isinstance(state.tools_called, list) else 0,
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
                    "parent_session_id": state.sid,
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
                metadata={"tools_called": state.tools_called} if state.tools_called else {},
            )
            _extend_session_messages(state.app, subsess.id, [sub_user, sub_asst])
            state.app.state.sessions.update(subsess.id, message_count=2, status="idle")
            _emit_semantic_event(
                state.app,
                state.sid,
                "subagent.started",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="running",
                summary=f"Spawned subagent {agent_id}.",
                actor={"agent_id": state.selected_agent or "orchestrator"},
                subject={"agent_id": str(agent_id), "session_id": subsess.id},
                payload={
                    "parent_session_id": state.sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "spawned_by_message_id": assistant_msg.id,
                },
            )
            state.bus.publish(
                Event(
                    type="subagent.started",
                    session_id=state.sid,
                    payload={
                        "parent_session_id": state.sid,
                        "child_session_id": subsess.id,
                        "agent_id": agent_id,
                        "spawned_by_message_id": assistant_msg.id,
                    },
                )
            )
            _emit_semantic_event(
                state.app,
                state.sid,
                "subagent.completed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary=f"Subagent {agent_id} completed.",
                actor={"agent_id": str(agent_id), "session_id": subsess.id},
                subject={"session_id": state.sid},
                payload={
                    "parent_session_id": state.sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                    "tokens": get("tokens") or {},
                    "cost_usd": float(get("cost_usd", 0.0) or 0.0),
                },
            )
            state.bus.publish(
                Event(
                    type="subagent.completed",
                    session_id=state.sid,
                    payload={
                        "parent_session_id": state.sid,
                        "child_session_id": subsess.id,
                        "agent_id": agent_id,
                        "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                        "tokens": get("tokens") or {},
                        "cost_usd": float(get("cost_usd", 0.0) or 0.0),
                    },
                )
            )

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
        _settle_turn_transcript()
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
