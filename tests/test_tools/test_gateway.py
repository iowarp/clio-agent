"""
Tests for CLIO MCP Gateway

Tests gateway routing with namespaced tool names using
in-memory Client(gateway) pattern.
"""

import json

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway, get_gateway


def _parse_result(result):
    """Extract dict from CallToolResult."""
    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {"raw": data}
    return {"raw": str(data)}


@pytest.mark.asyncio
async def test_gateway_has_namespaced_tools():
    """Test that gateway exposes HDF5 tools with 'hdf5_' prefix."""
    async with Client(gateway) as client:
        tools = await client.list_tools()
        tool_names = sorted([t.name for t in tools])
        assert "hdf5_list_datasets" in tool_names
        assert "hdf5_analyze_dataset" in tool_names
        assert "hdf5_check_compression" in tool_names
        assert "hdf5_optimize_chunking" in tool_names
        assert "hdf5_analyze_file" in tool_names


@pytest.mark.asyncio
async def test_gateway_tool_count():
    """Test that gateway has exactly 5 HDF5 tools."""
    async with Client(gateway) as client:
        tools = await client.list_tools()
        hdf5_tools = [t for t in tools if t.name.startswith("hdf5_")]
        assert len(hdf5_tools) == 5


@pytest.mark.asyncio
async def test_gateway_analyze_file(sample_hdf5):
    """Test calling analyze_file through gateway with namespace."""
    async with Client(gateway) as client:
        result = await client.call_tool("hdf5_analyze_file", {"filepath": sample_hdf5})
        data = _parse_result(result)

        assert "error" not in data
        assert data["total_datasets"] == 3
        assert data["total_groups"] == 1


@pytest.mark.asyncio
async def test_gateway_list_datasets(sample_hdf5):
    """Test calling list_datasets through gateway with namespace."""
    async with Client(gateway) as client:
        result = await client.call_tool("hdf5_list_datasets", {"filepath": sample_hdf5})
        data = _parse_result(result)

        assert "error" not in data
        assert data["total_datasets"] == 3


@pytest.mark.asyncio
async def test_gateway_check_compression(sample_hdf5):
    """Test calling check_compression through gateway with namespace."""
    async with Client(gateway) as client:
        result = await client.call_tool("hdf5_check_compression", {"filepath": sample_hdf5})
        data = _parse_result(result)

        assert "error" not in data
        assert data["compressed_datasets"] == 1


@pytest.mark.asyncio
async def test_get_gateway_helper():
    """Test get_gateway() returns the gateway instance."""
    gw = get_gateway()
    assert gw is gateway
    assert gw.name == "clio-gateway"


@pytest.mark.asyncio
async def test_gateway_error_handling():
    """Test that errors propagate correctly through gateway."""
    async with Client(gateway) as client:
        result = await client.call_tool(
            "hdf5_analyze_file", {"filepath": "/nonexistent/file.h5"}
        )
        data = _parse_result(result)
        assert "error" in data
