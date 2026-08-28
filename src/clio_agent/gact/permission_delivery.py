"""Deliver permission lifecycle events to their owner and attended session.

Child sessions own their permission rows, but a human commonly watches the
root session that spawned them. This module preserves that ownership while
mirroring request and resolution boundaries onto the root session stream.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from clio_agent.gact.events import Event

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
