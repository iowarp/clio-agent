"""The fleet serving one conversation, and the channel of the server behind a namespace.

The gact side of :mod:`clio_agent.tools.fleet_serving`: it reads the session's workspace
root and server set the way the turn binds them (``runtime/tool_session``), so the fleet a
reader names is the one the conversation's tool calls run on.
"""

from __future__ import annotations

from typing import Any

from clio_agent.tools.fleet_serving import serving_fleet_key, serving_net_child


def session_workspace_root(app: Any, sid: str) -> str:
    """The stored root folder of ``sid``'s workspace (``""`` without one)."""

    state = getattr(app, "state", None)
    sessions = getattr(state, "sessions", None)
    workspaces = getattr(state, "workspaces", None)
    session = sessions.get(sid) if sessions is not None and sid else None
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    workspace = workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
    return str(getattr(workspace, "root_path", "") or "")


def session_fleet_key(app: Any, sid: str) -> str:
    """The key of the fleet serving ``sid`` (``""``: the default executor)."""

    from clio_agent.gact.agents.resolution import session_server_set  # noqa: PLC0415

    return serving_fleet_key(session_workspace_root(app, sid), session_server_set(app, sid))


def session_net_child(app: Any, sid: str, namespace: str) -> str:
    """The channel of the server ``sid``'s fleet routes ``namespace`` to, or ``""``.

    Inside ``sid``'s own tool call it reads the root and server set the turn bound, the
    ones the executor resolved this call's fleet with; elsewhere it reads the session.
    """

    agent = getattr(getattr(app, "state", None), "agent", None)
    if agent is None or not namespace:
        return ""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.tools.execution import (  # noqa: PLC0415
        get_active_tool_server_set,
        get_active_tool_workspace_root,
    )

    if sid and _ctx.active_tool_session_id() == sid:
        key = serving_fleet_key(get_active_tool_workspace_root(), get_active_tool_server_set())
    else:
        key = session_fleet_key(app, sid)
    return serving_net_child(agent, key, namespace)


__all__ = ["session_fleet_key", "session_net_child", "session_workspace_root"]
