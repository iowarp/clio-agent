"""Resolve declaration-scoped in-process tools outside the MCP gateway."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_delete_a2ui_surface_tool,
    build_update_a2ui_components_tool,
    build_update_a2ui_data_model_tool,
)
from clio_agent.gact.agents import toolset_inventory
from clio_agent.gact.ask_user_tool import build_ask_user_tool
from clio_agent.gact.memory_tools import (
    build_memory_context_frame_tool,
    build_memory_search_tool,
    build_memory_summary_tool,
)
from clio_agent.gact.view_image_tool import build_view_image_tool


def declared_view_image_capability(config: Any) -> bool:
    """Return the evidenced image capability for one compiled agent profile."""

    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    if app is None:
        return bool(getattr(config, "supports_vision", False))
    from clio_agent.gact.providers.config import _vision_capability  # noqa: PLC0415

    supported, _reason = _vision_capability(
        app,
        str(getattr(config, "provider_id", "") or ""),
        str(getattr(config, "model", "") or ""),
    )
    return supported


def resolve_declared_native_tools(
    agent_def: Any,
    sources: dict[str, str],
    *,
    supports_vision: bool = False,
) -> tuple[list[str], dict[str, Any], list[str]]:
    """Return requested names, native implementations, and gateway remainder."""

    declared = [str(name).strip() for name in agent_def.tools if str(name).strip()]
    requested = [name for name in declared if name != "view_image" or supports_vision]
    available: dict[str, Any] = {}
    builders = {
        "ask_user": lambda: build_ask_user_tool(agent_def),
        "create_a2ui_surface": build_create_a2ui_surface_tool,
        "update_a2ui_components": build_update_a2ui_components_tool,
        "update_a2ui_data_model": build_update_a2ui_data_model_tool,
        "delete_a2ui_surface": build_delete_a2ui_surface_tool,
        "memory_search_sessions": lambda: build_memory_search_tool(agent_def),
        "memory_read_session_summary": lambda: build_memory_summary_tool(agent_def),
        "memory_read_context_frame": lambda: build_memory_context_frame_tool(agent_def),
    }
    if supports_vision:
        builders["view_image"] = build_view_image_tool
    for name, build in builders.items():
        if name not in requested:
            continue
        available[name] = build()
        toolset_inventory.register_tool_source(sources, name, "native-declared")
    gateway_requested = [name for name in requested if name not in available]
    return requested, available, gateway_requested


__all__ = ["declared_view_image_capability", "resolve_declared_native_tools"]
