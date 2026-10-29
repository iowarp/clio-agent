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
