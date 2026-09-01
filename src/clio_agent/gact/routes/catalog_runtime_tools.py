"""Projection of the live agent executor's preloaded MCP tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI

from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title
from clio_agent.gact.catalog import (
    _tool_owner_for_catalog,
    _tool_tags_for_catalog,
    _tool_visible_to_for_catalog,
)


def agent_runtime_tool_rows(app: FastAPI) -> list[dict[str, Any]]:
    """Project preloaded MCP definitions without spawning or reconnecting servers."""

    executor = getattr(getattr(app.state, "agent", None), "tool_executor", None)
    get_definitions = getattr(executor, "get_all_tool_definitions", None)
    if not callable(get_definitions):
        return []
    definitions = get_definitions()
    if not isinstance(definitions, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for key, tool in definitions.items():
        tool_name = str(runtime_tool_value(tool, "name") or key).strip()
        if not tool_name:
            continue
        namespace, separator, _ = tool_name.partition("_")
        rows.append(
            {
                "id": tool_name,
                "name": tool_name,
                "title": mcp_tool_title(tool),
                "description": str(runtime_tool_value(tool, "description") or ""),
                "server_id": f"mcp_{namespace}" if separator else "",
                "source": "agent_runtime_mcp",
                "input_schema": runtime_tool_value(tool, "input_schema", "inputSchema") or {},
                "output_schema": runtime_tool_value(tool, "output_schema", "outputSchema") or {},
                "owner": _tool_owner_for_catalog(tool_name),
                "tags": _tool_tags_for_catalog(tool_name),
                "visible_to": _tool_visible_to_for_catalog(tool_name),
            }
        )
    return rows


def runtime_tool_value(tool: Any, *names: str) -> Any:
    """Read the first populated field from an MCP model object or mapping."""

    for name in names:
        if isinstance(tool, Mapping) and name in tool:
            return tool[name]
        value = getattr(tool, name, None)
        if value is not None:
            return value
    return None
