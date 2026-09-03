"""Read-only projection of resident workspace MCP connections."""

from __future__ import annotations

from typing import Any


def workspace_mcp_snapshot(agent: Any, root: str) -> dict[str, dict[str, Any]]:
    """Return resident MCP namespace state without creating or starting a fleet.

    An untouched workspace has no executor and returns an empty snapshot. This
    function deliberately reads the executor cache directly: calling the normal
    resolver would create a fleet and turn an Infrastructure page view into a
    runtime lifecycle mutation.
    """

    lock = getattr(agent, "_workspace_executor_lock", None)
    executors = getattr(agent, "_workspace_tool_executors", None)
    if lock is None or not isinstance(executors, dict):
        return {}
    with lock:
        executor = executors.get(root)
        if executor is None or getattr(executor, "closed", False):
            return {}
        names = list(getattr(executor, "get_tool_names", lambda: [])())
        namespaces = tuple(getattr(executor, "namespaces", lambda: ())())
        prepared = getattr(executor, "is_namespace_prepared", lambda _name: False)
        return {
            namespace: {
                "status": "ready" if prepared(namespace) else "available",
                "tools": [name for name in names if name.startswith(f"{namespace}_")],
            }
            for namespace in namespaces
        }
