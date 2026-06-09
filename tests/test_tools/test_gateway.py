"""
Tests for CLIO MCP Gateway

Tests gateway routing with namespaced tool names using
in-memory Client(gateway) pattern.
"""

import json

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import _mount_with_namespace, gateway, get_gateway


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


def test_mount_helper_uses_namespace_when_supported():
    """Gateway helper should use FastMCP namespace API when available."""
    calls = []

    class Parent:
        def mount(self, server, namespace=None):
            calls.append((server, namespace))

    server = object()
    _mount_with_namespace(Parent(), server, "hdf5")

    assert calls == [(server, "hdf5")]


def test_mount_helper_falls_back_to_prefix():
    """Installed FastMCP 2.x exposes prefix, so the helper must preserve it."""
    calls = []

    class Parent:
        def mount(self, server, prefix=None):
            calls.append((server, prefix))

    server = object()
    _mount_with_namespace(Parent(), server, "parquet")

    assert calls == [(server, "parquet")]


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
async def test_gateway_preserves_stable_tool_names():
    """Gateway modernization must not rename existing HDF5/Parquet tools."""
    expected = {
        "hdf5_list_datasets",
        "hdf5_analyze_dataset",
        "hdf5_check_compression",
        "hdf5_optimize_chunking",
        "hdf5_analyze_file",
        "parquet_analyze_schema",
        "parquet_query_data",
        "parquet_compute_statistics",
    }
    async with Client(gateway) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

    assert expected <= tool_names


@pytest.mark.asyncio
async def test_gateway_tool_count():
    """Gateway exposes every tool from the mounted hdf5_server under the
    hdf5_ namespace. Five of those are used by DataExpert; the rest are
    used by HDF5Expert. Per-expert curation happens in the expert layer,
    not on the gateway.
    """
    async with Client(gateway) as client:
        tools = await client.list_tools()
        hdf5_tools = [t for t in tools if t.name.startswith("hdf5_")]
        assert len(hdf5_tools) == 11


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
