"""Tool-observer + live-assistant transcript cluster (#714 decomposition).

This module owns the runtime plumbing that turns each MCP tool call into:

* installed global hooks (permission / cancellation / telemetry observer) via
  :func:`_install_tool_runtime_hooks`;
* live transcript parts on the in-flight assistant message
  (:func:`_ensure_live_assistant_message`, :func:`_append_live_assistant_part`
  and its once-per-turn variant) so the UI shows route banners, tool calls, and
  tool results in real time (#711);
* route/handoff context emitted just before a live tool call
  (:func:`_agent_tool_owner`, :func:`_emit_live_tool_route_context`);
* the observer callable itself (:func:`_make_tool_observer`) that publishes
  ``tool.call.started`` / ``tool.call.completed`` onto the EventBus + semantic
  highway and appends each completed call to ``app.state.tool_call_ledger``.

These were carved out of ``gact/app.py`` verbatim (pure move, behavior
preserved). ``app.py`` re-exports every symbol so existing
``from clio_agent.gact.app import <name>`` callers + test seams stay green; in
particular ``build_app`` wires ``app.state.make_tool_observer`` and the
``GactDeps.install_tool_runtime_hooks`` seam from here.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.artifacts.ingest_edges import join_call_to_serving_child
from clio_agent.gact.delegation import _expert_handoff_fields
from clio_agent.gact.events import Event
from clio_agent.gact.evidence import (
    _bounded_tool_call_result,
    _tool_result_preview,
)
from clio_agent.gact.permission_gate import (
    _make_cancellation_checker,
    _make_permission_gate,
)
from clio_agent.gact.runtime.globals import (
    _active_semantic_turn_id,
    _emit_semantic_event,
    _iso_from_epoch,
    _new_message_id,
    _resolve_tool_session,
)
from clio_agent.gact.thought_dedup import TOOL_THOUGHT_STAGE, classify_live_thought
from clio_agent.gact.types import Message, Part
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.transcript import TurnTranscript

logger = logging.getLogger(__name__)

# Per-thread call_id + start-time stash so the ``completed`` phase reuses the
# same id and can compute duration. MCPToolBridge invokes the observer on a
# worker thread, so threading-locals (not contextvars) are the right scope.
_OBSERVER_CALL_IDS = threading.local()
_OBSERVER_CALL_T0 = threading.local()


def _tool_call_event_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a stable identity for de-duplicating tool telemetry events."""
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        return "__call_id__", call_id
    return _tool_call_name_args_key(call)


def _tool_call_name_args_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a tool-name/arguments identity for posthoc trajectory rows."""

    name = str(call.get("name") or call.get("tool") or "")
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    try:
        encoded_args = json.dumps(args or {}, sort_keys=True, default=str)
    except TypeError:
        encoded_args = str(args or {})
    return name, encoded_args


def _tool_call_has_result_evidence(call: Mapping[str, Any]) -> bool:
    """Return whether a tool-call row carries auditable result evidence."""

    for key in ("result", "observation", "output", "response", "result_preview"):
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        return True
    return False


def _normalize_tool_call_row(call: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a tool-call row while preserving bounded result evidence."""

    row: dict[str, Any] = {}
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        row["call_id"] = call_id
    name = call.get("name") or call.get("tool")
    if name:
        row["name"] = str(name)
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    if args is not None:
        row["args"] = args
    for key in ("ok", "duration_ms", "cached", "error", "telemetry_source"):
        if key in call:
            row[key] = call[key]
    for key in ("result", "observation", "output", "response", "result_preview"):
        if key not in call:
            continue
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "result":
            row["result"] = _bounded_tool_call_result(value)
        else:
            row[key] = _bounded_tool_call_result(value)
        break
    if row and "telemetry_source" not in row:
        row["telemetry_source"] = "posthoc_prediction"
    return row


