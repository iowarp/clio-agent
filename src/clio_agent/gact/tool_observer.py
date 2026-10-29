"""Tool observation and live-assistant transcript projection (#714).

Installs runtime hooks and projects route context, call lifecycle events, live
parts, and per-turn ledger entries without changing tool execution semantics.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Optional, cast

from clio_agent.gact import context as _ctx
from clio_agent.gact.artifacts.ingest_edges import join_call_to_serving_child
from clio_agent.gact.artifacts.observer_bridge import report_observer_call
from clio_agent.gact.artifacts.observer_provenance import tool_provenance_metadata
from clio_agent.gact.artifacts.provenance_presentation import with_provenance_blocks
from clio_agent.gact.delegation import _expert_handoff_fields
from clio_agent.gact.events import Event
from clio_agent.gact.evidence import _bounded_tool_call_result
from clio_agent.gact.file_changes import pop_previous_content, record_file_change
from clio_agent.gact.permission_gate import (
    _make_cancellation_checker,
    _make_permission_gate,
)
from clio_agent.gact.presentation_observer import completed_presentation, publish_presentation_delta
from clio_agent.gact.runtime.globals import (
    _active_semantic_turn_id,
    _emit_semantic_event,
    _iso_from_epoch,
    _new_message_id,
    _resolve_tool_session,
)
from clio_agent.gact.task_wait_presentation import completed_after_cancel_error, completion_status
from clio_agent.gact.thought_dedup import TOOL_THOUGHT_STAGE, resolve_started_tool_call_thought
from clio_agent.gact.tool_call_merge import (
    _merge_tool_call_rows as _merge_tool_call_rows,
)
from clio_agent.gact.tool_call_merge import (
    _normalize_tool_call_row as _normalize_tool_call_row,
)
from clio_agent.gact.tool_call_merge import (
    _tool_call_has_result_evidence as _tool_call_has_result_evidence,
)
from clio_agent.gact.tool_call_merge import (
    _tool_calls_from_handoff_rows as _tool_calls_from_handoff_rows,
)
from clio_agent.gact.tool_completion_delivery import terminal_tool_delivery
from clio_agent.gact.tool_observer_parts import completed_tool_metadata, completed_tool_text
from clio_agent.gact.tool_progress import ToolProgressRegistry
from clio_agent.gact.types import Message, Part
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit
from clio_agent.tools.mcp_results import content_blocks_for_wire
from clio_agent.tools.result_errors import structured_tool_result_error

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.transcript import TurnTranscript

logger = logging.getLogger(__name__)

# Per-invocation state follows async calls and their finite recording workers.
_OBSERVER_CALL_IDS: ContextVar[str | None] = ContextVar("observer_call_id", default=None)
_OBSERVER_CALL_T0: ContextVar[float | None] = ContextVar("observer_call_started", default=None)


from clio_agent.gact.tool_event_identity import (
    _tool_call_event_key as _tool_call_event_key,
)
from clio_agent.gact.tool_event_identity import (
    _tool_call_name_args_key as _tool_call_name_args_key,
)


def _session_turn_transcript(app: "FastAPI", sid: str) -> "Optional[TurnTranscript]":
    """The open TurnTranscript ledger for ``sid``, or ``None`` (#767 PR1).

    Since PR2 every production turn opens a ledger, so during a turn the
    live-part helpers below shim into it (the ledger owns identity/order/
    events); with none open — out-of-band tool calls outside any turn — they
    fall back to the legacy ``app.state`` dict path, byte-for-byte unchanged.
    """

    registry = getattr(app.state, "turn_transcripts", None)
    if registry is None:
        return None
    return registry.get(sid)


def _open_turn_transcript(app: "FastAPI", sid: str, turn_id: str) -> "TurnTranscript":
    """Open the turn's TurnTranscript ledger (#767 PR2) — turn-loop entrypoint.

    Opens the registry ledger for ``sid``/``turn_id``, ADOPTS an ask_user-
    paused turn's carried in-flight assistant state when the legacy dicts hold
    one (message id + live parts + once-keys survive the pause today — the
    resume turn continues the SAME assistant message, no second
    ``message.created``), then aliases the new ledger into the legacy
    ``app.state`` dicts so untouched finalize reads and the live projection
    keep working during the PR2/PR3 window.

    Parts are stored VERBATIM (#881; contract markers split at the root, #877). #1337:
    the seal sink persists each part when it becomes final (part_atom_minter).
    """

    from clio_agent.gact.part_atom_minter import transcript_sink  # noqa: PLC0415
    from clio_agent.gact.transcript import EventBusTranscriptPublisher  # noqa: PLC0415

    carried_msg_id = str(
        (getattr(app.state, "live_assistant_message_ids", {}) or {}).get(sid) or ""
    )
    carried_parts = list((getattr(app.state, "live_assistant_parts", {}) or {}).get(sid, []))
    carried_keys = set((getattr(app.state, "live_assistant_part_keys", {}) or {}).get(sid, set()))
    transcript = app.state.turn_transcripts.open_turn(
        sid,
        turn_id,
        EventBusTranscriptPublisher(app.state.bus, sid),
        sink=transcript_sink(app, sid),
    )
    if carried_msg_id or carried_parts or carried_keys:
        transcript.adopt_carried_state(
            carried_msg_id,
            parts=carried_parts,
            once_keys=carried_keys,
        )
    # Bind the aliases directly: the carried parts now live in the NEW ledger
    # list, so the plain mirror's "legacy parts present" conflict warning must
    # not fire for the deliberate ask_user carry.
    live_parts = getattr(app.state, "live_assistant_parts", None)
    if live_parts is None:
        live_parts = {}
        app.state.live_assistant_parts = live_parts
    live_parts[sid] = transcript.live_parts_alias()
    _mirror_transcript_state(app, sid, transcript)
    return transcript


def _mirror_transcript_state(app: "FastAPI", sid: str, transcript: "TurnTranscript") -> None:
    """Alias the transcript's identity/ledger into the legacy ``app.state`` dicts.

    ``app.state.live_assistant_parts[sid]`` becomes the transcript's internal
    ledger list and ``live_assistant_message_ids[sid]`` its message id, so
    untouched ``turn.py`` finalize reads (and ``routes/messages.py``'s live
    projection) keep working during the PR1/PR2 migration window.

    A frozen (settled/abandoned) ledger is never mirrored: an executor-thread
    producer that fetched the transcript just before ``abandon()`` →
    ``registry.close()`` must not hand the finished turn's identity and parts
    back to the just-popped legacy dicts (the same poison class as the late
    stream-tap chunk guarded in ``turn.py``'s ``_emit_chunk``).
    """

    if transcript.frozen:
        logger.warning(
            "turn_transcript mirror skipped reason=frozen_transcript_mirror "
            "session=%s message=%s — settled ledgers never re-enter the live dicts",
            sid,
            transcript.message_id or "",
        )
        return

    live_ids = getattr(app.state, "live_assistant_message_ids", None)
    if live_ids is None:
        live_ids = {}
        app.state.live_assistant_message_ids = live_ids
    if transcript.message_id:
        prior = str(live_ids.get(sid) or "")
        if prior and prior != transcript.message_id:
            logger.warning(
                "turn_transcript identity conflict reason=legacy_live_message_id_mismatch "
                "session=%s legacy=%s transcript=%s — transcript id wins",
                sid,
                prior,
                transcript.message_id,
            )
        live_ids[sid] = transcript.message_id
    live_parts = getattr(app.state, "live_assistant_parts", None)
    if live_parts is None:
        live_parts = {}
        app.state.live_assistant_parts = live_parts
    alias = transcript.live_parts_alias()
    existing = live_parts.get(sid)
    if existing is not alias:
        if existing:
            logger.warning(
                "turn_transcript alias conflict reason=legacy_live_parts_present "
                "session=%s legacy_count=%d — transcript ledger wins",
                sid,
                len(existing),
            )
        live_parts[sid] = alias


def _install_tool_runtime_hooks(app: "FastAPI") -> None:
    """Install permission, cancellation, and telemetry hooks for tool calls."""

    checker = getattr(app.state, "pending_cancellation_checker", None)
    if checker is None:
        checker = _make_cancellation_checker(app)
    gate = getattr(app.state, "pending_permission_gate", None)
    if gate is None:
        gate = _make_permission_gate(app)
    observer = getattr(app.state, "pending_tool_observer", None)
    if observer is None:
        observer = _make_tool_observer(app)
    # P2.3: the interceptor (pure consumer of the gate-stashed PreToolUse
    # modify/synthesize decision) + the PostToolUse producer, defaulting in when a
    # caller (test) has not pre-installed one.
    from clio_agent.gact.hooks import make_post_tool_hook, pre_tool_interceptor  # noqa: PLC0415

    interceptor = getattr(app.state, "pending_tool_interceptor", None) or pre_tool_interceptor
    post_tool = getattr(app.state, "pending_post_tool", None) or make_post_tool_hook(app)
    app.state.pending_cancellation_checker = checker
    app.state.pending_permission_gate = gate
    app.state.pending_tool_interceptor = interceptor
    app.state.pending_post_tool = post_tool
    app.state.pending_tool_observer = observer
    app.state.tool_hooks_installed = True
    # #735 (unified §1): install ONLY stamps this app's ``pending_*`` hooks. The
    # in-turn path resolves them per-app via ``resolve_tool_runtime`` (dispatching
    # on the keystone-bound ``active_app()``). We deliberately do NOT record them
    # as the process-global ``_FALLBACK_TOOL_RUNTIME``: in a multi-app process the
    # last install would win, so an app-less resolve would hand one app's call a
    # SIBLING app's gate/observer — the exact cross-app leak this design forbids.
    # App-less tool calls resolve to the neutral fallback + a loud
    # ``tool_runtime_unresolved`` reason instead (never a sibling's value).


def _ensure_live_assistant_message(app: "FastAPI", sid: str) -> str:
    """Return the in-flight assistant message id, creating it if needed."""

    transcript = _session_turn_transcript(app, sid)
    if transcript is not None:
        # #767 PR1: the ledger is the sole minter of the assistant message id
        # (message.created once); mirrored into the legacy dicts for untouched readers.
        msg_id = transcript.ensure_message()
        _mirror_transcript_state(app, sid, transcript)
        return msg_id

    live_ids = getattr(app.state, "live_assistant_message_ids", None)
    if live_ids is None:
        live_ids = {}
        app.state.live_assistant_message_ids = live_ids
    msg_id = str(live_ids.get(sid) or "")
    if msg_id:
        return msg_id
    msg_id = _new_message_id("asst")
    live_ids[sid] = msg_id
    now = _iso_from_epoch(time.time())
    app.state.bus.publish(
        Event(
            type="message.created",
            session_id=sid,
            payload=Message(
                id=msg_id,
                turn_id=_active_semantic_turn_id(),
                session_id=sid,
                role="assistant",
                created_at=now,
                updated_at=now,
                parts=[],
            ).to_wire(),
        )
    )
    return msg_id


def _append_live_assistant_part(app: "FastAPI", sid: str, part: Part) -> None:
    """Publish and remember a real runtime part for the active assistant turn."""

    transcript = _session_turn_transcript(app, sid)
    if transcript is not None:
        # #767 PR1: append through the single-writer ledger — it closes its own
        # open text, mints ids, and publishes message.part.added itself.
        # Each phase owns one row; the terminal return remains a later event.
        if part.type == "expert_handoff":
            transcript.upsert_delegation_part(part)
        else:
            transcript.append_part(part)
        _mirror_transcript_state(app, sid, transcript)
        return
    msg_id = _ensure_live_assistant_message(app, sid)
    live_parts = getattr(app.state, "live_assistant_parts", None)
    if live_parts is None:
        live_parts = {}
        app.state.live_assistant_parts = live_parts
    rows = live_parts.setdefault(sid, [])
    updated = False
    if part.type == "expert_handoff" and str(part.handle_id or ""):
        part_phase = (str(part.handle_id or ""), str(part.stage or ""))
        for index, row in enumerate(rows):
            row_phase = (str(row.handle_id or ""), str(row.stage or ""))
            if row.type == "expert_handoff" and row_phase == part_phase:
                part.id = row.id
                part.sequence = row.sequence
                part.metadata = {**row.metadata, **part.metadata}
                rows[index] = part
                updated = True
                break
    if not updated:
        rows.append(part)
    app.state.bus.publish(
        Event(
            type="message.part.updated" if updated else "message.part.added",
            session_id=sid,
            payload={
                "turn_id": _active_semantic_turn_id(),
                "message_id": msg_id,
                # Real runtime parts (tool calls/results) emitted live during
                # the turn (#711); not provider-token text, but emitted in real time.
                "stream_source": str(part.metadata.get("stream_source") or "live"),
                "part": part.to_wire(),
            },
        )
    )


def _append_live_assistant_part_once(
    app: "FastAPI",
    sid: str,
    key: str,
    part: Part,
) -> bool:
    """Publish a live part once per in-flight turn.

    Tool observers can fire many times for the same routed expert. The
    transcript should show the route decision once, then the concrete tool
    calls/results under it, not repeat the same route banner for every call.
    """

    transcript = _session_turn_transcript(app, sid)
    if transcript is not None:
        # #767 PR1: the idempotency key is turn-scoped ledger state. A duplicate key
        # never closes streamed text — the boundary close runs only when the key is fresh.
        if transcript.has_part_key(key):
            return False
        appended = transcript.append_part_once(key, part)
        _mirror_transcript_state(app, sid, transcript)
        return appended is not None

    live_keys = getattr(app.state, "live_assistant_part_keys", None)
    if live_keys is None:
        live_keys = {}
        app.state.live_assistant_part_keys = live_keys
    session_keys = live_keys.setdefault(sid, set())
    if key in session_keys:
        return False
    session_keys.add(key)
    _append_live_assistant_part(app, sid, part)
    return True


def _agent_tool_owner(app: "FastAPI", tool_name: str) -> tuple[str, str]:
    """Return (public_parent, owner) for a tool if CLIO can resolve it."""

    agent = getattr(app.state, "agent", None)
    if agent is None:
        return "", ""
    candidates = [tool_name]
    if "." in tool_name:
        candidates.append(tool_name.rsplit(".", 1)[-1])
    for candidate in candidates:
        try:
            owner = str(agent._selected_expert_for_tool(candidate) or "")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            continue
        if not owner:
            continue
        try:
            parent = str(agent._parent_route_for_child(owner) or "")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            parent = ""
        return parent or owner, owner
    return "", ""


def _routing_event_once(app: "FastAPI", sid: str, key: str) -> bool:
    """Once-per-turn guard for routing events (the part-once key store, reused)."""

    transcript = _session_turn_transcript(app, sid)
    if transcript is not None:
        if transcript.has_part_key(key):
            return False
        transcript.mark_part_key(key)
        return True
    live_keys = getattr(app.state, "live_assistant_part_keys", None)
    if live_keys is None:
        live_keys = {}
        app.state.live_assistant_part_keys = live_keys
    session_keys = live_keys.setdefault(sid, set())
    if key in session_keys:
        return False
    session_keys.add(key)
    return True


def _emit_live_tool_route_context(app: "FastAPI", sid: str, tool_name: str) -> None:
    """Emit route/handoff context immediately before a live tool call."""

    public_agent, owner = _agent_tool_owner(app, tool_name)
    if not public_agent or public_agent in {"chat", "none"}:
        return
    # Clean-wire rule (owner 2026-08-05): a routing decision is OBSERVABILITY,
    # not conversation — it rides the semantic highway (trace + obs SSE), never
    # a transcript part. Same once-per-turn key the part used.
    if not _routing_event_once(app, sid, f"route:{public_agent}"):
        return
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _active_semantic_trace_id,
        _active_semantic_turn_id,
        _emit_semantic_event,
    )

    _emit_semantic_event(
        app,
        sid,
        "routing.decision",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="completed",
        summary=f"routed to {public_agent}",
        actor={"agent_id": "main", "role": "orchestrator"},
        subject={"selected_agent": public_agent},
        payload={
            "selected_agent": public_agent,
            "rationale": f"Agent planner selected {public_agent} for tool {tool_name}.",
            "route_source": "live_tool_observer",
            "route_reason": f"Resolved from live tool owner {owner}.",
            "execution_path": f"orchestrator -> {public_agent}",
        },
    )
    if owner and owner != public_agent:
        row = {
            "agent_id": owner,
            "parent_id": public_agent,
            "dispatch_target": owner,
            "status": "running",
            "stage": "tool.started",
            "delegation_lifecycle": "sync",
            "execution_mode": "tool",
            "depth": 1,
            # #880: pending state rides the typed status/stage fields, not prose.
        }
        handoff_fields = _expert_handoff_fields(row)
        _append_live_assistant_part_once(
            app,
            sid,
            f"handoff:{public_agent}:{owner}",
            Part(
                id=f"live_handoff_{public_agent}_{owner}",
                type="expert_handoff",
                # Structured fields are the contract; ``text`` is a short label only.
                # The parent (decider) generates the handoff.
                agent_id=public_agent,
                parent_agent=handoff_fields["parent_agent"],
                child_agent=handoff_fields["child_agent"],
                stage=handoff_fields["stage"],
                status=handoff_fields["status"],
                text=f"{public_agent} -> {owner}",
                metadata={**row, "stream_source": "live", "route_source": "live_tool_observer"},
            ),
        )


def _make_tool_observer(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.tool_observer.

    Events attach to the active session when present and fall back to recency
    only for out-of-band calls; completions also enter the per-turn ledger.
    """

    # Declared-presentation + call-metadata registries (populated at the seam;
    # default "row"/no-metadata for undeclared names — never name-matching).
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        bounded_tool_call_input,
        declared_tool_representation,
        declared_tool_title,
        pop_declared_structured_content,
        tool_call_metadata_resolver,
    )

    progress_registry = ToolProgressRegistry()

    def observe(
        name: str,
        args: Mapping[str, Any],
        phase: Optional[str],
        error: Optional[str],
        result: Any | None = None,
    ) -> Any | None:
        if phase == "progress":
            projected = progress_registry.project(name, result)
            if projected is None:
                return None
            sid, payload, handle = projected
            publish_presentation_delta(app, sid, payload)
            app.state.bus.publish(Event(type="tool.call.progress", session_id=sid, payload=payload))
            return handle

        if phase == "started":
            # Belt-and-braces leak fix: discard a PRIOR call's leaked
            # declare_structured_content() before it can be misread as this call's.
            pop_declared_structured_content()
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return None
        # A representation may only ADD adornment, never remove the call row
        # (owner ruling, P5 wire semantics) -- every EXECUTED call emits its
        # tool_call/tool_result parts unconditionally. "handoff" alone skips
        # the row (its own expert_handoff part already IS call evidence).
        representation = declared_tool_representation(name)
        representation_fields = (
            {"representation": representation} if representation != "row" else {}
        )
        # Round-9: tool.call.* events carry the same curated title (never
        # fabricated) Part.tool_title uses below, so obs "called" rows stop
        # rendering the raw tool name.
        tool_title = declared_tool_title(name)
        tool_title_fields = {"tool_title": tool_title} if tool_title else {}
        # The expert that OWNS (runs) this tool, for per-part attribution (#732).
        _public_agent, tool_owner = _agent_tool_owner(app, name)
        # Attribute to the INVOKING expert (active ReAct scope), not the tool's owning
        # server/group; fall back to the owner outside a react scope (#732).
        invoking_expert = _ctx.active_react_scope() or tool_owner
        if phase == "started":
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            # Concurrent coroutines must retain their own call identity and clock.
            _OBSERVER_CALL_IDS.set(call_id)
            report_observer_call(call_id)
            started_at = time.time()
            _OBSERVER_CALL_T0.set(started_at)
            observer_handle = progress_registry.started(call_id, sid)
            from clio_agent.gact.presentation_observer import starting_presentation

            initial_presentation = starting_presentation(name, args)
            # B5 #979.7 (deferred B4 WRITER): join call_id → confined FLEET child (no-op on the
            # floor / built-in namespaces → the egress mint abstains). See ingest_edges.
            join_call_to_serving_child(app, sid, name, call_id)
            _emit_live_tool_route_context(app, sid, name)
            # Shared by the semantic event + the dedicated bus event below (safe:
            # _build_semantic_event copies rather than mutating it).
            started_payload = {
                "call_id": call_id,
                "started_at": _iso_from_epoch(started_at),
                "tool": name,
                "args": dict(args),
                "presentation": initial_presentation,
                "telemetry_source": "live_observer",
                **representation_fields,
                **tool_title_fields,
            }
            _emit_semantic_event(
                app,
                sid,
                "tool.call.started",
                turn_id=_ctx.active_turn_id(),
                trace_id=_ctx.active_trace_id(),
                status="running",
                summary=f"Tool {name} started.",
                actor={"tool": name},
                subject={"call_id": call_id},
                payload=started_payload,
            )
            app.state.bus.publish(
                Event(type="tool.call.started", session_id=sid, payload=started_payload)
            )
            if representation == "handoff":
                return observer_handle
            step_thought = _ctx.active_step_thought()
            raw_step_thought = step_thought
            transcript = _session_turn_transcript(app, sid)
            # #953: read the RUN-KEYED tap bucket (bare invoking_expert still owns attribution).
            _tap_scope = _ctx.run_keyed_scope(invoking_expert)
            thought_step_id = _ctx.active_parent_span_id()
            step_thought, decision = resolve_started_tool_call_thought(
                transcript=transcript,
                tap_scope=_tap_scope,
                thought_step_id=thought_step_id,
                step_thought=step_thought,
            )
            if raw_step_thought:
                stream_audit(
                    TOOL_THOUGHT_STAGE,
                    agent_id=invoking_expert,
                    field="next_thought",
                    visible=False,
                    duplicate_suppressed=decision.clear,
                    duplicate_reason=decision.reason,
                    step_id=thought_step_id,
                    head=raw_step_thought[:120],
                )
            call_metadata = {
                "started_at": _iso_from_epoch(started_at),
                "stream_source": "live",
                "telemetry_source": "live_observer",
                **({"thought_step_id": thought_step_id} if thought_step_id else {}),
            }
            # Per-tool STARTED metadata via the registry (tool_instrumentation.py)
            # -- never a hardcoded tool name in this generic path.
            metadata_resolver = tool_call_metadata_resolver(name)
            if metadata_resolver is not None:
                call_metadata.update(metadata_resolver(app, args))
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_call",
                    type="tool_call",
                    agent_id=invoking_expert,
                    call_id=call_id,
                    tool_name=name,
                    tool_title=declared_tool_title(name),
                    # The step's reasoning rides the tool_call part (#732): the
                    # model's text and the action it chose are one ordered event.
                    thought=step_thought,
                    input=bounded_tool_call_input(name, args),
                    presentation=initial_presentation,
                    metadata=call_metadata,
                ),
            )
            return observer_handle
        elif phase == "completed":
            call_id = _OBSERVER_CALL_IDS.get() or ""
            terminal_output = progress_registry.completed(call_id)
            t0 = _OBSERVER_CALL_T0.get()
            completed_epoch = time.time()
            duration_ms = (completed_epoch - t0) * 1000 if t0 else 0.0
            _OBSERVER_CALL_T0.set(None)  # The tool returned, even if recording fails.
            cancel_event = app.state.cancel_events.get(sid)
            completed_after_cancel = sid in app.state.cancel_flags or (
                cancel_event is not None and cancel_event.is_set()
            )
            completion_error = error
            prev = pop_previous_content(result)  # Undo's pre-write copy: never on a wire
            if completion_error is None and result is not None:
                completion_error = structured_tool_result_error(result)
            cancellation_metadata: dict[str, Any] = {}
            if completed_after_cancel:
                completion_error = completed_after_cancel_error(name, completion_error)
                cancellation_metadata = {"execution_cancellation": "best_effort"}
            ok = completion_error is None
            # A native tool may DECLARE its typed structured payload (P5; the model's
            # return value is unchanged). Read + cleared once per completed call so it
            # never leaks onto a later call; else the MCP-shaped extraction below.
            declared_structured = pop_declared_structured_content()
            structured_content = (
                declared_structured
                if declared_structured is not None
                else (
                    result.get("structuredContent")
                    if isinstance(result, Mapping) and "structuredContent" in result
                    else None
                )
            )
            result, presentation = completed_presentation(
                name, args, result, structured_content, terminal_output, error=completion_error
            )
            status = completion_status(name, ok, completed_after_cancel, result, presentation)
            result_summary = f"Tool {name} {status}."
            # Seam #966 S1+S5 (#971): TransformRecord before the SSE payload (reload==live).
            from clio_agent.gact.artifacts.transforms import observe_tool_transform  # noqa: PLC0415

            transform_record = (
                observe_tool_transform(
                    app, sid, name, dict(args), call_id, ok, result, call_started_at=t0
                )
                if not completed_after_cancel
                else None
            )
            provenance = tool_provenance_metadata(transform_record)
            # Never None here (completed_presentation returns dict); narrows the Optional cast.
            presentation = cast(dict[str, Any], with_provenance_blocks(presentation, provenance))
            # Served payload = the tool-response atom's FACTS (ok/duration/cached/result/
            # error). No ui_summary/result_summary captions — clio transmits, it does not
            # author UI labels; the envelope ``summary`` below is the one short caption.
            payload = {
                "call_id": call_id,
                "completed_at": _iso_from_epoch(completed_epoch),
                "tool": name,
                "ok": ok,
                "presentation": presentation,
                "duration_ms": duration_ms,
                "cached": False,
                "telemetry_source": "live_observer",
                **representation_fields,
                **tool_title_fields,
                **({"error": completion_error} if completion_error else {}),
                **({"result": _bounded_tool_call_result(result)} if result is not None else {}),
                **cancellation_metadata,
            }
            # Append to the per-session ledger FIRST -- before the (potentially
            # I/O-bound, e.g. durable-trace-writing) semantic emit + live parts --
            # so the turn handler's post-forward drain never races a slow emit and
            # drops tools_called from the assistant message metadata.
            ledger = getattr(app.state, "tool_call_ledger", None)
            if ledger is not None and not completed_after_cancel:
                ledger.setdefault(sid, []).append(
                    {
                        "name": name,
                        "call_id": call_id,
                        "args": dict(args),
                        "ok": ok,
                        "duration_ms": duration_ms,
                        "cached": False,
                        "telemetry_source": "live_observer",
                        **representation_fields,
                        **({"error": completion_error} if completion_error else {}),
                        **(
                            {"result": _bounded_tool_call_result(result)}
                            if result is not None
                            else {}
                        ),
                        **cancellation_metadata,
                    }
                )
            # Trace keeps the full result; wire/ledger projections are bounded.
            # SSE redacts `result` through SENSITIVE_KEYS.
            trace_payload = {**payload, "result": result} if result is not None else payload
            try:
                with terminal_tool_delivery(app, sid, payload):
                    _emit_semantic_event(
                        app,
                        sid,
                        "tool.call.completed",
                        turn_id=_ctx.active_turn_id(),
                        trace_id=_ctx.active_trace_id(),
                        status=status,
                        summary=result_summary,
                        actor={"tool": name},
                        subject={"call_id": call_id},
                        payload=trace_payload,
                    )
                from clio_agent.gact.spotter_watcher import wake_on_parent_activity  # noqa: PLC0415

                wake_on_parent_activity(app, sid, tool_name=name, ok=ok, result=structured_content)
            finally:
                if representation != "handoff":
                    result_text = completed_tool_text(result, completion_error)
                    _append_live_assistant_part(
                        app,
                        sid,
                        Part(
                            id=f"live_{call_id}_result",
                            type="tool_result",
                            agent_id=invoking_expert,
                            call_id=call_id,
                            tool_name=name,
                            is_error=not ok,
                            duration_ms=duration_ms,
                            cached=False,
                            # #1190: the structured copy is a TOP-LEVEL part field (the UI
                            # render ladder's contract) — ONE home, never mirrored into
                            # ``metadata``. Wire-only: the model's observation is the
                            # separate ``model_text`` built at the execution boundary.
                            structured_content=structured_content,
                            presentation=presentation,
                            content_blocks=content_blocks_for_wire(result),  # #1188
                            content=[
                                Part(
                                    id=f"live_{call_id}_result_text",
                                    type="text",
                                    agent_id=invoking_expert,
                                    text=result_text,
                                )
                            ],
                            metadata=completed_tool_metadata(
                                result,
                                completed_at=_iso_from_epoch(completed_epoch),
                                cancellation=cancellation_metadata,
                                provenance=provenance,
                            ),
                        ),
                    )
                # A write that ran is on disk even if the turn was cancelled meanwhile.
                record_file_change(app, sid, name, call_id, error, result, prev, invoking_expert)
        return None

    return observe


_INTERNAL_METADATA_TOOL_NAMES = frozenset({"finish"})


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
