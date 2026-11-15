"""The producer refuses a ``clio.mcp-app.v1`` whose call cannot open a view."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.a2ui_producer._mcp_app_reference import validate_mcp_app_components

DEFINITIONS = {
    "clock_open_timeline": SimpleNamespace(meta={"ui": {"resourceUri": "ui://clock/view"}}),
    "clock_set_time": SimpleNamespace(meta={}),
}


def _app(definitions: dict[str, Any] | None) -> Any:
    executor = SimpleNamespace(get_all_tool_definitions=lambda: definitions)
    agent = SimpleNamespace(_active_tool_executor=lambda: executor)
    if definitions is None:
        agent = SimpleNamespace()
    return SimpleNamespace(state=SimpleNamespace(agent=agent))


def _component(tool: str) -> dict[str, Any]:
    return {"id": "app", "component": "clio.mcp-app.v1", "server": "clock", "tool": tool}


def test_a_view_opening_tool_passes_and_other_components_are_ignored() -> None:
    components = [_component("open_timeline"), {"id": "root", "component": "Column"}]
    assert validate_mcp_app_components(_app(DEFINITIONS), components) is None
    assert validate_mcp_app_components(_app(None), [{"id": "t", "component": "Text"}]) is None


def test_a_missing_tool_names_the_servers_view_tools() -> None:
    outcome = validate_mcp_app_components(_app(DEFINITIONS), [_component("nope")])
    assert outcome is not None
    assert outcome["reason"] == "a2ui_mcp_app_tool_not_found"
    assert "is not a tool of that server" in outcome["detail"]
    assert "open_timeline" in outcome["detail"]
    assert outcome["hint"]


def test_a_tool_without_a_view_is_refused() -> None:
    outcome = validate_mcp_app_components(_app(DEFINITIONS), [_component("set_time")])
    assert outcome is not None and "does not open a view" in outcome["detail"]


def test_no_tool_executor_is_a_typed_refusal() -> None:
    outcome = validate_mcp_app_components(_app(None), [_component("open_timeline")])
    assert outcome is not None and outcome["reason"] == "a2ui_mcp_app_tool_not_found"
