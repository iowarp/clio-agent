"""SSE event publication for durable MCP task records (#1205).

Mirrors ``agent_tasks.py``'s ``AGENT_TASK_EVENTS`` / ``publish_agent_task_event`` for
the sibling :class:`~clio_agent.tools.mcp_task_records.TaskRecord` store (#1115):
every mutation :class:`~clio_agent.gact.mcp_task_store.SessionMetadataTaskStore`
persists calls back through
:func:`clio_agent.tools.mcp_task_records.task_change_listener`, installed here at
boot to bridge onto the SAME per-session event bus agent-task lifecycle events
already use. No parallel envelope, no new SSE route: the event lands on the owning
CLIO session's channel, which ``GET /v1/sessions/{sid}/events`` already streams
(RULE 6 / the cleanup program's "reuse the established event envelope" directive).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from clio_agent.tools.mcp_task_records import TaskRecord, set_task_change_listener

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = [
    "MCP_TASK_EVENT_DEFAULT",
    "MCP_TASK_EVENTS",
    "install_mcp_task_event_publisher",
    "publish_mcp_task_event",
]

# One event name per terminal edge, mirroring ``agent_tasks.AGENT_TASK_EVENTS``.
# Every OTHER SEP-2663 status (the non-terminal "working", "input_required", a
# future upstream addition, ...) maps to the single default below so a new status
# can never go unpublished waiting on a catalog update.
MCP_TASK_EVENTS: dict[str, str] = {
    "completed": "mcp_task.completed",
    "failed": "mcp_task.failed",
    "cancelled": "mcp_task.cancelled",
}
MCP_TASK_EVENT_DEFAULT = "mcp_task.updated"


def publish_mcp_task_event(app: "FastAPI", record: TaskRecord) -> None:
    """Publish one ``mcp_task.*`` event to the owning CLIO session's channel.

    A record with no resolvable CLIO session (held process-locally, unattributed,
    or its session row was deleted out from under it) has no session channel to
    publish to. That is already a typed degrade the store itself reports
    (``mcp_task_record_held_locally`` / ``mcp_task_session_deleted``); this
    function does not re-report it, it simply has nothing to publish.
    """

    session_id = record.session_id
    if not session_id:
        return
    from clio_agent.gact.events import Event  # noqa: PLC0415 - avoid import cycle

    event_type = MCP_TASK_EVENTS.get(record.status, MCP_TASK_EVENT_DEFAULT)
    app.state.bus.publish(Event(type=event_type, session_id=session_id, payload=record.to_wire()))


def install_mcp_task_event_publisher(app: "FastAPI") -> None:
    """Wire durable MCP-task mutations to this app's event bus (boot entry point).

    Installs a closure over ``app`` as the process-global change listener
    (:func:`clio_agent.tools.mcp_task_records.set_task_change_listener`), called by
    :class:`~clio_agent.gact.mcp_task_store.SessionMetadataTaskStore` on every
    ``put``. One listener slot exists per process, matching the single-app-per-
    process shape every other ``install_*`` boot hook (``install_agent_task_registry``,
    ``install_session_task_store``) already assumes.
    """

    set_task_change_listener(lambda record: publish_mcp_task_event(app, record))
