"""One view, one app: opening a view that is already open reuses its app.

A server that declares how its view reopens (``ai.iowarp.clio/reopen``) names the view
by those arguments, and a view service (``ai.iowarp.clio/service``) says where it is
drawn. A later call of the same tool on the same server with the same reopen arguments
and the same service, while that app lives in the session, opens nothing new: the live
app takes the new result, no second card or tab is made (each would hold another frame
of the same view), and the UI is told to show the app it already has
(:data:`APP_SHOWN_EVENT`). A person's reopen from a card is never folded in: it mounts
its own app (MA19).
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.mcp_apps import MCPAppRecord

#: The session event of a reused app: ``{session_id, app_instance_id}``.
APP_SHOWN_EVENT = "mcp_app_shown"


def _served(record: MCPAppRecord) -> bool:
    service = record.service
    return service is not None and service.refusal is None and bool(service.token)


def same_view(records: Iterable[MCPAppRecord], candidate: MCPAppRecord) -> MCPAppRecord | None:
    """The live app ``candidate`` would duplicate, or ``None``."""

    if not candidate.reopen_arguments or not _served(candidate):
        return None
    assert candidate.service is not None
    for record in reversed(list(records)):
        if (
            record.session_id == candidate.session_id
            and record.source_namespace == candidate.source_namespace
            and record.tool_name == candidate.tool_name
            and record.resource_uri == candidate.resource_uri
            and record.reopen_arguments == candidate.reopen_arguments
            and _served(record)
            and record.service is not None
            and record.service.port == candidate.service.port
            and secrets.compare_digest(record.service.token, candidate.service.token)
        ):
            return record
    return None


def adopt(record: MCPAppRecord, candidate: MCPAppRecord) -> None:
    """The live app takes the newer call's input and result."""

    record.tool_input = candidate.tool_input
    record.tool_result = candidate.tool_result
    record.last_access = time.monotonic()


def publish_shown(app: Any, record: MCPAppRecord) -> None:
    """Tell ``record``'s session that a call showed its app again (transient event)."""
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus = getattr(getattr(app, "state", None), "bus", None)
    if bus is None:
        return
    bus.publish(
        Event(
            type=APP_SHOWN_EVENT,
            session_id=record.session_id,
            payload={"session_id": record.session_id, "app_instance_id": record.app_instance_id},
            transient=True,
        )
    )


__all__ = ["APP_SHOWN_EVENT", "adopt", "publish_shown", "same_view"]
