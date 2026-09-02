"""Capability-keyed MCP task routing (#1281, campaign C1-S1).

Fixes the #1274 defect: every declared MCP server was reached through a
``ProxyClient`` whose ``_auto_internal_extensions = False`` pin suppresses
the SEP-2663 tasks extension declaration, so a ``task=required`` tool
answered -32021 on every call through the declared path. The fix is
capability-keyed routing: a server whose task capability has been
NEGOTIATED (never probed by behavior or timing) gets a DIRECT
task-declaring client at call time; a server whose capability is unknown
or genuinely v1 keeps today's proxy path byte-identical.

This module is the owner for the routing DECISION and the two capability
READ sites; ``tools/gateway.py`` and ``tools/mcp_executor.py`` sit at their
file-size ratchet ceiling (#775 no-accretion), so only thin call sites land
there. The per-server capability RECORD itself (:class:`~clio_agent.tools.
mcp_connection_era.MCPTaskCapability`) and its registry live in
``mcp_connection_era.py`` (the existing home for typed, discovery-time MCP
connection facts) per the campaign design.

Two capability-read seams, deliberately asymmetric:

- :func:`record_definitive_capability` -- the DEFINITIVE read (server-
  declared extensions AND a full tool listing), called from the single
  choke point both live listing paths share: ``gateway._list_declared_tools``
  (the boot catalog pass, a cache refresh, and an on-demand namespace
  mount all route through it). Has enough information to record a genuine
  negative (a task-free server, or a v1 server whose listing shows no
  ``execution.task_support`` arm).
- An OPPORTUNISTIC, POSITIVE-ONLY read lives in ``mcp_connection_era.
  instrument_client_era``'s existing ``__aenter__`` composition (not in this
  module -- that seam already instruments EVERY real client connect,
  including a mounted proxy's per-request backend clone, the ONE place that
  dials a proxy's real backend for real; piggybacking there needs no new
  class-composition machinery). It fires on both a proxy-routed listing
  fan-out and a proxy-routed call, closing the gap where a real backend
  connect happens before ``_list_declared_tools``'s own definitive pass has
  run (e.g. a directly-constructed executor in tests). A bare connect
  carries no guaranteed tool listing, so it never writes a negative --
  never clobbering an earlier-discovered True.

Both feed the SAME registry (:func:`~clio_agent.tools.mcp_connection_era.
record_task_capability`), which always overwrites with the latest verdict.
The route decision itself (:func:`resolve_namespace_route`) only ever
CONSULTS that registry -- it is read-only, never a third write path.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from fastmcp.utilities.tasks import TASKS_EXTENSION_ID

from clio_agent.errors import MCP_TASK_CAPABILITY_UNKNOWN, MCP_TASKS_DIRECT_ROUTE_SELECTED
from clio_agent.tools.mcp_connection_era import (
    MCPTaskCapability,
    latest_task_capability,
    record_task_capability,
)

if TYPE_CHECKING:
    from clio_agent.tools.mcp_config import MCPServerSpec
    from clio_agent.tools.mcp_handlers import MCPClientCapabilities
    from clio_agent.tools.mcp_runtime import MCPClientHandlers

logger = logging.getLogger(__name__)

__all__ = [
    "NamespaceRouteDecision",
    "direct_client_factory",
    "record_definitive_capability",
    "recorded_task_route_decisions",
    "resolve_namespace_route",
]


def _tool_task_support(tool: Any) -> str | None:
    """One listed tool's legacy-era ``execution.task_support`` marker, if any."""

    execution = getattr(tool, "execution", None)
    return getattr(execution, "task_support", None) if execution is not None else None


def _extensions_declare_tasks(client: Any) -> bool:
    """Whether a connected client's SERVER-declared extensions carry the tasks id.

    Reads ``client.server_capabilities`` -- populated by the SDK's own
    negotiation (``initialize``/``server/discover``), independent of what
    THIS client itself declared. A ``ProxyClient`` that suppresses its OWN
    extension advertisement (#1119) still sees the backend's true
    capabilities here, since the server answers unconditionally.
    """

    capabilities = getattr(client, "server_capabilities", None)
    extensions = getattr(capabilities, "extensions", None) or {}
    return TASKS_EXTENSION_ID in extensions


def record_definitive_capability(
    server_id: str, client: Any, tools: Sequence[Any]
) -> MCPTaskCapability:
    """Record the DEFINITIVE task capability from a connect + full listing.

    Reads both era markers -- the modern server-declared extensions first,
    then the legacy per-tool ``execution.task_support`` arm ("optional" or
    "required", SEP-1686) -- and records a genuine negative
    (``task_capable=False``, ``source="none"``) when neither is present.
    Call only from a site that has BOTH a live ``client`` and its full
    ``tools`` listing in hand (``gateway._list_declared_tools``).
    """

    if _extensions_declare_tasks(client):
        return record_task_capability(
            server_id, task_capable=True, source="capabilities_extensions"
        )
    if any(_tool_task_support(tool) in ("optional", "required") for tool in tools):
        return record_task_capability(server_id, task_capable=True, source="tool_execution")
    return record_task_capability(server_id, task_capable=False, source="none")