def _merge_tool_call_rows(
    primary_rows: list[dict[str, Any]],
    supplemental_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge tool-call telemetry without dropping richer result evidence."""

    merged: list[dict[str, Any]] = [_normalize_tool_call_row(row) for row in primary_rows if row]
    by_key: dict[tuple[str, str], list[int]] = {}
    by_name_args: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(merged):
        by_key.setdefault(_tool_call_event_key(row), []).append(index)
        by_name_args.setdefault(_tool_call_name_args_key(row), []).append(index)

    for raw_supplemental in supplemental_rows:
        supplemental = _normalize_tool_call_row(raw_supplemental)
        if not supplemental:
            continue
        key = _tool_call_event_key(supplemental)
        candidate_index: int | None = None
        supplemental_has_result = _tool_call_has_result_evidence(supplemental)
        supplemental_ok = supplemental.get("ok")
        candidate_indexes = list(by_key.get(key, []))
        if not candidate_indexes:
            fallback_indexes = by_name_args.get(_tool_call_name_args_key(supplemental), [])
            if supplemental_has_result:
                fallback_indexes = [
                    index for index in fallback_indexes if merged[index].get("ok") is not False
                ]
            if fallback_indexes:
                candidate_indexes = fallback_indexes
        for index in candidate_indexes:
            existing = merged[index]
            existing_ok = existing.get("ok")
            if key[0] == "__call_id__":
                candidate_index = index
                break
            if supplemental_has_result and existing_ok is False and supplemental_ok is not False:
                continue
            if supplemental_has_result and not _tool_call_has_result_evidence(existing):
                candidate_index = index
                break
            if not supplemental_has_result:
                candidate_index = index
                break
        if candidate_index is None:
            by_key.setdefault(key, []).append(len(merged))
            by_name_args.setdefault(_tool_call_name_args_key(supplemental), []).append(len(merged))
            merged.append(supplemental)
            continue

        existing = merged[candidate_index]
        old_key = _tool_call_event_key(existing)
        for field_name, value in supplemental.items():
            if field_name in {"result", "observation", "output", "response", "result_preview"}:
                if not _tool_call_has_result_evidence(existing):
                    existing[field_name] = value
                continue
            if value in (None, "", [], {}):
                continue
            if field_name not in existing or existing[field_name] in (None, "", [], {}):
                existing[field_name] = value
            elif field_name in {"duration_ms", "cached", "telemetry_source", "ok", "error"}:
                existing[field_name] = value
        new_key = _tool_call_event_key(existing)
        if new_key != old_key and candidate_index not in by_key.get(new_key, []):
            by_key.setdefault(new_key, []).append(candidate_index)
    return merged


def _tool_calls_from_handoff_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return nested child tool-call evidence from delegation rows."""

    tool_rows: list[dict[str, Any]] = []

    def visit(row: Any) -> None:
        if not isinstance(row, Mapping):
            return
        for call in row.get("tools_called") or []:
            if isinstance(call, Mapping):
                tool_rows.append(_normalize_tool_call_row(call))
        for child in row.get("children") or []:
            visit(child)

    for row in rows:
        visit(row)
    return tool_rows


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

    The ledger stores every streamed thought/answer part VERBATIM (#881): the
    server no longer binds a visible-text prose cleaner here — model prose flows
    to the wire byte-for-byte and the DSPy contract markers are split off at the
    root (the #877 line-start detector), not scrubbed out of the transcript.
    """

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
        # (message.created published exactly once, whichever producer arrives
        # first); mirrored into the legacy dicts for untouched readers.
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
        transcript.append_part(part)
        _mirror_transcript_state(app, sid, transcript)
        return

    msg_id = _ensure_live_assistant_message(app, sid)
    live_parts = getattr(app.state, "live_assistant_parts", None)
    if live_parts is None:
        live_parts = {}
        app.state.live_assistant_parts = live_parts
    live_parts.setdefault(sid, []).append(part)
    app.state.bus.publish(
        Event(
            type="message.part.added",
            session_id=sid,
            payload={
                "turn_id": _active_semantic_turn_id(),
                "message_id": msg_id,
                # Real runtime parts (tool calls/results, routing) emitted live during
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
        # #767 PR1: the idempotency key is turn-scoped ledger state. A
        # duplicate key never closes streamed text — the boundary close runs
        # inside append_part only when the key is fresh.
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


def _emit_live_tool_route_context(app: "FastAPI", sid: str, tool_name: str) -> None:
    """Emit route/handoff context immediately before a live tool call."""

    public_agent, owner = _agent_tool_owner(app, tool_name)
    if not public_agent or public_agent in {"chat", "none"}:
        return
    _append_live_assistant_part_once(
        app,
        sid,
        f"route:{public_agent}",
        Part(
            id=f"live_route_{public_agent}",
            type="routing_decision",
            # The routing decision is MADE by the orchestrator; ``selected_agent``
            # below is the CHOSEN expert.
            agent_id="main",
            selected_agent=public_agent,
            rationale=f"Agent planner selected {public_agent} for tool {tool_name}.",
            confidence=0.0,
            heuristic=False,
            metadata={
                "route_source": "live_tool_observer",
                "route_reason": f"Resolved from live tool owner {owner}.",
                "stream_source": "live",
            },
            execution_path=f"orchestrator -> {public_agent}",
        ),
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

    Publishes tool.call.started / tool.call.completed events into
    the EventBus, attaching to the active turn session when present
    and falling back to recency only for out-of-band calls. Also
    appends each completed call into ``app.state.tool_call_ledger[sid]`` so the
    turn handler can attach a per-turn ``tools_called`` list to the
    assistant message metadata even when the underlying expert
    didn't populate ``pred.tools_called`` itself (e.g. the
    deterministic short-circuit paths).
    """

    def observe(
        name: str,
        args: Mapping[str, Any],
        phase: Optional[str],
        error: Optional[str],
        result: Any | None = None,
    ) -> None:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return
        # The expert that OWNS (runs) this tool, for per-part attribution. Empty
        # when CLIO can't resolve a routed owner (e.g. an orchestrator-level tool).
        _public_agent, tool_owner = _agent_tool_owner(app, name)
        # Attribute the tool_call/tool_result to the expert that INVOKED the tool
        # (the active ReAct scope, e.g. ``geospatial``), NOT the tool's owning
        # server/group (``geo``/``ndp``) — #732. Falls back to the owner when no
        # react scope is active (an orchestrator-level / chat-path tool call).
        invoking_expert = _ctx.active_react_scope() or tool_owner
        if phase == "started":
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            # Stash the per-thread call_id so the completion event
            # uses the same id. Threading-locals works for
            # MCPToolBridge's worker thread.
            _OBSERVER_CALL_IDS.value = call_id
            # Stamp the start time so completion can compute duration.
            _OBSERVER_CALL_T0.value = time.time()
            # B5 #979.7 (deferred B4 WRITER): join call_id → confined FLEET child (no-op on the
            # floor / built-in namespaces → the egress mint abstains). See ingest_edges.
            join_call_to_serving_child(app, sid, name, call_id)
            _emit_live_tool_route_context(app, sid, name)
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
                payload={
                    "call_id": call_id,
                    "tool": name,
                    "args": dict(args),
                    "telemetry_source": "live_observer",
                },
            )
            app.state.bus.publish(
                Event(
                    type="tool.call.started",
                    session_id=sid,
                    payload={
                        "call_id": call_id,
                        "tool": name,
                        "args": dict(args),
                        "telemetry_source": "live_observer",
                    },
                )
            )
            step_thought = _ctx.active_step_thought()
            # #732/#883: next_thought owns its OWN streamed text row; the copy on
            # tool_call.thought is redundant. Clear it IFF THIS step's next_thought
            # tap slice SURVIVES cleaning as a visible row — a per-step, in-thread,
            # format-only predicate (never a prose compare). A marker-only slice that
            # cleans to empty, or no slice at all (SDK gap), KEEPS the thought so it
            # never vanishes. Every outcome emits a structured reason (no silent
            # fallback). See tests/test_gact/test_next_thought_single_owner.py.
            transcript = _session_turn_transcript(app, sid)
            # #953: read the RUN-KEYED tap bucket (bare invoking_expert still owns attribution).
            _tap_scope = _ctx.run_keyed_scope(invoking_expert)
            had_stream, survived = (
                transcript.tap_step_survives_clean(_tap_scope, "next_thought")
                if transcript is not None
                else (False, False)
            )
            decision = classify_live_thought(had_stream, survived)
            if step_thought:
                stream_audit(
                    TOOL_THOUGHT_STAGE,
                    agent_id=invoking_expert,
                    field="next_thought",
                    visible=False,
                    duplicate_suppressed=decision.clear,
                    duplicate_reason=decision.reason,
                    step_id=_ctx.active_parent_span_id(),
                    head=step_thought[:120],
                )
            if decision.clear or not step_thought:
                step_thought = ""
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_call",
                    type="tool_call",
                    agent_id=invoking_expert,
                    call_id=call_id,
                    tool_name=name,
                    # The step's reasoning rides the tool_call part (#732): the
                    # model's text and the action it chose are one ordered event.
                    thought=step_thought,
                    input=dict(args),
                    metadata={"stream_source": "live", "telemetry_source": "live_observer"},
                ),
            )
        elif phase == "completed":
            call_id = getattr(_OBSERVER_CALL_IDS, "value", "") or ""
            t0 = getattr(_OBSERVER_CALL_T0, "value", None)
            duration_ms = (time.time() - t0) * 1000 if t0 else 0.0
            cancel_event = app.state.cancel_events.get(sid)
            completed_after_cancel = sid in app.state.cancel_flags or (
                cancel_event is not None and cancel_event.is_set()
            )
            completion_error = error
            cancellation_metadata: dict[str, Any] = {}
            if completed_after_cancel:
                completion_error = (
                    completion_error or "tool call completed after session cancellation"
                )
                cancellation_metadata = {
                    "execution_cancellation": "best_effort",
                    "executor_work_may_continue": True,
                }
            ok = completion_error is None
            structured_content = (
                result.get("structuredContent")
                if isinstance(result, Mapping) and "structuredContent" in result
                else None
            )
            result_summary = f"Tool {name} {'completed' if ok else 'failed'}."
            # Served payload = the tool-response atom's FACTS (ok/duration/cached/result/
            # error). No ui_summary/result_summary captions — clio transmits, it does not
            # author UI labels; the envelope ``summary`` below is the one short caption.
            payload = {
                "call_id": call_id,
                "tool": name,
                "ok": ok,
                "duration_ms": duration_ms,
                "cached": False,
                "telemetry_source": "live_observer",
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
                        **({"error": completion_error} if completion_error else {}),
                        **(
                            {"result": _bounded_tool_call_result(result)}
                            if result is not None
                            else {}
                        ),
                        **cancellation_metadata,
                    }
                )
            # Canonical trace captures the FULL tool result (never capped) -- the
            # bounded projection in `payload` is only for the wire bus event +
            # ledger/assistant-metadata. (SSE still redacts `result` via
            # SENSITIVE_KEYS; only the durable trace keeps the full value.)
            trace_payload = {**payload, "result": result} if result is not None else payload
            _emit_semantic_event(
                app,
                sid,
                "tool.call.completed",
                turn_id=_ctx.active_turn_id(),
                trace_id=_ctx.active_trace_id(),
                status="completed" if ok else "failed",
                summary=result_summary,
                actor={"tool": name},
                subject={"call_id": call_id},
                payload=trace_payload,
            )
            app.state.bus.publish(
                Event(
                    type="tool.call.completed",
                    session_id=sid,
                    payload=payload,
                )
            )
            # Seam #966 S1+S5 (#971): mint generated versions + record the coarse
            # TransformRecord (success AND failure — a failed write is provenance).
            if not completed_after_cancel:
                from clio_agent.gact.artifacts.transforms import (  # noqa: PLC0415
                    observe_tool_transform,
                )

                observe_tool_transform(app, sid, name, dict(args), call_id, ok, result)
            _OBSERVER_CALL_T0.value = (
                None  # finding [3]: clear the latch (idle thread -> DIRTY lease)
            )
            result_text = completion_error or (
                _tool_result_preview(result) if result is not None else "completed"
            )
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
                    content=[
                        Part(
                            id=f"live_{call_id}_result_text",
                            type="text",
                            agent_id=invoking_expert,
                            text=result_text,
                        )
                    ],
                    metadata={
                        "stream_source": "live",
                        "telemetry_source": "live_observer",
                        **(
                            {"structured_content": structured_content}
                            if structured_content is not None
                            else {}
                        ),
                        **(
                            {"result": _bounded_tool_call_result(result)}
                            if result is not None
                            else {}
                        ),
                        **cancellation_metadata,
                    },
                ),
            )

    return observe


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
