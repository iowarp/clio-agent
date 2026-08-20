"""SSE event publication for durable MCP task records (#1205, #1236).

Mirrors ``agent_tasks.py``'s ``AGENT_TASK_EVENTS`` / ``publish_agent_task_event`` for
the sibling :class:`~clio_agent.tools.mcp_task_records.TaskRecord` store (#1115):
every mutation :class:`~clio_agent.gact.mcp_task_store.SessionMetadataTaskStore`
persists calls back through
:func:`clio_agent.tools.mcp_task_records.task_change_listener`, installed here at
boot to bridge onto the SAME per-session event bus agent-task lifecycle events
already use. No parallel envelope, no new SSE route: the event lands on the owning
CLIO session's channel, which ``GET /v1/sessions/{sid}/events`` already streams
(RULE 6 / the cleanup program's "reuse the established event envelope" directive).

#1236 adds a SECOND, LEAN event type (``mcp_task.console``) fed by the separate
:func:`~clio_agent.tools.mcp_task_records.task_console_listener` hook a backend's
``on_poll`` observer fires when it folds NEW console bytes in (e.g.
``tools/relay_console.py::make_console_on_poll``). It exists alongside, not
instead of, the full-record ``mcp_task.*`` events below: those still carry the
whole rolling tail on every mutation (reload/catch-up honesty), but repeating
that WHOLE tail as the live signal for "console grew a little" bloats the
stream and risks crowding other events out of the bounded per-session
history/queue (``gact/events.py::EventBus``, capacity 256). The console event
carries only the delta.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from clio_agent.tools.mcp_task_records import (
    TaskKey,
    TaskRecord,
    set_task_change_listener,
    set_task_console_listener,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = [
    "MCP_TASK_CONSOLE_EVENT",
    "MCP_TASK_EVENT_DEFAULT",
    "MCP_TASK_EVENTS",
    "install_mcp_task_event_publisher",
    "publish_mcp_task_console_delta",
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

#: The #1236 lean console-delta event type. Deliberately outside MCP_TASK_EVENTS
#: (that catalog keys purely on TERMINAL status; this one is not status-keyed at
#: all -- it fires on every growing poll of a still-``working`` task).
MCP_TASK_CONSOLE_EVENT = "mcp_task.console"


def publish_mcp_task_event(app: "FastAPI", record: TaskRecord) -> None:
    """Publish one ``mcp_task.*`` event to the owning CLIO session's channel.

    A record with no resolvable CLIO session (held process-locally, unattributed,
    or its session row was deleted out from under it) has no session channel to
    publish to. That is already a typed degrade the store itself reports
    (``mcp_task_record_held_locally`` / ``mcp_task_session_deleted``); this
    function does not re-report it, it simply has nothing to publish.

    The event TYPE is chosen from :attr:`TaskRecord.display_status` (#1236),
    not the raw ``status`` -- a terminal task whose delivered result carried
    ``isError: true`` derives to ``effective_status="failed"``, and the event a
    live subscriber sees must say ``mcp_task.failed``, never
    ``mcp_task.completed``, to match (clio-relay#265's "completed is a terrible
    status indicator" ruling made concrete on the wire).
    """

    session_id = record.session_id
    if not session_id:
        return
    from clio_agent.gact.events import Event  # noqa: PLC0415 - avoid import cycle

    event_type = MCP_TASK_EVENTS.get(record.display_status, MCP_TASK_EVENT_DEFAULT)
    app.state.bus.publish(Event(type=event_type, session_id=session_id, payload=record.to_wire()))


def publish_mcp_task_console_delta(
    app: "FastAPI",
    key: TaskKey,
    *,
    channel: str,
    delta: str,
    offset: int,
    truncated: bool,
) -> None:
    """Publish one LEAN ``mcp_task.console`` delta event (#1236).

    Carries only the NEW bytes (``delta``), never the whole rolling tail --
    the record (``TaskRecord.backend["console"]["tail"]``, reachable via a
    reload or the next ``mcp_task.*`` snapshot event) stays the source of
    truth for the full tail. ``channel`` names the stream the delta came from
    (``"console"`` today; deliberately not hardcoded to ``"stdout"`` so a
    future relay stderr tail slots in without a shape change). A key with no
    resolvable CLIO session has nothing to publish to, mirroring
    :func:`publish_mcp_task_event`.
    """

    session_id = key.session_id
    if not session_id:
        return
    from clio_agent.gact.events import Event  # noqa: PLC0415 - avoid import cycle

    app.state.bus.publish(
        Event(
            type=MCP_TASK_CONSOLE_EVENT,
            session_id=session_id,
            payload={
                "key": key.to_wire(),
                "channel": channel,
                "delta": delta,
                "offset": offset,
                "truncated": truncated,
            },
        )
    )


def install_mcp_task_event_publisher(app: "FastAPI") -> None:
    """Wire durable MCP-task mutations to this app's event bus (boot entry point).

    Installs closures over ``app`` as the process-global change listener
    (:func:`clio_agent.tools.mcp_task_records.set_task_change_listener`, called by
    :class:`~clio_agent.gact.mcp_task_store.SessionMetadataTaskStore` on every
    ``put``) AND the console-delta listener
    (:func:`clio_agent.tools.mcp_task_records.set_task_console_listener`, called
    by a backend's ``on_poll`` observer when it folds new console bytes in,
    #1236). One listener slot of EACH exists per process, matching the single-
    app-per-process shape every other ``install_*`` boot hook
    (``install_agent_task_registry``, ``install_session_task_store``) already
    assumes.
    """

    set_task_change_listener(lambda record: publish_mcp_task_event(app, record))
    set_task_console_listener(
        lambda key, channel, delta, offset, truncated: publish_mcp_task_console_delta(
            app, key, channel=channel, delta=delta, offset=offset, truncated=truncated
        )
    )
