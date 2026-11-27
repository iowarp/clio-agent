"""A person's own MCP servers (user and workspace ``mcp.yaml``) in a root session's agent.

The session's fleet declares them beside its plugin servers
(:func:`clio_agent.tools.mcp_config.load_mcp_servers`). A root agent takes each whole,
as it takes a plugin's server: its tools reach the agent, it is summonable, and a tool
it adds later reaches it too (:mod:`clio_agent.gact.agents.live_tool_refresh`).
``always_load`` decides only how its tools are offered: listed (by size, like a plugin
server that declares nothing), or else behind ``search_tools`` until the agent or the
person asks for them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def person_server_rows(declared_specs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The executor's declared person servers, as the rows a plugin server has."""

    rows: dict[str, dict[str, Any]] = {}
    for namespace, spec in declared_specs.items():
        source = str(getattr(spec, "source", "") or "")
        if source.startswith("plugin:"):
            continue
        rows[namespace] = {
            "plugin": "",
            "description": "",
            "defer": None if bool(getattr(spec, "always_load", False)) else True,
            "source": source,
        }
    return rows


def executor_person_servers(tool_executor: Any) -> dict[str, dict[str, Any]]:
    """:func:`person_server_rows` of ``tool_executor``'s declared fleet."""

    return person_server_rows(getattr(tool_executor, "_clio_namespace_specs", None) or {})


def attach_person_servers(
    tool_executor: Any,
    gateway_tools: dict[str, Any],
    mount_failures: dict[str, str],
    requested_tools: list[str],
) -> set[str]:
    """Prepare the person's servers and merge their tools into the root agent's.

    Returns the namespaces taken whole. A server that fails to prepare is recorded in
    ``mount_failures`` with its typed reason; the agent's tool inventory reports it.
    """

    from clio_agent.gact.mcp_background_prepare import prepare_for_turn  # noqa: PLC0415

    declared_specs = getattr(tool_executor, "_clio_namespace_specs", None) or {}
    namespaces = set(person_server_rows(declared_specs))
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
        logger.warning("person_mcp_mount_failed namespace=%s reason=%s", namespace, reason)

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
    return namespaces - set(failures)
