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
) -> None:
    """Mount always-load namespaces and merge their tools into root state."""

    from clio_agent.gact.mcp_readiness import mount_failure_reason, mount_namespace_for_session

    declared_specs = getattr(tool_executor, "_clio_namespace_specs", None) or {}
    namespaces = {
        namespace
        for namespace, spec in declared_specs.items()
        if bool(getattr(spec, "always_load", False))
    }
    prepared = getattr(tool_executor, "is_namespace_prepared", None)
    for namespace in sorted(namespaces):
        if callable(prepared) and prepared(namespace):
            continue
        try:
            mount_namespace_for_session(tool_executor, namespace, declared_specs[namespace])
        except Exception as exc:  # noqa: BLE001 - one optional service cannot brick the agent
            mount_failures[namespace] = mount_failure_reason(exc)
            logger.warning(
                "always_load_mcp_mount_failed namespace=%s reason=%s error=%s",
                namespace,
                mount_failures[namespace],
                exc,
            )

    prefixes = tuple(f"{namespace}_" for namespace in sorted(namespaces))
    if not prefixes:
        return
    attached = {
        name: tool
        for tool in tool_executor.to_dspy_tools()
        for name in [str(getattr(tool, "name", "") or "")]
        if name.startswith(prefixes)
    }
    gateway_tools.update(attached)
    requested_tools.extend(name for name in attached if name not in requested_tools)
