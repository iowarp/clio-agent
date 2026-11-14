"""Apply a connector's new values to the servers that are running now (CN7).

A saved-configuration change rebuilds a workspace's tool fleet the next time it is
resolved, but a fleet in use by a running turn is kept until the turn ends (#1244),
and a call waiting on the setup card (CN5) is inside such a turn. So a value change
also rebinds the connector's namespace in every resident fleet in place: the
namespace takes the new declaration (a URL server reconnects, a command server is
stopped and starts again with the new arguments and environment on its next use),
and its tools are listed again, once for all fleets. What the plugin added (``PLUGIN_ROOT``,
``PLUGIN_DATA``) is kept. A namespace whose new listing fails keeps its last tools,
with the typed trace ``connector_relist_failed``.

Each fleet takes the declaration with its own values: a fleet a conversation's own
connector values keyed apart (CV-D3) keeps those values and only follows the
defaults it does not override, and a fleet whose connection does not change is left
alone (typed ``connector_rebind_unchanged``), so a default saved from one
conversation never restarts another conversation's server.

A connector the person enables while a turn runs (connector discovery) is not in
that turn's fleet at all: :func:`join_namespace` installs it there, so the turn can
call its tools from its next step. It installs the spec the conversation's server set
declares for the connector (:func:`conversation_spec`: the plugin's ``PLUGIN_ROOT`` and
``PLUGIN_DATA``, its source), the one the fleet mounts on its next resolve, so the
joined namespace is never seen as a changed spec there (CV-N3).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from clio_agent.runtime import trace
from clio_agent.tools.fleet_key import fleet_root

_PLUGIN_ENV = ("PLUGIN_ROOT", "PLUGIN_DATA")


def _executors(agent: Any) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    default = getattr(agent, "tool_executor", None)
    if default is not None:
        out.append(("", default))
    state = getattr(agent, "_workspace_state", None)
    if callable(state):
        lock, executors, _leases = state()
        with lock:
            out.extend(executors.items())
    return out


def rebound_spec(current: Any, declaration: Mapping[str, Any], namespace: str) -> Any:
    """``current`` (a mounted spec) carrying ``declaration``'s connection fields."""

    from clio_agent.tools.mcp_config import spec_from_declaration  # noqa: PLC0415

    fresh = spec_from_declaration(namespace, dict(declaration), source=current.source)
    kept = {key: value for key, value in (current.env or {}).items() if key in _PLUGIN_ENV}
    return dataclasses.replace(
        current,
        transport=fresh.transport,
        command=fresh.command,
        args=fresh.args,
        env={**fresh.env, **kept},
        url=fresh.url,
        headers=fresh.headers,
        auth=fresh.auth,
        declared=fresh.declared,
        validation_errors=fresh.validation_errors,
    )


def fleet_declaration(executor: Any, declaration: Mapping[str, Any]) -> dict[str, Any]:
    """``declaration`` (a saved connector template) with the fleet's own values filled."""

    from clio_agent.connectors.materialize import fill_declaration  # noqa: PLC0415

    values = getattr(executor, "_clio_connector_values", None) or {}
    overlay = values.get(str(declaration.get("connector") or "")) if declaration else None
    return fill_declaration(declaration, overlay, from_entry=True)


def _same_connection(a: Any, b: Any) -> bool:
    fields = ("transport", "command", "args", "env", "url", "headers")
    return all(getattr(a, name, None) == getattr(b, name, None) for name in fields)


