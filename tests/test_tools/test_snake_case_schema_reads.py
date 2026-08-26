"""Production schema reads use the mcp-2 snake_case names (finding #5).

fastmcp-4 / mcp-2 renamed ``Tool.inputSchema`` -> ``Tool.input_schema`` (and
``outputSchema`` -> ``output_schema``), keeping camelCase only as a deprecated,
warn-once compatibility shim. Production code must read the snake_case names off
live Tool objects so it never trips the deprecation — proven here with the
camelCase compat shim DISABLED and ``FastMCPDeprecationWarning`` promoted to an
error on the touched read paths. camelCase survives ONLY as a wire-mapping key.
"""

from __future__ import annotations

import warnings

import fastmcp
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import FastMCPDeprecationWarning

from clio_agent.gact.routes.catalog_runtime_tools import runtime_tool_value
from clio_agent.tools.mcp_executor import _tool_input_schema


@pytest.fixture()
def _camelcase_shim_off():
    """Disable FastMCP's camelCase compat shim for the duration of a test."""
    previous = fastmcp.settings.mcp_camelcase_compat
    fastmcp.settings.mcp_camelcase_compat = False
    try:
        yield
    finally:
        fastmcp.settings.mcp_camelcase_compat = previous


async def _live_tool() -> object:
    """Return a real fastmcp-4 Tool object (as the executor sees it)."""
    server = FastMCP("schema-src")

    @server.tool
    def demo(value: str) -> str:
        """Echo."""
        return value

    async with Client(server) as client:
        tools = await client.list_tools()
    return tools[0]


@pytest.mark.asyncio
async def test_camelcase_alias_is_gone_with_shim_off(_camelcase_shim_off) -> None:
    """Sanity: with the shim off the deprecated camelCase alias is absent."""
    tool = await _live_tool()
    with pytest.raises(AttributeError):
        _ = tool.inputSchema  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tool_input_schema_reads_snake_without_deprecation(
    _camelcase_shim_off,
) -> None:
    """The executor's central schema reader is snake-only (no deprecated access)."""
    tool = await _live_tool()
    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=FastMCPDeprecationWarning)
        schema = _tool_input_schema(tool)
    assert "value" in schema.get("properties", {})


@pytest.mark.asyncio
async def test_catalog_projection_reads_snake_without_deprecation(
    _camelcase_shim_off,
) -> None:
    """The gact catalog projection reads snake-first off a live Tool."""
    tool = await _live_tool()
    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=FastMCPDeprecationWarning)
        input_schema = runtime_tool_value(tool, "input_schema", "inputSchema")
        output_schema = runtime_tool_value(tool, "output_schema", "outputSchema")
    assert isinstance(input_schema, dict)
    assert "value" in input_schema.get("properties", {})
    # output_schema may be None for a str-returning tool; the read must not warn.
    assert output_schema is None or isinstance(output_schema, dict)


def test_wire_mapping_camelcase_key_still_accepted() -> None:
    """camelCase survives as a wire-mapping key (persisted/legacy rows)."""
    wire_row = {"inputSchema": {"properties": {"x": {"type": "string"}}}}
    schema = _tool_input_schema(wire_row)
    assert "x" in schema.get("properties", {})
