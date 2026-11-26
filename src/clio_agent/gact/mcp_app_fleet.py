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

__all__ = [
    "MCP_APP_SERVER_STOPPED",
    "admitted",
    "drawing_fleet",
    "server_stopped",
    "views_left",
    "watch_views",
]


def _canonical_root(app: Any, sid: str) -> str:
    """The key of the fleet serving ``sid``: its workspace root and, when the
    conversation has connector values of its own, their variant (CV-D3)."""
    from clio_agent.gact.connector_values import session_fleet_variant  # noqa: PLC0415
    from clio_agent.gact.view_fleet import _fleet_root  # noqa: PLC0415
    from clio_agent.tools.fleet_key import fleet_key  # noqa: PLC0415
    from clio_agent.tools.workspace_root import canonical_workspace_root  # noqa: PLC0415

    root = canonical_workspace_root(_fleet_root(app, sid))
    return fleet_key(root, session_fleet_variant(app, sid, root)) if root else ""


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


def _inner(fleet: Any) -> Any:
    return getattr(fleet, "_async_executor", fleet)


def admitted(app: Any, record: Any) -> None:
    """Stamp ``record`` with the fleet whose server opened it, and that server's start."""
    from clio_agent.tools.namespace_instances import generation  # noqa: PLC0415

    try:
        fleet = _current_fleet(app, record.session_id)
        record.fleet = weakref.ref(fleet) if fleet is not None else None
    except TypeError:  # an executor that cannot be weakly referenced: liveness unknown
        record.fleet = None
        return
    if fleet is not None:
        record.instance_generation = generation(
            fleet, getattr(record, "source_namespace", None) or ""
        )


def server_stopped(record: Any) -> bool:
    """Whether the server that opened ``record`` stopped: its fleet closed, or the fleet
    stopped that namespace's server since. MI1 keeps one server per connector per folder,
    except that a superseded one stays alive while views it drew are open."""
    from clio_agent.tools.namespace_instances import generation  # noqa: PLC0415

    ref = getattr(record, "fleet", None)
    if ref is None:
        return False  # not stamped: nothing known, never dropped on a guess
    fleet = ref()
    if fleet is None or bool(getattr(fleet, "closed", False)):
        return True
    stamped = getattr(record, "instance_generation", 0)
    return generation(fleet, getattr(record, "source_namespace", None) or "") != stamped


def watch_views(registry: Any) -> None:
    """Tell the tools layer which namespaces' servers draw open views (MI1).

    A route heal does not replace a server whose views are open; it is retried once
    they close. Registered once per registry, for its lifetime.
    """
    from clio_agent.tools import namespace_instances  # noqa: PLC0415

    ref = weakref.ref(registry)
    remove: list[Any] = []

    def drawn(executor: Any, namespace: str) -> list[Any] | None:
        current = ref()
        if current is None:
            for undo in remove:
                undo()
            return None
        with current._lock:
            records = (
                *current._records.values(),
                *current._cleanup_records.values(),
                *current._closing_records.values(),  # its cleanup call still needs the server
            )
        found = []
        for record in records:
            fleet = record.fleet() if record.fleet is not None else None
            if (
                fleet is not None
                and record.source_namespace == namespace
                and _inner(fleet) is executor
                and not server_stopped(record)
            ):
                found.append(record)
        return found

    def open_views(executor: Any, namespace: str) -> str:
        return "open_views" if drawn(executor, namespace) else ""

    def view_labels(executor: Any, namespace: str) -> list[str]:
        return [
            f"{record.tool_name} view {record.app_instance_id} (conversation {record.session_id})"
            for record in drawn(executor, namespace) or ()
        ]

    remove.append(namespace_instances.add_in_use_probe(open_views))
    remove.append(namespace_instances.add_view_namer(view_labels))


def views_left() -> None:
    """A view left the registry: stop superseded servers that no longer draw one."""
    from clio_agent.tools import namespace_instances  # noqa: PLC0415

    try:
        namespace_instances.retire_superseded()
    except Exception:  # noqa: BLE001 - typed; a retirement never blocks a close
        logger.exception("mcp_namespace_retire_failed")


def drawing_fleet(record: Any) -> Any:
    """The live fleet whose server drew ``record`` (``None`` when it is gone).

    A view's own calls go there, so a view drawn before its conversation moved to
    another fleet (a connector value change) keeps talking to the server that drew it.
    """

    ref = getattr(record, "fleet", None)
    fleet = ref() if ref is not None else None
    if fleet is None or server_stopped(record):
        return None
    return fleet
