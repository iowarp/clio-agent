"""An open app view keeps its server up; a view whose server stopped is not live.

A view is drawn by the MCP server whose tool opened it, and a view with a live service
(MA23) talks to a port that server opened. That server lives in its conversation's
workspace fleet, which the workspace reaper (:mod:`clio_agent.tools.reaper`) closed
after ``tools.mcp.workspace_ttl_s`` without a tool call or a turn: a view pinned in a
tab or a surface then pointed at a stopped server, and showing it mounted a dead
service (seen live: ``service_unreachable`` and the proxy's error under a "Ready"
header). Plan rule 6: no timer or expiry on an open view.

* :func:`held_roots` is a reaper hold provider: every record the app registry holds
  (an inline card while the registry keeps it, a tab, a surface) keeps its session's
  fleet resident. The registry's own rules decide how long an inline card lives; a
  pinned one lives until it is closed.
* :func:`admitted` stamps a new record with the fleet whose server opened it (a weak
  reference) and registers the hold provider once.
* :func:`server_stopped` tells the registry a record's fleet was closed anyway (a
  configuration change, a write-root grant, the LRU cap with no hold): the registry
  drops it with the typed reason :data:`MCP_APP_SERVER_STOPPED`, so the card and the
  tab answer "not live" and offer Reopen, which replays the opening call (MA19) and
  starts the server again, instead of mounting a dead service.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

MCP_APP_SERVER_STOPPED = "mcp_app_server_stopped"
MCP_APP_FLEET_UNHELD = "mcp_app_fleet_unheld"

__all__ = [
    "MCP_APP_FLEET_UNHELD",
    "MCP_APP_SERVER_STOPPED",
    "admitted",
    "held_roots",
    "server_stopped",
]


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


def held_roots(app: Any) -> set[str]:
    """The workspace roots whose fleets serve a view the registry holds."""
    from clio_agent.gact.mcp_apps import _registry  # noqa: PLC0415

    registry = _registry(app)
    roots: set[str] = set()
    for sid in registry.session_ids():
        root = _canonical_root(app, sid)
        if root:
            roots.add(root)
    return roots


def _ensure_hold_provider(app: Any) -> None:
    state = getattr(app, "state", None)
    if state is None or getattr(state, "mcp_app_fleet_hold", False):
        return
    reaper = getattr(getattr(state, "agent", None), "_workspace_reaper", None)
    add = getattr(reaper, "add_hold_provider", None)
    if not callable(add):
        trace.event("TOOLS", "%s reason=no_reaper", MCP_APP_FLEET_UNHELD)
        return
    add(lambda: held_roots(app))
    state.mcp_app_fleet_hold = True


def admitted(app: Any, record: Any) -> None:
    """Stamp ``record`` with the fleet that opened it; keep open views' fleets resident."""
    _ensure_hold_provider(app)
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
