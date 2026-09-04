"""Read-only projection of resident workspace MCP connections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clio_agent.tools.workspace_root import canonical_workspace_root

#: No agent is published on the app yet (deferred construction, a failed bind).
FLEET_HOST_ABSENT = "workspace_fleet_host_absent"
#: This workspace has never resolved a tool executor -- nothing is resident.
FLEET_NOT_STARTED = "workspace_fleet_not_started"
#: The fleet WAS resident and has been closed (the #933 idle reaper, an evict).
FLEET_CLOSED = "workspace_fleet_closed"
#: The host or executor is missing state this reader requires. NOT "not ready":
#: a required accessor that disappeared is a broken contract, and defaulting it
#: to a falsy stand-in reports "no namespaces are prepared" for a fleet that may
#: be fully connected.
FLEET_INTERFACE_DRIFT = "workspace_fleet_interface_drift"


@dataclass(frozen=True)
class WorkspaceMcpSnapshot:
    """Resident namespace state, or the typed reason there is none.

    ``namespaces`` empty with ``reason`` empty is a real answer (a started fleet
    that declares nothing). Every OTHER empty result names why.
    """

    namespaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str = ""
    detail: str = ""

    @property
    def degraded(self) -> dict[str, str] | None:
        """The typed degradation row for a caller that surfaces reasons."""

        if not self.reason:
            return None
        return {"reason": self.reason, "detail": self.detail}


def _degraded(reason: str, detail: str) -> WorkspaceMcpSnapshot:
    return WorkspaceMcpSnapshot(reason=reason, detail=detail)


def workspace_mcp_snapshot(agent: Any, root: str) -> WorkspaceMcpSnapshot:
    """Return resident MCP namespace state without creating or starting a fleet.

    This function deliberately reads the executor cache directly: calling the
    normal resolver would create a fleet and turn an Infrastructure page view
    into a runtime lifecycle mutation. ``root`` is canonicalized through the same
    helper the fleet registry keys with, so a workspace whose ``root_path`` is
    spelled differently (``~``, trailing separator, forward slashes on Windows)
    still finds its own executor.

    NOTE: this takes ``ClioAgent._workspace_executor_lock``, a ``threading.Lock``
    a live turn can hold. Async callers must run it on a worker thread.
    """

    lock = getattr(agent, "_workspace_executor_lock", None)
    if lock is None:
        return _degraded(
            FLEET_HOST_ABSENT,
            "no agent is published on this server yet, so no workspace fleet exists",
        )
    executors = getattr(agent, "_workspace_tool_executors", None)
    if not isinstance(executors, dict):
        return _degraded(
            FLEET_INTERFACE_DRIFT,
            f"agent._workspace_tool_executors is {type(executors).__name__}, expected dict",
        )
    key = canonical_workspace_root(root)
    with lock:
        executor = executors.get(key)
        if executor is None:
            return _degraded(
                FLEET_NOT_STARTED,
                f"no resident tool executor for workspace root {key or '<unbound>'}",
            )
        if getattr(executor, "closed", False):
            return _degraded(
                FLEET_CLOSED,
                f"the resident fleet for {key} was closed; it rebuilds on the next tool call",
            )
        try:
            # Hard attribute access, not a ``getattr(..., default)``: every real
            # executor implements these, so a miss is drift worth reporting.
            names = list(executor.get_tool_names())
            namespaces = tuple(executor.namespaces())
            prepared = executor.is_namespace_prepared
        except AttributeError as exc:
            return _degraded(FLEET_INTERFACE_DRIFT, f"resident executor is missing {exc}")
        return WorkspaceMcpSnapshot(
            namespaces={
                namespace: {
                    "status": "ready" if prepared(namespace) else "available",
                    "tools": [name for name in names if name.startswith(f"{namespace}_")],
                }
                for namespace in namespaces
            }
        )
