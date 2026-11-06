"""Root-session attachment for user-configured always-load MCP services."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def attach_always_load_tools(
    tool_executor: Any,
    gateway_tools: dict[str, Any],
    mount_failures: dict[str, str],
    requested_tools: list[str],
) -> set[str]:
    """Mount always-load namespaces and merge their tools into root state.

    Returns the always-load namespaces, which the root agent takes whole (so a tool
    their server adds later reaches it: :mod:`clio_agent.gact.agents.live_tool_refresh`).
    """

    from clio_agent.gact.mcp_background_prepare import prepare_for_turn

    declared_specs = getattr(tool_executor, "_clio_namespace_specs", None) or {}
    namespaces = {
        namespace
        for namespace, spec in declared_specs.items()
        if bool(getattr(spec, "always_load", False))
    }
    listed = {
        str(getattr(tool, "name", "") or "").partition("_")[0]
        for tool in tool_executor.to_dspy_tools()
    }
    cold = {namespace: declared_specs[namespace] for namespace in sorted(namespaces - listed)}
    # Only missing definitions wait. Execution startup continues independently when
    # a valid cached listing is already present, including for non-uv launchers.
    failures = prepare_for_turn(tool_executor, cold)
    for namespace, reason in failures.items():
        mount_failures[namespace] = reason
        logger.warning("always_load_mcp_mount_failed namespace=%s reason=%s", namespace, reason)

    prefixes = tuple(f"{namespace}_" for namespace in sorted(namespaces))
    if not prefixes:
        return namespaces
    attached = {
        name: tool
        for tool in tool_executor.to_dspy_tools()
        for name in [str(getattr(tool, "name", "") or "")]
        if name.startswith(prefixes)
    }
    gateway_tools.update(attached)
    requested_tools.extend(name for name in attached if name not in requested_tools)
    return namespaces
