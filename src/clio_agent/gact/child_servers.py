"""A settled child's own MCP servers close with it.

A child session (a subagent) runs in its parent's workspace fleet. A server its agent
shares with the conversation, or with another session of that workspace that is still
working, is the conversation's: it stays up with its state as long as the fleet does.
A server only the child's agent mounted is the child's: when the child's task settles
(completed, failed or cancelled, including a Stop that stops children), its kept-alive
connection closes at once, and with it the server's process and sandbox, instead of
idling until the fleet's reaper. Nothing here names a server; the rule reads only the
mounted server sets. A hold the child kept while waiting on its user ends too.

The work runs on its own thread: the settle that triggers it runs on the task
history's IO lane, and resolving server sets and closing connections must not block it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

#: Logged with the namespaces a settled child's close stopped.
CHILD_SERVERS_CLOSED = "child_servers_closed"
#: The fleet-close reason the retired connections record.
CHILD_SETTLED = "child_settled"


def close_child_servers(app: Any, session_id: str) -> threading.Thread | None:
    """Close ``session_id``'s own servers in the background (a child that settled)."""

    if app is None or not session_id:
        return None
    worker = threading.Thread(
        target=_close, args=(app, session_id), name=f"clio-child-servers-{session_id}"
    )
    worker.daemon = True
    worker.start()
    return worker


def _close(app: Any, sid: str) -> None:
    from clio_agent.gact import session_warmup  # noqa: PLC0415

    session_warmup.release_session_fleet(sid)
    try:
        plan = exclusive_servers(app, sid)
    except Exception:  # noqa: BLE001 - settling never fails; the fleet's reaper still applies
        logger.exception("%s_failed session=%s", CHILD_SERVERS_CLOSED, sid)
        return
    if plan is None:
        return
    root, executor, namespaces = plan
    from clio_agent.tools.fleet_close_listeners import proxies_dropped  # noqa: PLC0415

    inner = getattr(executor, "_async_executor", executor)
    servers = getattr(inner, "_namespace_servers", None) or {}
    closed: list[str] = []
    for namespace in namespaces:
        proxy = servers.get(namespace)
        if proxy is None:
            continue
        reset = getattr(executor, "reset_namespace", None)
        try:
            if callable(reset) and not getattr(executor, "closed", False):
                reset(namespace)  # the executor's own hold on it ends first
        except Exception:  # noqa: BLE001 - the retained connection still retires below
            logger.exception("%s_reset_failed namespace=%s", CHILD_SERVERS_CLOSED, namespace)
        from clio_agent.tools import namespace_instances  # noqa: PLC0415

        proxies_dropped(root, namespace_instances.unshared((proxy,), inner), CHILD_SETTLED)
        closed.append(namespace)
    if closed:
        logger.info(
            "%s session=%s root=%s namespaces=%s",
            CHILD_SERVERS_CLOSED,
            sid,
            root,
            ",".join(closed),
        )


def _declared(server_set: str) -> set[str]:
    from clio_agent.plugins.servers import server_set_declarations  # noqa: PLC0415

    try:
        declarations = server_set_declarations(server_set)
    except KeyError:
        return set()
    return {name for servers in declarations.values() for name in servers}


def _others(app: Any, session: Any) -> set[str]:
    """The sessions whose servers must stay: the parent chain, open siblings, busy ones."""

    sessions = app.state.sessions
    others: set[str] = set()
    parent = str(getattr(session, "parent_session_id", "") or "")
    seen = {session.id}
    while parent and parent not in seen:
        seen.add(parent)
        others.add(parent)
        upper = sessions.get(parent)
        parent = str(getattr(upper, "parent_session_id", "") or "") if upper else ""
    direct = str(getattr(session, "parent_session_id", "") or "")
    if direct:
        from clio_agent.gact.routes.session_cancellation import stop_scope  # noqa: PLC0415

        others.update(str(row.get("session_id") or "") for row in stop_scope(app, direct))
    runner = getattr(app.state, "turn_runner", None)
    active = getattr(runner, "active_sessions", None)
    if callable(active):
        for sid in active():
            other = sessions.get(sid)
            if other is not None and other.workspace_id == session.workspace_id:
                others.add(sid)
    others.discard(session.id)
    others.discard("")
    return others


def exclusive_servers(app: Any, sid: str) -> tuple[str, Any, list[str]] | None:
    """``(root, fleet executor, namespaces)`` only the settled child ``sid`` uses, or ``None``.

    ``None`` when ``sid`` is not a child, its fleet is not resident, or any other
    session's server set cannot be read (then nothing closes: the reaper still does).
    """

    from clio_agent.gact.agents.resolution import session_server_set  # noqa: PLC0415
    from clio_agent.tools.fleet_key import fleet_key  # noqa: PLC0415
    from clio_agent.tools.fleet_own_set import fleet_variant  # noqa: PLC0415
    from clio_agent.tools.fleet_server_merge import _mounted_server_sets  # noqa: PLC0415
    from clio_agent.tools.workspace_root import canonical_workspace_root  # noqa: PLC0415

    session = app.state.sessions.get(sid)
    if session is None or not getattr(session, "parent_session_id", ""):
        return None
    workspace = app.state.workspaces.get(session.workspace_id)
    root = canonical_workspace_root(str(getattr(workspace, "root_path", "") or ""))
    agent = getattr(app.state, "agent", None)
    state = getattr(agent, "_workspace_state", None)
    if not root or not callable(state):
        return None
    own = session_server_set(app, sid)
    if not own:
        return None
    lock, executors, _leases = state()
    with lock:
        executor = executors.get(fleet_key(root, fleet_variant(own, root)))
    if executor is None or getattr(executor, "closed", False):
        return None
    keep_sets = {mounted for mounted in _mounted_server_sets(executor) if mounted != own}
    for other in _others(app, session):
        keep_sets.add(session_server_set(app, other))
    if own in keep_sets:
        return None
    kept: set[str] = set()
    for server_set in keep_sets:
        kept |= _declared(server_set)
    namespaces = sorted(_declared(own) - kept)
    return (root, executor, namespaces) if namespaces else None


__all__ = ["CHILD_SERVERS_CLOSED", "CHILD_SETTLED", "close_child_servers", "exclusive_servers"]