def _install(executor: Any, namespace: str, spec: Any, root: str) -> None:
    """Route ``namespace`` of ``executor`` to a fresh proxy of ``spec`` (its connection
    is closed, so the next use starts it with ``spec``)."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415
    from clio_agent.tools.fleet_close_listeners import proxies_dropped  # noqa: PLC0415
    from clio_agent.tools.gateway import (  # noqa: PLC0415
        build_gateway,
        namespace_direct_factories,
        namespace_proxies,
    )

    specs = executor._clio_namespace_specs
    gateway = build_gateway({namespace: spec}, cwd=fleet_root(root) or None)
    proxy = namespace_proxies(gateway).get(namespace)
    inner = getattr(executor, "_async_executor", None)
    specs[namespace] = spec
    if inner is not None:
        inner_specs = getattr(inner, "_clio_namespace_specs", None)
        if isinstance(inner_specs, dict):
            inner_specs[namespace] = spec
        if proxy is not None:
            replaced = inner._namespace_servers.get(namespace)
            inner._namespace_servers[namespace] = proxy
            # CV-N2: a connection retained on the replaced proxy keeps its server running.
            proxies_dropped(root, (replaced,), "namespace_rebound")
        factories = getattr(inner, "_clio_namespace_direct_factories", None)
        factory = namespace_direct_factories(gateway).get(namespace)
        if isinstance(factories, dict) and factory is not None:
            factories[namespace] = factory
    reset = getattr(executor, "reset_namespace", None)
    if callable(reset):
        reset(namespace)
    mcp_prepare.forget(namespace)


def rebind_namespace(agent: Any, namespace: str, declaration: Mapping[str, Any]) -> list[str]:
    """Rebind ``namespace`` in every resident fleet; return the workspace roots touched."""

    from clio_agent.tools.mcp_discovery import ensure_namespace  # noqa: PLC0415

    touched: list[str] = []
    # The namespace's tools do not depend on the fleet: list them once, with the first
    # fleet's server, and give the same listing to every other fleet.
    listed: dict[str, Any] | None = None
    for root, executor in _executors(agent):
        specs = getattr(executor, "_clio_namespace_specs", None)
        if not isinstance(specs, dict) or namespace not in specs:
            continue
        if getattr(executor, "closed", False) or getattr(executor, "_closed", False):
            continue
        spec = rebound_spec(specs[namespace], fleet_declaration(executor, declaration), namespace)
        if _same_connection(spec, specs[namespace]):
            trace.event("TOOLS", "connector_rebind_unchanged namespace=%s root=%s", namespace, root)
            continue
        _install(executor, namespace, spec, root)
        touched.append(root)
        if not spec.usable:
            continue
        try:
            tools = listed if listed is not None else ensure_namespace(namespace, spec)
        except Exception as exc:  # noqa: BLE001 - typed; the namespace keeps its tools
            trace.event(
                "TOOLS",
                "connector_relist_failed namespace=%s root=%s reason=%s",
                namespace,
                root,
                type(exc).__name__,
            )
            continue
        listed = tools
        merge = getattr(executor, "merge_namespace_tools", None)
        if callable(merge):
            merge(namespace, tools)
    trace.event(
        "TOOLS", "connector_rebound namespace=%s roots=%s", namespace, ",".join(touched) or "-"
    )
    return touched


class ConnectorJoinError(RuntimeError):
    """A connector enabled during a turn could not join that turn's fleet (typed)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def conversation_spec(app: Any, session_id: str, namespace: str, root: str) -> Any | None:
    """The spec this conversation's server set declares for ``namespace`` (CV-N3).

    Once the connector's plugin is in the conversation, the session's server set
    declares its server the way every fleet mounts it (``plugins.servers``: the plugin's
    ``PLUGIN_ROOT`` and ``PLUGIN_DATA``, the conversation's values, source
    ``plugin:<id>``). ``None``, with the typed trace ``connector_join_spec_undeclared``,
    when the set does not declare it; the join then builds the spec from the saved
    connection.
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_server_set,
    )
    from clio_agent.plugins.servers import server_set_declarations  # noqa: PLC0415
    from clio_agent.tools.mcp_config import load_mcp_servers  # noqa: PLC0415

    reason = "not_in_server_set"
    spec = None
    try:
        key = session_server_set(app, session_id) if session_id else ""
        declared = server_set_declarations(key)
    except SessionAgentUnavailable as exc:
        reason, declared = exc.reason, {}
    except KeyError:
        reason, declared = "server_set_not_registered", {}
    if not session_id:
        reason = "no_conversation"
    elif declared:
        # The fleet builds its gateway the same way (``_build_tool_gateway``, cwd = root).
        specs = load_mcp_servers(cwd=fleet_root(root) or None, plugin_servers=declared)
        spec = specs.get(namespace)
    if spec is None:
        trace.event(
            "TOOLS",
            "connector_join_spec_undeclared namespace=%s session=%s reason=%s",
            namespace,
            session_id or "-",
            reason,
        )
    return spec


def fleet_root_of(agent: Any, executor: Any) -> str:
    """The fleet key ``executor`` is filed under ("" for the default executor)."""

    return next((r for r, e in _executors(agent) if e is executor), "")


def join_namespace(
    agent: Any,
    executor: Any,
    namespace: str,
    declaration: Mapping[str, Any],
    *,
    spec: Any = None,
) -> dict[str, Any]:
    """Mount a connector the person enabled during a turn into that turn's fleet.

    A running turn keeps the fleet it started with (#1244), so a connector enabled
    while it runs (connector discovery) is not in it. Its namespace is installed in
    ``executor`` from the saved connection and its tools merged into the executor's
    table: the listing the enable just made (:mod:`clio_agent.connectors.listing`),
    else a fresh one. ``spec`` is the conversation's declared spec
    (:func:`conversation_spec`); without it the spec is built from ``declaration``.
    Returns the prefixed tools; raises :class:`ConnectorJoinError`.
    """

    from clio_agent.connectors import listing  # noqa: PLC0415
    from clio_agent.tools.mcp_config import spec_from_declaration  # noqa: PLC0415
    from clio_agent.tools.mcp_discovery import ensure_namespace  # noqa: PLC0415
    from clio_agent.tools.mcp_tools_changed import prefixed_namespace_tools  # noqa: PLC0415

    specs = getattr(executor, "_clio_namespace_specs", None)
    merge = getattr(executor, "merge_namespace_tools", None)
    if not isinstance(specs, dict) or not callable(merge):
        raise ConnectorJoinError("connector_join_fleet_unavailable")
    root = fleet_root_of(agent, executor)
    if spec is None:
        declaration = fleet_declaration(executor, declaration)
        current = specs.get(namespace)
        spec = (
            rebound_spec(current, declaration, namespace)
            if current is not None
            else spec_from_declaration(namespace, dict(declaration), source="user")
        )
    if not spec.usable:
        raise ConnectorJoinError("connector_join_unusable", "; ".join(spec.validation_errors))
    # The fleet mounts it already, the same way (another conversation of this workspace
    # added it): its server, and every view it serves, stays. Reinstalling would restart
    # it under those views.
    mounted = specs.get(namespace) == spec
    if not mounted:
        _install(executor, namespace, spec, root)
    connector = str(getattr(spec, "connector", "") or namespace)
    kept = listing.offered(connector, listing.last_listed(connector))
    try:
        tools = (
            prefixed_namespace_tools(namespace, kept) if kept else ensure_namespace(namespace, spec)
        )
    except Exception as exc:  # noqa: BLE001 - typed for the caller
        raise ConnectorJoinError("connector_join_listing_failed", type(exc).__name__) from exc
    merge(namespace, tools)
    trace.event(
        "TOOLS",
        "connector_joined_turn namespace=%s root=%s tools=%d source=%s mounted=%s",
        namespace,
        root or "-",
        len(tools),
        "last_listing" if kept else "listed",
        "already" if mounted else "now",
    )
    return dict(tools)
