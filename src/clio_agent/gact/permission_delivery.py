"""Deliver permission lifecycle events to their owner and attended session.

Child sessions own their permission rows, but a human commonly watches the
root session that spawned them. This module preserves that ownership while
mirroring request and resolution boundaries onto the root session stream.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact.events import Event
from clio_agent.gact.permission_evidence import emit_permission_resolution, permission_identity
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.session_kind import session_kind

if TYPE_CHECKING:
    from fastapi import FastAPI

PermissionEventType = Literal["permission.requested", "permission.resolved"]


def tool_title(name: str) -> str:
    """The tool's own title (an MCP tool's ``title``), empty when it declares none."""

    from clio_agent.gact.agents.tool_instrumentation import declared_tool_title  # noqa: PLC0415

    return declared_tool_title(name)


def permission_summary(name: str, context: Mapping[str, Any] | None, subject: str) -> str:
    """The title a permission card shows for a call to ``name``.

    A native tool's own summary wins. An external MCP tool reads as its declared title
    ("Change the timestep"), so a person is not shown the namespaced identifier; a tool
    without one keeps the plain ``<subject> call: <name>`` line.
    """

    if context and context.get("kind") == "native" and context.get("summary"):
        return str(context["summary"])[:500]
    if subject == "external MCP tool" and (title := tool_title(name)):
        return title
    return f"{subject} call: {name}"


def attended_session_id(app: Any, session_id: str) -> str:
    """Return the top human-attended session for ``session_id``.

    The parent walk is cycle-guarded and degrades to the supplied session when
    no store or parent relationship is available.
    """

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return session_id
    seen: set[str] = set()
    current = session_id
    while current and current not in seen:
        seen.add(current)
        session = sessions.get(current)
        if session is not None and session_kind(session) == "branch":
            return current
        parent = str(getattr(session, "parent_session_id", "") or "") if session is not None else ""
        if not parent:
            return current
        current = parent
    return session_id


def publish_permission_event(
    app: Any,
    event_type: PermissionEventType,
    *,
    owner_session_id: str,
    payload: Mapping[str, Any],
) -> None:
    """Publish a permission event to its owner and root attended stream.

    A mirrored payload retains the child's ``session_id`` and adds delivery
    metadata, keeping ownership distinct from the stream the human watches.
    """

    bus = getattr(app.state, "bus", None)
    if bus is None:
        return
    attended = attended_session_id(app, owner_session_id)
    targets = (owner_session_id,) if attended == owner_session_id else (owner_session_id, attended)
    for target in targets:
        delivered = dict(payload)
        if target != owner_session_id:
            delivered["forwarded_from_session_id"] = owner_session_id
            delivered["attended_session_id"] = attended
        bus.publish(Event(type=event_type, session_id=target, payload=delivered))


def publish_waiting_call(app: Any, row: Mapping[str, Any], *, resumed: bool = False) -> None:
    """Show the call a pending permission holds as a waiting tool row.

    The row's id is the identity the call keeps when it runs or is refused, so the
    waiting row becomes the running, denied or finished one rather than a second row.
    ``resumed`` reports that a call which was already running (a script asking for its
    own approval) runs again after the person allowed it.
    """

    bus = getattr(app.state, "bus", None)
    tool_call = row.get("tool_call")
    tool_call = tool_call if isinstance(tool_call, Mapping) else {}
    call_id = str(tool_call.get("call_id") or "")
    if bus is None or not call_id:
        return
    if not resumed:
        # The reasoning that led to the call ends when the call is held: closing the open
        # part stops its clock, which otherwise ran on through the wait for a person and
        # showed it as thinking ("Thought for 13 minutes") once the call finally opened.
        registry = getattr(app.state, "turn_transcripts", None)
        transcript = registry.get(str(row.get("session_id") or "")) if registry else None
        if transcript is not None:
            transcript.close_open_text()
    from clio_agent.gact.tool_call_nesting import nested_call_fields  # noqa: PLC0415

    bus.publish(
        Event(
            type="tool.call.resumed" if resumed else "tool.call.waiting",
            session_id=str(row.get("session_id") or ""),
            payload={
                "call_id": call_id,
                "tool": str(tool_call.get("tool_name") or ""),
                **(
                    {"tool_title": title}
                    if (title := tool_title(str(tool_call.get("tool_name") or "")))
                    else {}
                ),
                "args": dict(tool_call.get("input") or {}),
                "permission_id": str(row.get("id") or ""),
                "requested_at": str(row.get("created_at") or ""),
                **nested_call_fields(),
            },
        )
    )


_TERMINAL_TOOL_STATES = frozenset({"succeeded", "failed", "denied", "cancelled"})


def overlay_waiting_calls(
    app: Any, session_id: str, tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Project calls held by this session's pending permissions as ``pending``.

    A call that has not started yet has no recorded row, so its entity comes from
    the permission request; a call already running (a script awaiting its own
    approval) reads as waiting until the person answers.
    """

    waiting: dict[str, Mapping[str, Any]] = {}
    for row in list((getattr(app.state, "permissions", None) or {}).values()):
        if row.get("session_id") != session_id or row.get("status") != "pending":
            continue
        tool_call = row.get("tool_call")
        call_id = str(tool_call.get("call_id") or "") if isinstance(tool_call, Mapping) else ""
        if call_id:
            waiting[call_id] = row
    if not waiting:
        return tools
    projected: list[dict[str, Any]] = []
    for tool in tools:
        tool_id = str(tool.get("id") or "")
        if tool_id in waiting and tool.get("state") not in _TERMINAL_TOOL_STATES:
            tool = {**tool, "state": "pending"}
            if not tool.get("title") and (title := tool_title(str(tool.get("name") or ""))):
                tool["title"] = title
        waiting.pop(tool_id, None)
        projected.append(tool)
    for call_id, row in waiting.items():
        tool_call = row["tool_call"]
        name = str(tool_call.get("tool_name") or "Tool")
        projected.append(
            {
                "id": call_id,
                "session_id": session_id,
                "name": name,
                **({"title": title} if (title := tool_title(name)) else {}),
                "state": "pending",
                "input": tool_call.get("input"),
                "permission_id": str(row.get("id") or ""),
            }
        )
    return projected


def record_resolved_permission(
    app: "FastAPI",
    *,
    session_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    status: str,
    action: str,
    summary: str,
    reason: str,
) -> str:
    """Record and deliver an already resolved tool permission decision."""
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "id": pid,
        "session_id": session_id,
        **permission_identity(app, session_id),
        "tool_call": {
            "tool_name": tool_name,
            "input": dict(args),
        },
        "summary": summary,
        "created_at": now_iso,
        "status": status,
        "action": action,
        "resolved_at": now_iso,
        "reason": reason,
    }
    if hasattr(app.state, "permissions"):
        app.state.permissions[pid] = row
        enforce_dict_bound(app, app.state.permissions, "permissions", session_id=session_id)
    emit_permission_resolution(app, row)
    if hasattr(app.state, "bus"):
        publish_permission_event(
            app,
            "permission.resolved",
            owner_session_id=session_id,
            payload={
                "permission_id": pid,
                "action": action,
                "session_id": session_id,
                "reason": reason,
            },
        )
    return pid
