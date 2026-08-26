"""Workspace-aware tool-executor resolution for dynamic agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def resolve_active_base_agent_tool_executor(
    base_agent: Any,
    *,
    app: Any,
    session_id: str,
    emit_downgrade: Callable[[Any], None],
) -> Any:
    """Resolve the active workspace executor without a process-global fallback."""
    resolver = getattr(base_agent, "_active_tool_executor", None)
    if callable(resolver):
        app_state = getattr(app, "state", None) if app is not None else None
        sessions = getattr(app_state, "sessions", None) if app_state is not None else None
        workspaces = getattr(app_state, "workspaces", None) if app_state is not None else None
        session = sessions.get(session_id) if sessions is not None and session_id else None
        workspace_id = str(getattr(session, "workspace_id", "") or "")
        workspace = (
            workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
        )
        workspace_root = str(getattr(workspace, "root_path", "") or "")
        if workspace_root:
            assert app is not None
            from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
                _runtime_active_agent_blueprint_id,
            )
            from clio_agent.tools.execution import (  # noqa: PLC0415
                tool_blueprint_context,
                tool_workspace_context,
            )

            blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
            with (
                tool_workspace_context(workspace_root),
                tool_blueprint_context(blueprint_id),
            ):
                executor = resolver()
        else:
            executor = resolver()
        if executor is not None:
            emit_downgrade(executor)
            return executor
    executor = getattr(base_agent, "tool_executor", None)
    emit_downgrade(executor)
    return executor
