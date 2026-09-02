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
  ``execution.task_support`` arm). A cache HIT never reaches this function
  at all -- :func:`capability_cache_fields`/the listing-cache's own replay
  (#1281 F3) is what keeps a cache hit's capability queryable without a
  live re-list.
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
record_task_capability`), which overwrites with the latest verdict subject
to the F7 demotion guard (a False may not clobber an authoritative True
unless equally authoritative -- see that function's docstring).

The route DECISION (:func:`resolve_namespace_route`) is purely read-only
against that registry and writes nothing, not even to the audit ring --
adversarial review F4: the ring must record the decision ACTUALLY TAKEN
(which also depends on whether a direct-client factory exists AND
successfully constructs), never the unreachable intent. Call-time routing
therefore goes through :func:`resolve_and_build_direct_client`, which
resolves AND attempts construction in one step and returns the outcome that
:func:`record_namespace_route_decision` (F6: mirrors ``mcp_connection_era.
_record_downgrade`` -- ring + log + ``stream_audit`` on every reasoned
branch) then records.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from fastmcp.utilities.tasks import TASKS_EXTENSION_ID

from clio_agent.errors import (
    MCP_TASK_CAPABILITY_UNKNOWN,
    MCP_TASK_DIRECT_FACTORY_CONSTRUCTION_FAILED,
    MCP_TASK_DIRECT_FACTORY_MISSING,
    MCP_TASK_ROUTE_HEALED,
    MCP_TASKS_DIRECT_ROUTE_SELECTED,
)
from clio_agent.tools.mcp_connection_era import (
    MCPTaskCapability,
    latest_task_capability,
    protocol_version_era,
    record_task_capability,
)

if TYPE_CHECKING:
    from clio_agent.tools.mcp_config import MCPServerSpec
    from clio_agent.tools.mcp_handlers import MCPClientCapabilities
    from clio_agent.tools.mcp_runtime import MCPClientHandlers

logger = logging.getLogger(__name__)

__all__ = [
    "NamespaceRouteDecision",
    "capability_cache_fields",
    "direct_client_factory",
    "record_definitive_capability",
    "record_namespace_route_decision",
    "record_route_healed",
    "recorded_task_route_decisions",
    "resolve_and_build_direct_client",
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
    The negotiated era (:func:`~clio_agent.tools.mcp_connection_era.
    protocol_version_era`, off ``client.protocol_version``) rides along so
    the F7 demotion guard can tell an equally-authoritative re-read from a
    downgraded one. Call only from a site that has BOTH a live ``client``
    and its full ``tools`` listing in hand (``gateway._list_declared_tools``).
    """

    era = protocol_version_era(getattr(client, "protocol_version", None))
    if _extensions_declare_tasks(client):
        return record_task_capability(
            server_id, task_capable=True, source="capabilities_extensions", era=era
        )
    if any(_tool_task_support(tool) in ("optional", "required") for tool in tools):
        return record_task_capability(
            server_id, task_capable=True, source="tool_execution", era=era
        )
    return record_task_capability(server_id, task_capable=False, source="none", era=era)


def capability_cache_fields(namespace: str) -> tuple[bool | None, str | None, str | None]:
    """``(task_capable, source, era)`` for persisting alongside a listing-cache entry.

    #1281 F3 (adversarial review): both listing-cache callers
    (``gateway.list_tool_definitions``, ``mcp_discovery._list_one_namespace``)
    are cache-first, and a cache HIT never reaches
    :func:`record_definitive_capability` -- the capability registry would
    stay permanently unknown for a warm namespace. Callers pass this
    triple to ``listing_cache.store_listing`` right after a LIVE listing (so
    the entry carries the just-negotiated verdict for the NEXT hit to
    replay); ``listing_cache.load_listing`` replays it back through
    :func:`~clio_agent.tools.mcp_connection_era.record_task_capability` on
    every hit. All-``None`` when nothing has been recorded for ``namespace``
    yet, in which case the cache entry persists no capability fields at all.
    """

    capability = latest_task_capability(namespace)
    if capability is None:
        return None, None, None
    return capability.task_capable, capability.source, capability.era


@dataclass(frozen=True)
class NamespaceRouteDecision:
    """Which client construction path one namespace connect should use, and why."""

    use_direct: bool
    reason: str | None


#: Bounded, queryable ring of route decisions -- mirrors ``mcp_connection_
#: era``'s ``_DOWNGRADES`` audit-sink pattern (#775 no-silent-fallback: every
#: routing branch is observable, not merely logged). Written ONLY by
#: :func:`record_namespace_route_decision` -- the decision ACTUALLY TAKEN,
#: never the unreachable intent (#1281 F4).
_ROUTE_DECISIONS: "deque[tuple[str, NamespaceRouteDecision]]" = deque(maxlen=256)
_ROUTE_DECISIONS_LOCK = Lock()


def resolve_namespace_route(namespace: str) -> NamespaceRouteDecision:
    """Decide direct vs. proxy for one namespace connect -- typed, never probed.

    PURE and read-only: consults ONLY :func:`~clio_agent.tools.
    mcp_connection_era.latest_task_capability` and writes NOTHING (not the
    capability registry, not the audit ring -- #1281 F4: this function only
    knows the INTENT; whether a direct factory actually exists and
    successfully constructs is call-time information this function cannot
    see, so recording here would let the ring claim a route that was never
    actually taken). Capability unknown (no discovery has landed yet) keeps
    today's proxy path, typed :data:`~clio_agent.errors.
    MCP_TASK_CAPABILITY_UNKNOWN`. A known task-capable server's INTENT is
    direct, typed :data:`~clio_agent.errors.MCP_TASKS_DIRECT_ROUTE_SELECTED`
    (:func:`resolve_and_build_direct_client` may still downgrade this to a
    factory-missing/construction-failed proxy fallback). A known
    task-INCAPABLE server (genuine v1, or a genuinely task-free server)
    keeps the proxy path with no reason recorded here --
    ``tasks_declaration``'s own ``mcp_tasks_declaration_suppressed`` reason
    already covers that client-class angle when the call actually dispatches
    through ``ProxyClient``.
    """

    capability = latest_task_capability(namespace)
    if capability is None:
        return NamespaceRouteDecision(use_direct=False, reason=MCP_TASK_CAPABILITY_UNKNOWN)
    if capability.task_capable:
        return NamespaceRouteDecision(use_direct=True, reason=MCP_TASKS_DIRECT_ROUTE_SELECTED)
    return NamespaceRouteDecision(use_direct=False, reason=None)


def resolve_and_build_direct_client(
    namespace: str, direct_factories: Mapping[str, Callable[[], Any]]
) -> tuple[Any | None, NamespaceRouteDecision]:
    """Resolve the route AND attempt to build the direct client in one step.

    Returns ``(client, decision)`` where ``client`` is ``None`` iff the
    caller must fall back to the proxy path, and ``decision`` is the route
    ACTUALLY TAKEN (never merely :func:`resolve_namespace_route`'s intent --
    #1281 F4): a namespace INTENDED direct with no factory threaded onto the
    calling executor demotes to proxy typed
    :data:`~clio_agent.errors.MCP_TASK_DIRECT_FACTORY_MISSING`; a factory
    that raises on construction (#1281 F9: e.g. ``transport_for`` refusing a
    spec at call time) demotes to proxy typed :data:`~clio_agent.errors.
    MCP_TASK_DIRECT_FACTORY_CONSTRUCTION_FAILED` rather than hard-failing a
    call the proxy would still serve (the server's own capability
    declaration already proves the proxy CAN serve it, just without the
    tasks extension). Does not itself record to the ring/audit trail --
    call :func:`record_namespace_route_decision` with the returned decision.
    """

    route = resolve_namespace_route(namespace)
    if not route.use_direct:
        return None, route
    factory = direct_factories.get(namespace)
    if factory is None:
        return None, NamespaceRouteDecision(
            use_direct=False, reason=MCP_TASK_DIRECT_FACTORY_MISSING
        )
    try:
        return factory(), route
    except Exception as exc:  # noqa: BLE001 - typed fallback (F9): the proxy path still serves this call
        logger.warning(
            "mcp direct route factory construction failed namespace=%s reason=%s error=%s",
            namespace,
            MCP_TASK_DIRECT_FACTORY_CONSTRUCTION_FAILED,
            exc,
        )
        return None, NamespaceRouteDecision(
            use_direct=False, reason=MCP_TASK_DIRECT_FACTORY_CONSTRUCTION_FAILED
        )


def record_namespace_route_decision(namespace: str, decision: NamespaceRouteDecision) -> None:
    """Record the route decision ACTUALLY TAKEN for one namespace connect.

    Mirrors ``mcp_connection_era._record_downgrade`` (#1281 F6): appends to
    the bounded queryable ring on every call, and -- for every REASONED
    branch (``decision.reason is not None``) -- logs and ``stream_audit``s,
    so no branch is silent (#775). The capability-unknown branch logs at
    INFO (an expected, self-healing transient at boot, not yet a problem);
    every other reasoned branch (direct-route-selected, factory-missing,
    factory-construction-failed) logs at WARNING -- factory problems are
    genuine degrades, and even the "good news" direct-route-selected branch
    is worth an operator's attention the first time it fires for a server
    (per #6/#1274: this is the fix engaging for real).
    """

    with _ROUTE_DECISIONS_LOCK:
        _ROUTE_DECISIONS.append((namespace, decision))
    if decision.reason is None:
        return
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    log = logger.info if decision.reason == MCP_TASK_CAPABILITY_UNKNOWN else logger.warning
    log(
        "mcp namespace route decision namespace=%s use_direct=%s reason=%s",
        namespace,
        decision.use_direct,
        decision.reason,
    )
    stream_audit(
        "mcp_task_route_decision",
        reason=decision.reason,
        namespace=namespace,
        use_direct=decision.use_direct,
    )


def record_route_healed(namespace: str) -> None:
    """Typed + audited (#1281 F2, F6): a namespace's cached client was
    evicted and reconnected direct because capability discovery landed True
    AFTER the connect that originally cached it on the proxy path."""

    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    logger.warning(
        "mcp namespace route healed namespace=%s reason=%s", namespace, MCP_TASK_ROUTE_HEALED
    )
    stream_audit("mcp_task_route_healed", reason=MCP_TASK_ROUTE_HEALED, namespace=namespace)


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
        entered) FastMCP client for this declared server. Invocation may
        raise (e.g. ``transport_for`` on a malformed spec); callers use
        :func:`resolve_and_build_direct_client`, which catches and demotes
        typed rather than propagating raw (#1281 F9).
    """

    def _factory() -> Any:
        from clio_agent.tools.mcp_config import transport_for  # noqa: PLC0415
        from clio_agent.tools.mcp_runtime import make_mcp_client  # noqa: PLC0415

        transport = transport_for(spec, cwd=cwd)
        return make_mcp_client(
            transport, handlers=handlers, capabilities=capabilities, server_id=namespace
        )

    return _factory
