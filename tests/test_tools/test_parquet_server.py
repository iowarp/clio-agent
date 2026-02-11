"""
Tests for Parquet MCP Server

Tests all 3 Parquet tools using in-memory Client(parquet_server) pattern,
plus gateway integration tests for namespaced parquet_* tool access.
"""

import json

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.parquet_server import parquet_server


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


# --- analyze_schema tests ---


@pytest.mark.asyncio
async def test_analyze_schema_returns_columns(sample_parquet):
    """Test analyze_schema returns column names and types."""
    async with Client(parquet_server) as client:
        result = await client.call_tool("analyze_schema", {"filepath": sample_parquet})
        data = _parse_result(result)

        assert "error" not in data
        assert data["num_columns"] == 3
        col_names = [c["name"] for c in data["columns"]]
        assert "id" in col_names
        assert "temperature" in col_names
        assert "city" in col_names


@pytest.mark.asyncio
async def test_analyze_schema_returns_metadata(sample_parquet):
    """Test analyze_schema returns num_rows, num_row_groups, file_size."""
    async with Client(parquet_server) as client:
        result = await client.call_tool("analyze_schema", {"filepath": sample_parquet})
        data = _parse_result(result)

        assert "error" not in data
        assert data["num_rows"] == 100
        assert data["num_row_groups"] >= 1
        assert data["file_size_bytes"] > 0


@pytest.mark.asyncio
async def test_analyze_schema_nonexistent_file():
    """Test analyze_schema returns error for nonexistent file."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "analyze_schema", {"filepath": "/nonexistent/data.parquet"}
        )
        data = _parse_result(result)
        assert "error" in data


# --- query_data tests ---


@pytest.mark.asyncio
async def test_query_data_all_columns(sample_parquet):
    """Test query_data returns all columns when none specified."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "query_data", {"filepath": sample_parquet}
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["total_rows"] == 100
        assert len(data["columns"]) == 3
        assert "id" in data["columns"]
        assert "temperature" in data["columns"]
        assert "city" in data["columns"]
        assert len(data["rows"]) == 100


@pytest.mark.asyncio
async def test_query_data_specific_columns(sample_parquet):
    """Test query_data filters to specific columns."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "query_data", {"filepath": sample_parquet, "columns": "id,temperature"}
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["columns"] == ["id", "temperature"]
        # Verify rows only have the two columns
        for row in data["rows"]:
            assert set(row.keys()) == {"id", "temperature"}


@pytest.mark.asyncio
async def test_query_data_with_row_limit(sample_parquet):
    """Test query_data respects row_limit parameter."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "query_data", {"filepath": sample_parquet, "row_limit": 5}
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["total_rows"] == 100
        assert data["rows_returned"] == 5
        assert len(data["rows"]) == 5


@pytest.mark.asyncio
async def test_query_data_nonexistent_file():
    """Test query_data returns error for nonexistent file."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "query_data", {"filepath": "/nonexistent/data.parquet"}
        )
        data = _parse_result(result)
        assert "error" in data


# --- compute_statistics tests ---


@pytest.mark.asyncio
async def test_compute_statistics_numeric(sample_parquet):
    """Test compute_statistics for numeric column (temperature)."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "compute_statistics", {"filepath": sample_parquet, "column": "temperature"}
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["column"] == "temperature"
        assert data["total_count"] == 100
        assert data["null_count"] == 0
        # Numeric stats
        assert "min" in data
        assert "max" in data
        assert "mean" in data
        assert "std" in data
        assert "median" in data
        assert "unique_count" in data
        # Values should be in expected range (15.0 - 35.0)
        assert data["min"] >= 15.0
        assert data["max"] <= 35.0
        assert 15.0 <= data["mean"] <= 35.0


@pytest.mark.asyncio
async def test_compute_statistics_string(sample_parquet):
    """Test compute_statistics for string column (city)."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "compute_statistics", {"filepath": sample_parquet, "column": "city"}
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["column"] == "city"
        assert data["total_count"] == 100
        # String stats: no min/max/mean, but has unique_count and value_counts
        assert "unique_count" in data
        assert data["unique_count"] <= 5  # Only 5 possible cities
        assert "value_counts" in data
        assert len(data["value_counts"]) <= 5


@pytest.mark.asyncio
async def test_compute_statistics_nonexistent_column(sample_parquet):
    """Test compute_statistics returns error for nonexistent column."""
    async with Client(parquet_server) as client:
        result = await client.call_tool(
            "compute_statistics", {"filepath": sample_parquet, "column": "nonexistent_col"}
        )
        data = _parse_result(result)
        assert "error" in data


# --- Gateway integration tests ---


@pytest.mark.asyncio
async def test_gateway_lists_parquet_tools():
    """Test gateway exposes parquet tools with 'parquet_' prefix."""
    async with Client(gateway) as client:
        tools = await client.list_tools()
        tool_names = [t.name for t in tools]

        assert "parquet_analyze_schema" in tool_names
        assert "parquet_query_data" in tool_names
        assert "parquet_compute_statistics" in tool_names

        # HDF5 tools should still be present
        assert "hdf5_analyze_file" in tool_names


@pytest.mark.asyncio
async def test_gateway_call_parquet_tool(sample_parquet):
    """Test calling parquet_analyze_schema through the gateway."""
    async with Client(gateway) as client:
        result = await client.call_tool(
            "parquet_analyze_schema", {"filepath": sample_parquet}
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["num_rows"] == 100
        assert data["num_columns"] == 3
