"""Every CLIO built-in tool, by name, and the one place that builds them.

A plugin claims a built-in tool with ``native_tool("name")`` in its
``ai.iowarp.clio/tools/``; an agent gets exactly the built-in tools its resolved
plugins claim. Nothing is attached implicitly. This registry maps each claimable
name to how it is built:

* gateway tools (``fs_*``, ``shell_bash``) come from the in-process MCP gateway;
* every other name has a builder here, or is built by the agent module because it
  needs the agent's own runtime (``load_skill``, ``spawn_skill_task``,
  ``spawn_agent_task``, ``spawn_agents_parallel``, ``draft_alternatives``).

Some tools exist only when reality allows: image and PDF viewing need a model
that accepts them, A2UI producers need a resolved catalog, NDP operations need a
connected NDP source, knowledge tools need a connected knowledge source. A
claimed tool withheld for such a reason is returned in ``withheld`` with a typed
reason, never dropped silently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from clio_agent.gact.agents import toolset_inventory
from clio_agent.gact.task_declarations import (
    TASK_COMPATIBILITY_ALIASES,
    TASK_TOOL_NAMES,
    build_ask_user_tool,
    build_task_compatibility_tools,
    build_task_tools,
)
from clio_agent.tools.catalog import TOOL_CATALOG

#: Built through the in-process MCP gateway (fs and shell servers).
GATEWAY_TOOLS: frozenset[str] = frozenset(TOOL_CATALOG)

#: Built by the agent module from its own runtime (skills, spawns, drafts).
AGENT_RUNTIME_TOOLS: frozenset[str] = frozenset(
    {
        "load_skill",
        "spawn_skill_task",
        "spawn_agent_task",
        "spawn_agents_parallel",
        "draft_alternatives",
    }
)

_A2UI_PRODUCERS = (
    "create_a2ui_surface",
    "update_a2ui_components",
    "update_a2ui_data_model",
    "delete_a2ui_surface",
    "inspect_a2ui_surface",
    "capture_a2ui_surface",
    "publish_dashboard_report",
)
_RESOURCE_TOOLS = (
    "workspace_resource_list",
    "workspace_resource_inspect",
    "workspace_resource_wait",
    "workspace_resource_read",
    "workspace_resource_search",
    "workspace_resource_structure",
)
_NDP_DISCOVERY = ("connected_data_search_ndp", "connected_data_ndp_organizations")
_NDP_ENDPOINT = (
    "connected_data_ndp_endpoint_connect",
    "connected_data_ndp_endpoint_operations",
    "connected_data_ndp_upload_artifact",
    "connected_data_ndp_endpoint_request",
)
_KNOWLEDGE = (
    "knowledge_open",
    "knowledge_search",
    "knowledge_related",
    "knowledge_create",
    "knowledge_edit",
)
_CRON = ("cron_create", "cron_list", "cron_delete")


def _singles(agent_def: Any) -> dict[str, Callable[[], Any]]:
    from clio_agent.gact.a2ui_producer import (  # noqa: PLC0415
        build_capture_a2ui_surface_tool,
        build_create_a2ui_surface_tool,
        build_delete_a2ui_surface_tool,
        build_inspect_a2ui_surface_tool,
        build_publish_dashboard_report_tool,
        build_update_a2ui_components_tool,
        build_update_a2ui_data_model_tool,
    )
    from clio_agent.gact.action_cards import build_raise_alert_card_tool  # noqa: PLC0415
    from clio_agent.gact.artifacts.proposals import build_create_artifact_tool  # noqa: PLC0415
    from clio_agent.gact.autonomous_loop import build_loop_wakeup_tool  # noqa: PLC0415
    from clio_agent.gact.document_tools import (  # noqa: PLC0415
        build_prepare_document_runtime_tool,
        build_prepare_document_tool,
        build_prepare_execution_runtime_tool,
    )
    from clio_agent.gact.flow_tools import (  # noqa: PLC0415
        build_run_flow_tool,
        build_run_workflow_tool,
    )
    from clio_agent.gact.goal import build_goal_status_tool  # noqa: PLC0415
    from clio_agent.gact.goal_control import (  # noqa: PLC0415
        build_goal_wait_tool,
        build_set_goal_tool,
    )
    from clio_agent.gact.loop_control import build_set_loop_tool  # noqa: PLC0415
    from clio_agent.gact.memory.tool_builders import BUILDERS as MEMORY_BUILDERS  # noqa: PLC0415
    from clio_agent.gact.plan_mode import build_plan_exit_tool  # noqa: PLC0415
    from clio_agent.gact.plugin_authoring import build_make_plugin_tool  # noqa: PLC0415
    from clio_agent.gact.recall_context_tool import build_recall_context_tool  # noqa: PLC0415
    from clio_agent.gact.search_assistant import (  # noqa: PLC0415
        build_clio_search_read_tool,
        build_clio_search_tool,
    )
    from clio_agent.gact.session_recall_tools import (  # noqa: PLC0415
        build_session_context_frame_tool,
        build_session_search_tool,
        build_session_summary_tool,
    )
    from clio_agent.gact.ssh_tools import build_ssh_execute_tool  # noqa: PLC0415
    from clio_agent.gact.storage.connect_tool import (  # noqa: PLC0415
        build_connected_data_connect_tool,
        build_connected_data_index_tool,
    )
    from clio_agent.gact.storage.download_tool import (  # noqa: PLC0415
        build_connected_data_download_tool,
    )
    from clio_agent.gact.storage.setup_tool import (  # noqa: PLC0415
        build_connected_data_open_tool,
        build_connected_data_status_tool,
        build_connected_data_write_tool,
    )
    from clio_agent.gact.todos import build_write_todos_tool  # noqa: PLC0415
    from clio_agent.gact.view_image_tool import build_view_image_tool  # noqa: PLC0415
    from clio_agent.gact.view_pdf_tool import build_view_pdf_tool  # noqa: PLC0415
    from clio_agent.gact.weather_tools import build_weather_forecast_tool  # noqa: PLC0415
    from clio_agent.providers.model_discovery import (  # noqa: PLC0415
        build_refresh_provider_models_tool,
    )

    return {
        "ask_user": lambda: build_ask_user_tool(agent_def),
        "create_artifact": lambda: build_create_artifact_tool(agent_def),
        "plan_exit": lambda: build_plan_exit_tool(agent_def),
        "write_todos": lambda: build_write_todos_tool(agent_def),
        "raise_alert_card": lambda: build_raise_alert_card_tool(agent_def),
        "recall_context": build_recall_context_tool,
        "loop_wakeup": build_loop_wakeup_tool,
        "set_loop": build_set_loop_tool,
        "set_goal": build_set_goal_tool,
        "wait_for": build_goal_wait_tool,
        "goal_status": build_goal_status_tool,
        "refresh_provider_models": build_refresh_provider_models_tool,
        "view_image": build_view_image_tool,
        "view_pdf": build_view_pdf_tool,
        "prepare_document_runtime": build_prepare_document_runtime_tool,
        "prepare_execution_runtime": build_prepare_execution_runtime_tool,
        "prepare_document": build_prepare_document_tool,
        "ssh_execute": build_ssh_execute_tool,
        "clio_search": build_clio_search_tool,
        "clio_search_read": build_clio_search_read_tool,
        **MEMORY_BUILDERS,
        "session_search": lambda: build_session_search_tool(agent_def),
        "session_read_summary": lambda: build_session_summary_tool(agent_def),
        "session_read_context_frame": lambda: build_session_context_frame_tool(agent_def),
        "create_a2ui_surface": build_create_a2ui_surface_tool,
        "update_a2ui_components": build_update_a2ui_components_tool,
        "update_a2ui_data_model": build_update_a2ui_data_model_tool,
        "delete_a2ui_surface": build_delete_a2ui_surface_tool,
        "inspect_a2ui_surface": build_inspect_a2ui_surface_tool,
        "capture_a2ui_surface": build_capture_a2ui_surface_tool,
        "publish_dashboard_report": build_publish_dashboard_report_tool,
        "get_weather_forecast": build_weather_forecast_tool,
        "connected_data_download": build_connected_data_download_tool,
        "connected_data_status": build_connected_data_status_tool,
        "connected_data_connect": build_connected_data_connect_tool,
        "connected_data_index": build_connected_data_index_tool,
        "connected_data_open": build_connected_data_open_tool,
        "connected_data_write": build_connected_data_write_tool,
        "run_flow": build_run_flow_tool,
        "run_workflow": build_run_workflow_tool,
        "make_plugin": build_make_plugin_tool,
    }


_SINGLE_NAMES = frozenset(
    {
        "ask_user",
        "create_artifact",
        "plan_exit",
        "write_todos",
        "raise_alert_card",
        "recall_context",
        "loop_wakeup",
        "set_loop",
        "set_goal",
        "wait_for",
        "goal_status",
        "refresh_provider_models",
        "view_image",
        "view_pdf",
        "prepare_document_runtime",
        "prepare_execution_runtime",
        "prepare_document",
        "ssh_execute",
        "clio_search",
        "clio_search_read",
        "session_search",
        "session_read_summary",
        "session_read_context_frame",
        "memory_search",
        "memory_read",
        "create_memory",
        "update_memory",
        "forget_memory",
        "propose_memory_change",
        "get_weather_forecast",
        "connected_data_download",
        "connected_data_status",
        "connected_data_connect",
        "connected_data_index",
        "connected_data_open",
        "connected_data_write",
        "run_flow",
        "run_workflow",
        "make_plugin",
        *_A2UI_PRODUCERS,
    }
)

#: Every name a plugin may claim with ``native_tool``.
NATIVE_TOOL_NAMES: frozenset[str] = frozenset(
    GATEWAY_TOOLS
    | AGENT_RUNTIME_TOOLS
    | _SINGLE_NAMES
    | TASK_TOOL_NAMES
    | TASK_COMPATIBILITY_ALIASES
    | set(_RESOURCE_TOOLS + _NDP_DISCOVERY + _NDP_ENDPOINT + _KNOWLEDGE + _CRON)
)


def declared_view_image_capability(config: Any) -> bool:
    """Return the evidenced image capability for one compiled agent profile."""

    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    if app is None:
        from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
            get_effective_capabilities,
        )

        effective = get_effective_capabilities(
            str(getattr(config, "provider_id", "") or getattr(config, "provider", "") or ""),
            str(getattr(config, "api_base", "") or ""),
            str(getattr(config, "model", "") or ""),
        )
        modalities = effective.input_modalities.value or frozenset()
        return "image" in modalities
    from clio_agent.gact.providers.config import _vision_capability  # noqa: PLC0415

    supported, _reason = _vision_capability(
        app,
        str(getattr(config, "provider_id", "") or ""),
        str(getattr(config, "model", "") or ""),
    )
    return supported


def declared_view_pdf_capability(config: Any) -> bool:
    """Return the evidenced PDF-document capability for one compiled agent profile."""

    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    if app is None:
        return bool(getattr(config, "supports_pdf", False))
    from clio_agent.gact.providers.config import _pdf_capability  # noqa: PLC0415

    supported, _reason = _pdf_capability(
        app,
        str(getattr(config, "provider_id", "") or ""),
        str(getattr(config, "model", "") or ""),
    )
    return supported


def declared_native_capabilities(config: Any) -> dict[str, bool]:
    """The evidenced model capabilities that gate image, PDF and capture tools."""

    return {
        "supports_vision": declared_view_image_capability(config),
        "supports_pdf": declared_view_pdf_capability(config),
    }


def _pick(tools: Iterable[Any], wanted: set[str]) -> dict[str, Any]:
    return {tool.name: tool for tool in tools if tool.name in wanted}


def build_native_tools(
    agent_def: Any,
    requested: Iterable[str],
    sources: dict[str, str],
    *,
    supports_vision: bool = False,
    supports_pdf: bool = False,
    a2ui_catalogs: bool = True,
    describe_all: bool = False,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """Build the claimed built-in tools.

    Returns ``(available, gateway_requested, withheld)``: tools built here, the
    names the MCP gateway provides, and claimed names withheld with a reason.
    Names in :data:`AGENT_RUNTIME_TOOLS` are left to the agent module.
    ``describe_all`` builds the NDP tools without a connected source, for the
    product catalog that documents them (never for an agent's toolset).
    """

    wanted = {str(name) for name in requested} & NATIVE_TOOL_NAMES
    withheld: dict[str, str] = {}
    if not supports_vision:
        for name in ("view_image", "capture_a2ui_surface"):
            if name in wanted:
                withheld[name] = "model_lacks_image_input"
    if not supports_pdf and "view_pdf" in wanted:
        withheld["view_pdf"] = "model_lacks_pdf_input"
    if not a2ui_catalogs:
        for name in _A2UI_PRODUCERS:
            if name in wanted:
                withheld.setdefault(name, "no_a2ui_catalog_resolved")
    from clio_agent.gact.memory.policy import withheld_memory_tools  # noqa: PLC0415

    for name, reason in withheld_memory_tools(wanted).items():
        withheld.setdefault(name, reason)  # memory off, a read-only session, recall off
    wanted -= set(withheld)
    available: dict[str, Any] = {}
    if wanted & TASK_TOOL_NAMES:
        available.update(_pick(build_task_tools(), wanted))
    if wanted & TASK_COMPATIBILITY_ALIASES:
        available.update(_pick(build_task_compatibility_tools(sorted(wanted)), wanted))
    if wanted & set(_CRON):
        from clio_agent.gact.cron_tools import build_cron_tools  # noqa: PLC0415

        available.update(_pick(build_cron_tools(), wanted))
    if wanted & set(_RESOURCE_TOOLS):
        from clio_agent.gact.resource_tools import build_resource_tools  # noqa: PLC0415

        available.update(_pick(build_resource_tools(agent_def), wanted))
    if wanted & set(_NDP_DISCOVERY + _NDP_ENDPOINT):
        from clio_agent.gact.storage.ndp_endpoint_tools import (
            build_ndp_endpoint_tools,  # noqa: PLC0415
        )
        from clio_agent.gact.storage.ndp_tool_activation import (  # noqa: PLC0415
            session_ndp_tools_enabled,
        )
        from clio_agent.gact.storage.ndp_tools import build_ndp_discovery_tools  # noqa: PLC0415

        if describe_all or session_ndp_tools_enabled():
            available.update(
                _pick([*build_ndp_discovery_tools(), *build_ndp_endpoint_tools()], wanted)
            )
        else:
            # Connecting is how an empty workspace acquires its first Endpoint.
            available.update(_pick(build_ndp_endpoint_tools(setup_only=True), wanted))
        for name in wanted & set(_NDP_DISCOVERY + _NDP_ENDPOINT):
            if name not in available:
                withheld[name] = "ndp_source_not_connected"
    if wanted & set(_KNOWLEDGE):
        from clio_agent.gact.storage.knowledge_tools import build_knowledge_tools  # noqa: PLC0415

        available.update(_pick(build_knowledge_tools(), wanted))
        for name in wanted & set(_KNOWLEDGE):
            if name not in available:
                withheld[name] = "knowledge_source_not_connected"
    singles = _singles(agent_def)
    for name in sorted(wanted & _SINGLE_NAMES):
        available[name] = singles[name]()
    for name in available:
        toolset_inventory.register_tool_source(sources, name, "native")
    gateway_requested = sorted(wanted & GATEWAY_TOOLS)
    return available, gateway_requested, withheld


__all__ = [
    "AGENT_RUNTIME_TOOLS",
    "GATEWAY_TOOLS",
    "NATIVE_TOOL_NAMES",
    "build_native_tools",
    "declared_native_capabilities",
    "declared_view_image_capability",
    "declared_view_pdf_capability",
]