@dataclass(frozen=True)
class NamespaceRouteDecision:
    """Which client construction path one namespace connect should use, and why."""

    use_direct: bool
    reason: str | None


#: Bounded, queryable ring of route decisions -- mirrors ``mcp_connection_
#: era``'s ``_DOWNGRADES`` audit-sink pattern (#775 no-silent-fallback: every
#: routing branch is observable, not merely logged).
_ROUTE_DECISIONS: "deque[tuple[str, NamespaceRouteDecision]]" = deque(maxlen=256)
_ROUTE_DECISIONS_LOCK = Lock()


def resolve_namespace_route(namespace: str) -> NamespaceRouteDecision:
    """Decide direct vs. proxy for one namespace connect -- typed, never probed.

    Consults ONLY :func:`~clio_agent.tools.mcp_connection_era.
    latest_task_capability` (read-only; this function never writes a
    capability verdict). Capability unknown (no discovery has landed yet)
    keeps today's proxy path, typed :data:`~clio_agent.errors.
    MCP_TASK_CAPABILITY_UNKNOWN`. A known task-capable server routes direct,
    typed :data:`~clio_agent.errors.MCP_TASKS_DIRECT_ROUTE_SELECTED`. A known
    task-INCAPABLE server (genuine v1, or a genuinely task-free server) keeps
    the proxy path with no reason recorded here -- ``tasks_declaration``'s
    own ``mcp_tasks_declaration_suppressed`` reason already covers that
    client-class angle when the call actually dispatches through
    ``ProxyClient``.
    """

    capability = latest_task_capability(namespace)
    if capability is None:
        decision = NamespaceRouteDecision(use_direct=False, reason=MCP_TASK_CAPABILITY_UNKNOWN)
    elif capability.task_capable:
        decision = NamespaceRouteDecision(use_direct=True, reason=MCP_TASKS_DIRECT_ROUTE_SELECTED)
    else:
        decision = NamespaceRouteDecision(use_direct=False, reason=None)
    with _ROUTE_DECISIONS_LOCK:
        _ROUTE_DECISIONS.append((namespace, decision))
    if decision.reason is not None:
        logger.debug(
            "mcp namespace route decision namespace=%s use_direct=%s reason=%s",
            namespace,
            decision.use_direct,
            decision.reason,
        )
    return decision


def recorded_task_route_decisions() -> list[tuple[str, NamespaceRouteDecision]]:
    """Return a snapshot of recorded namespace route decisions (queryable audit)."""

    with _ROUTE_DECISIONS_LOCK:
        return list(_ROUTE_DECISIONS)


def direct_client_factory(
    spec: "MCPServerSpec",
    cwd: str | None,
    *,
    handlers: "MCPClientHandlers | None",
    capabilities: "MCPClientCapabilities | None",
    namespace: str,
) -> Callable[[], Any]:
    """Build a per-namespace DIRECT-CLIENT factory closure for a declared spec.

    Captures ``spec``/``cwd``/``handlers``/``capabilities`` at MOUNT time
    (``gateway.build_gateway``) -- the same bundle the proxy backend binds
    into its own ``_proxy_for_spec`` partial -- so switching a namespace from
    proxy to direct changes only the extension declaration, nothing else
    CLIO wires (handlers, capabilities). The transport is built LAZILY
    inside the returned closure (a fresh ``transport_for`` call per
    invocation), so mounting never spawns a subprocess.

    The closure uses ``make_mcp_client``'s DEFAULT client class (``fastmcp.
    Client``, never ``ProxyClient``) -- that alone is what makes
    ``tasks_declaration`` attach the SEP-2663 tasks extension (the
    ``_auto_internal_extensions`` gate the proxy path forbids), and era
    instrumentation (``server_id=namespace``) comes free through
    ``make_mcp_client``.

    Returns:
        A zero-argument callable that, invoked, returns a fresh (not yet
        entered) FastMCP client for this declared server.
    """

    def _factory() -> Any:
        from clio_agent.tools.mcp_config import transport_for  # noqa: PLC0415
        from clio_agent.tools.mcp_runtime import make_mcp_client  # noqa: PLC0415

        transport = transport_for(spec, cwd=cwd)
        return make_mcp_client(
            transport, handlers=handlers, capabilities=capabilities, server_id=namespace
        )

    return _factory
