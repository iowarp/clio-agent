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
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from clio_agent.runtime import trace

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


def rebind_namespace(agent: Any, namespace: str, declaration: Mapping[str, Any]) -> list[str]:
    """Rebind ``namespace`` in every resident fleet; return the workspace roots touched."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415
    from clio_agent.tools.gateway import (  # noqa: PLC0415
        build_gateway,
        namespace_direct_factories,
        namespace_proxies,
    )
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
        spec = rebound_spec(specs[namespace], declaration, namespace)
        gateway = build_gateway({namespace: spec}, cwd=root or None)
        proxy = namespace_proxies(gateway).get(namespace)
        inner = getattr(executor, "_async_executor", None)
        specs[namespace] = spec
        if inner is not None:
            inner_specs = getattr(inner, "_clio_namespace_specs", None)
            if isinstance(inner_specs, dict):
                inner_specs[namespace] = spec
            if proxy is not None:
                inner._namespace_servers[namespace] = proxy
            factories = getattr(inner, "_clio_namespace_direct_factories", None)
            factory = namespace_direct_factories(gateway).get(namespace)
            if isinstance(factories, dict) and factory is not None:
                factories[namespace] = factory
        reset = getattr(executor, "reset_namespace", None)
        if callable(reset):
            reset(namespace)
        mcp_prepare.forget(namespace)
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
