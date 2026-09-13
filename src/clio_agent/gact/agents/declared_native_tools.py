"""Resolve declaration-scoped in-process tools outside the MCP gateway."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_tools import build_create_a2ui_surface_tool
from clio_agent.gact.agents import toolset_inventory
from clio_agent.gact.ask_user_tool import build_ask_user_tool
from clio_agent.gact.memory_tools import (
    build_memory_context_frame_tool,
    build_memory_search_tool,
    build_memory_summary_tool,
)


def resolve_declared_native_tools(
    agent_def: Any, sources: dict[str, str]
) -> tuple[list[str], dict[str, Any], list[str]]:
    """Return requested names, native implementations, and gateway remainder."""

    requested = [str(name).strip() for name in agent_def.tools if str(name).strip()]
    available: dict[str, Any] = {}
    builders = {
        "ask_user": lambda: build_ask_user_tool(agent_def),
        "create_a2ui_surface": build_create_a2ui_surface_tool,
        "memory_search_sessions": lambda: build_memory_search_tool(agent_def),
        "memory_read_session_summary": lambda: build_memory_summary_tool(agent_def),
        "memory_read_context_frame": lambda: build_memory_context_frame_tool(agent_def),
    }
    for name, build in builders.items():
        if name not in requested:
            continue
        available[name] = build()
        toolset_inventory.register_tool_source(sources, name, "native-declared")
    gateway_requested = [name for name in requested if name not in available]
    return requested, available, gateway_requested


__all__ = ["resolve_declared_native_tools"]
