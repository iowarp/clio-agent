"""Bring a resident workspace fleet onto a changed MCP configuration, server by server.

A saved MCP configuration change (a connector enabled or edited, a user server added
or removed) moves the agent's configuration revision (``_mcp_configuration_revision``).
The fleet used to close whole on that move, at its next idle point when a turn held
it, so enabling one connector restarted every other server of the workspace and lost
its state: an open scene, a notebook kernel, a running job's connection.

:func:`reconcile_configuration` compares the fleet's mounted declarations with the
ones its server sets declare now and changes only what differs:

* a namespace whose declaration changed is installed again (its server stops and
  starts on its next use with the new declaration), as a connector rebind does;
* a namespace no longer declared is stopped, unrouted and its tools dropped;
* a namespace declared now and not mounted joins lazily, as a server-set merge does;
* every other server keeps running with its state.

Logged ``workspace_fleet_configuration_reconciled`` with each list.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace
from clio_agent.tools.fleet_server_merge import (
    _mounted_server_sets,
    _specs_equal,
    _stamp,
    merge_server_set_namespaces,
)
from clio_agent.tools.gateway import namespace_specs


def _declared_now(
    agent: Any, root: str, server_sets: list[str]
) -> tuple[dict[str, list[Any]], dict[str, Any]]:
    """Each mounted server set's gateway now, and every spec each namespace is declared with.

    Two sets may declare one namespace differently; the fleet kept the first one mounted
    (:func:`merge_server_set_namespaces`), so any of them counts as unchanged.
    """

    gateways: dict[str, Any] = {}
    declared: dict[str, list[Any]] = {}
    for server_set in server_sets:
        gateway = agent._build_tool_gateway(cwd=root, server_set=server_set)
        gateways[server_set] = gateway
        for namespace, spec in namespace_specs(gateway).items():
            declared.setdefault(namespace, []).append(spec)
    return declared, gateways


def _drop(executor: Any, namespace: str, root: str) -> None:
    """Stop ``namespace``'s server and remove it from the fleet's routing and tools."""

    from clio_agent.tools.fleet_close_listeners import proxies_dropped  # noqa: PLC0415

    reset = getattr(executor, "reset_namespace", None)
    if callable(reset):
        reset(namespace)
    specs = getattr(executor, "_clio_namespace_specs", None)
    if isinstance(specs, dict):
        specs.pop(namespace, None)
    inner = getattr(executor, "_async_executor", None)
    if inner is None:
        return
    inner_specs = getattr(inner, "_clio_namespace_specs", None)
    if isinstance(inner_specs, dict):
        inner_specs.pop(namespace, None)
    factories = getattr(inner, "_clio_namespace_direct_factories", None)
    if isinstance(factories, dict):
        factories.pop(namespace, None)
    servers = getattr(inner, "_namespace_servers", None)
    if isinstance(servers, dict) and namespace in servers:
        proxies_dropped(root, (servers.pop(namespace),), "namespace_removed")
    tools = getattr(inner, "_mcp_tools", None)
    if isinstance(tools, dict):
        prefix = f"{namespace}_"
        kept = {name: tool for name, tool in tools.items() if not name.startswith(prefix)}
        if len(kept) != len(tools):
            inner._mcp_tools = kept
            inner.__dict__["_clio_tools_revision"] = inner.tools_revision + 1


def reconcile_configuration(agent: Any, executor: Any, root: str, epoch: Any) -> dict[str, Any]:
    """Change only the namespaces of ``executor`` whose declaration moved; stamp ``epoch``.

    Returns ``{"changed": [...], "removed": [...], "added": [...], "kept": <int>}``.
    """

    from clio_agent.gact.connector_rebind import _install  # noqa: PLC0415

    mounted = sorted(_mounted_server_sets(executor)) or [""]
    existing = getattr(executor, "_clio_namespace_specs", None)
    if not isinstance(existing, dict):
        existing = {}
        _stamp(executor, "_clio_namespace_specs", existing)
    declared, gateways = _declared_now(agent, root, mounted)
    changed = sorted(
        namespace
        for namespace, spec in existing.items()
        if namespace in declared
        and not any(_specs_equal(spec, other) for other in declared[namespace])
    )
    removed = sorted(namespace for namespace in existing if namespace not in declared)
    added = sorted(namespace for namespace in declared if namespace not in existing)
    kept = len(existing) - len(changed) - len(removed)
    for namespace in changed:
        _install(executor, namespace, declared[namespace][0], root)
    for namespace in removed:
        _drop(executor, namespace, root)
    if added:
        for server_set, gateway in gateways.items():
            merge_server_set_namespaces(executor, gateway, server_set=server_set, root=root)
    _stamp(executor, "_clio_mcp_configuration_revision", epoch)
    trace.event(
        "TOOLS",
        "workspace_fleet_configuration_reconciled root=%s epoch=%s changed=%s removed=%s "
        "added=%s kept=%d",
        root,
        epoch,
        ",".join(changed) or "<none>",
        ",".join(removed) or "<none>",
        ",".join(added) or "<none>",
        kept,
    )
    return {"changed": changed, "removed": removed, "added": added, "kept": kept}


__all__ = ["reconcile_configuration"]
