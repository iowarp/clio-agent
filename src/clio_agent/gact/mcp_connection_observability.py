"""Per-session semantic-event surfacing for MCP connection-era downgrades (#1201).

``tools/mcp_connection_era.py`` cannot emit gact semantic events -- it is a
tools-layer module and imports no gact (RULE 4 topology). This is the
gact-side reader: called from the two natural per-session "an executor was
just resolved for real use" sites -- ``gact/agents/builders.py::
_active_base_agent_tool_executor`` and ``gact/mcp_apps.py::_bound_executor``
-- both of which already have ``sid`` in scope. Emits ONE
``mcp.connection.downgraded`` semantic event per ``(session, server)`` pair
the first time a downgrade is observed for it, so the degradation reaches the
durable per-session trace instead of staying visible only in the tools-layer
bounded ring / the doctor row.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

#: (session_id, server_id) pairs already surfaced -- never re-emit the same
#: downgrade into the same session's trace on every subsequent tool call.
_EMITTED: set[tuple[str, str]] = set()
_EMITTED_LOCK = threading.Lock()


def emit_downgrade_events_for_executor(app: "FastAPI", sid: str, executor: Any) -> None:
    """Emit a semantic event for each of ``executor``'s namespaces currently
    showing a recorded era downgrade, once per ``(session, server_id)``.

    A no-op when ``sid`` is empty, ``executor`` is ``None``, ``executor`` does
    not expose ``namespaces()`` (a bare test double), or no namespace has ever
    recorded a downgrade.
    """

    if not sid or executor is None:
        return
    namespaces = getattr(executor, "namespaces", None)
    if not callable(namespaces):
        return

    from clio_agent.tools.mcp_connection_era import latest_mcp_connection_era  # noqa: PLC0415

    for server_id in namespaces():
        era = latest_mcp_connection_era(server_id)
        if era is None or era.degrade_reason is None:
            continue
        key = (sid, server_id)
        with _EMITTED_LOCK:
            if key in _EMITTED:
                continue
            _EMITTED.add(key)

        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            "mcp.connection.downgraded",
            status="completed",
            summary=(
                f"MCP server {server_id!r} negotiated the legacy protocol era under auto mode."
            ),
            actor={"server_id": server_id},
            subject={"server_id": server_id},
            payload={
                "server_id": server_id,
                "reason": era.degrade_reason,
                "protocol_version": era.protocol_version,
                "connect_mode": era.connect_mode,
            },
        )


__all__ = ["emit_downgrade_events_for_executor"]
