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

import threading
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.delegation import _expert_handoff_summary
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
from clio_agent.gact.types import Message, Part

if TYPE_CHECKING:
    from fastapi import FastAPI

# Per-thread call_id + start-time stash so the ``completed`` phase reuses the
# same id and can compute duration. MCPToolBridge invokes the observer on a
# worker thread, so threading-locals (not contextvars) are the right scope.
_OBSERVER_CALL_IDS = threading.local()
_OBSERVER_CALL_T0 = threading.local()


def _install_tool_runtime_hooks(app: "FastAPI") -> None:
    """Install permission, cancellation, and telemetry hooks for tool calls."""

    from clio_agent.tools.execution import (  # noqa: PLC0415
        set_global_cancellation_checker,
        set_global_permission_gate,
        set_global_tool_interceptor,
        set_global_tool_observer,
    )

    checker = getattr(app.state, "pending_cancellation_checker", None)
    if checker is None:
        checker = _make_cancellation_checker(app)
    gate = getattr(app.state, "pending_permission_gate", None)
    if gate is None:
        gate = _make_permission_gate(app)
    observer = getattr(app.state, "pending_tool_observer", None)
    if observer is None:
        observer = _make_tool_observer(app)
    interceptor = getattr(app.state, "pending_tool_interceptor", None)
    set_global_cancellation_checker(checker)
    set_global_permission_gate(gate)
    set_global_tool_interceptor(interceptor)
    set_global_tool_observer(observer)
    app.state.pending_cancellation_checker = checker
    app.state.pending_permission_gate = gate
    app.state.pending_tool_interceptor = interceptor
    app.state.pending_tool_observer = observer
    app.state.tool_hooks_installed = True


def _ensure_live_assistant_message(app: "FastAPI", sid: str) -> str:
    """Return the in-flight assistant message id, creating it if needed."""

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
            ).model_dump(exclude_none=True),
        )
    )
    return msg_id


def _append_live_assistant_part(app: "FastAPI", sid: str, part: Part) -> None:
    """Publish and remember a real runtime part for the active assistant turn."""

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
                "part": part.model_dump(exclude_none=True),
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
            "output_summary": f"Preparing {tool_name}.",
        }
        _append_live_assistant_part_once(
            app,
            sid,
            f"handoff:{public_agent}:{owner}",
            Part(
                id=f"live_handoff_{public_agent}_{owner}",
                type="expert_handoff",
                text=_expert_handoff_summary(row),
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
        if phase == "started":
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            # Stash the per-thread call_id so the completion event
            # uses the same id. Threading-locals works for
            # MCPToolBridge's worker thread.
            _OBSERVER_CALL_IDS.value = call_id
            # Stamp the start time so completion can compute duration.
            _OBSERVER_CALL_T0.value = time.time()
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
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_call",
                    type="tool_call",
                    call_id=call_id,
                    tool_name=name,
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
            result_summary = f"Tool {name} {'completed' if ok else 'failed'}."
            payload = {
                "call_id": call_id,
                "tool": name,
                "ok": ok,
                "duration_ms": duration_ms,
                "cached": False,
                "telemetry_source": "live_observer",
                "ui_summary": result_summary,
                "result_summary": result_summary,
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
            result_text = completion_error or (
                _tool_result_preview(result) if result is not None else "completed"
            )
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_result",
                    type="tool_result",
                    call_id=call_id,
                    tool_name=name,
                    is_error=not ok,
                    duration_ms=duration_ms,
                    cached=False,
                    content=[
                        Part(
                            id=f"live_{call_id}_result_text",
                            type="text",
                            text=result_text,
                        )
                    ],
                    metadata={
                        "stream_source": "live",
                        "telemetry_source": "live_observer",
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
