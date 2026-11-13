"""A view whose server stopped is not live: its card offers Reopen (MA19).

A view is drawn by the MCP server whose tool opened it, and a view with a live service
(MA23) talks to a port that server opened. That server lives in its conversation's
workspace fleet. A conversation someone views keeps its fleet (``view_fleet``); once
nobody has viewed it for ``tools.mcp.workspace_ttl_s`` the reaper
(:mod:`clio_agent.tools.reaper`) closes the fleet, and a configuration change, a
write-root grant or the LRU cap can close it too. A pinned view then pointed at a
stopped server, and showing it mounted a dead service (seen live:
``service_unreachable`` and the proxy's error under a "Ready" header).

* :func:`admitted` stamps a new record with the fleet whose server opened it (a weak
  reference to the executor).
* :func:`server_stopped` tells the registry that fleet was closed: the registry drops
  the record with the typed reason :data:`MCP_APP_SERVER_STOPPED` and tells the
  session's UI (``mcp_app_closed``), so the card and the tab show the app ended and
  offer Reopen, which replays the opening call and starts the server again.

The fleet is not kept alive for an open view the person cannot see: the reap happens
only when no one views the conversation, so no open view is lost (plan rule 6).
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

MCP_APP_SERVER_STOPPED = "mcp_app_server_stopped"

__all__ = ["MCP_APP_SERVER_STOPPED", "admitted", "server_stopped"]


def _canonical_root(app: Any, sid: str) -> str:
    from clio_agent.gact.view_fleet import _fleet_root  # noqa: PLC0415
    from clio_agent.tools.workspace_root import canonical_workspace_root  # noqa: PLC0415

    root = _fleet_root(app, sid)
    return canonical_workspace_root(root) if root else ""


def _current_fleet(app: Any, sid: str) -> Any:
    """The resident executor serving ``sid`` now, without building one."""
    agent = getattr(getattr(app, "state", None), "agent", None)
    if agent is None:
        return None
    root = _canonical_root(app, sid)
    state = getattr(agent, "_workspace_state", None)
    if root and callable(state):
        lock, executors, _leases = state()
        with lock:
            return executors.get(root)
    return getattr(agent, "tool_executor", None)


def admitted(app: Any, record: Any) -> None:
    """Stamp ``record`` with the fleet whose server opened it."""
    try:
        fleet = _current_fleet(app, record.session_id)
        record.fleet = weakref.ref(fleet) if fleet is not None else None
    except TypeError:  # an executor that cannot be weakly referenced: liveness unknown
        record.fleet = None


def server_stopped(record: Any) -> bool:
    """Whether the fleet whose server opened ``record`` has been closed."""
    ref = getattr(record, "fleet", None)
    if ref is None:
        return False  # not stamped: nothing known, never dropped on a guess
    fleet = ref()
    return fleet is None or bool(getattr(fleet, "closed", False))
