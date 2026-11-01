"""``session.work.updated``: the working state on the rail changed (plan or todos).

The Work rail polls ``GET /v1/sessions/{sid}/work``; this event lets it refresh as soon
as the agent writes its plan or todos, instead of on the next poll.
"""

from __future__ import annotations

from typing import Any

__all__ = ["WORK_UPDATED", "publish_work_updated"]

WORK_UPDATED = "session.work.updated"


def publish_work_updated(app: Any, session_id: str, part: str) -> None:
    """Tell readers that ``part`` (``plan`` or ``todos``) of the session's work changed."""
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus = getattr(getattr(app, "state", None), "bus", None)
    if bus is not None:
        bus.publish(Event(type=WORK_UPDATED, session_id=session_id, payload={"part": part}))
