"""Tests for deriving the tool catalog from connected declared-MCP namespaces.

The catalog is the static built-ins merged with entries derived from the
connected MCP namespaces; visibility comes from each pack expert's ``tools:``
list. These tests are hermetic: a tiny in-process FastMCP is wrapped as a
declared proxy (no subprocess, no network).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.server import create_proxy

from clio_agent.tools.catalog import TOOL_CATALOG, ToolCatalogEntry
from clio_agent.tools.gateway import build_gateway, build_tool_catalog
from clio_agent.tools.mcp_config import MCPServerSpec


@dataclass
class _Expert:
    id: str
    tools: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def declared_server() -> FastMCP:
    """A tiny in-process FastMCP standing in for a declared MCP server."""
    server = FastMCP("declared-demo")

    @server.tool(tags={"alpha", "beta"})
    def ping(text: str) -> str:
        """Echo the text back."""
        return text

    return server


def _in_process_factory(declared: FastMCP):
    def factory(_spec: MCPServerSpec) -> FastMCP:
        return create_proxy(declared)

    return factory


def _gateway_with_demo(declared_server: FastMCP) -> FastMCP:
    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    return build_gateway({"demo": spec}, proxy_factory=_in_process_factory(declared_server))


def test_declared_tool_in_catalog_with_namespace_owner(declared_server: FastMCP):
    gw = _gateway_with_demo(declared_server)
    catalog = build_tool_catalog(gw)
    assert "demo_ping" in catalog
    entry = catalog["demo_ping"]
    assert entry.owner == "demo"
    assert "demo" in entry.tags
    assert "alpha" in entry.tags and "beta" in entry.tags
    assert "demo" in entry.visible_to
    # static built-in entries survive the merge
    assert "shell_bash" in catalog
    assert catalog["shell_bash"] == TOOL_CATALOG["shell_bash"]


def test_declared_tool_visibility_from_expert_tools(declared_server: FastMCP):
    gw = _gateway_with_demo(declared_server)
    experts = [
        _Expert(id="data", tools=["demo_ping"]),
        _Expert(id="other", tools=["shell_bash"]),
    ]
    catalog = build_tool_catalog(gw, experts=experts)
    entry = catalog["demo_ping"]
    assert "data" in entry.visible_to
    assert "other" not in entry.visible_to
    # planner-visible by default
    assert entry.planner_visible is True
    assert "planner" in entry.visible_to


def test_expert_not_planner_visible_hides_from_planner(declared_server: FastMCP):
    gw = _gateway_with_demo(declared_server)
    experts = [
        _Expert(id="data", tools=["demo_ping"], metadata={"planner_visible": False}),
    ]
    catalog = build_tool_catalog(gw, experts=experts)
    entry = catalog["demo_ping"]
    assert "data" in entry.visible_to
    assert entry.planner_visible is False
    assert "planner" not in entry.visible_to


def test_static_catalog_entries_win_over_derived(declared_server: FastMCP):
    """A declared tool whose name collides with a static entry keeps the static one."""
    gw = _gateway_with_demo(declared_server)
    custom_static = dict(TOOL_CATALOG)
    custom_static["demo_ping"] = ToolCatalogEntry(
        name="demo_ping",
        owner="preexisting",
        tags=frozenset({"static"}),
        visible_to=frozenset({"preexisting"}),
    )
    catalog = build_tool_catalog(gw, static_catalog=custom_static)
    assert catalog["demo_ping"].owner == "preexisting"


def test_catalog_without_declared_gateway_equals_static():
    """build_tool_catalog(None) is the static catalog (default behavior)."""
    catalog = build_tool_catalog(None)
    assert catalog == dict(TOOL_CATALOG)
    assert catalog is not TOOL_CATALOG
