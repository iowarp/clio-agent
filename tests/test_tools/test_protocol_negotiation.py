"""Acceptance coverage for MCP protocol negotiation."""

import pytest
from fastmcp import Client, FastMCP


@pytest.mark.asyncio
async def test_negotiates_mcp_2026_07_28() -> None:
    """The current client and server negotiate the 2026-07-28 protocol."""
    server = FastMCP("protocol-negotiation")

    async with Client(server) as client:
        assert client.protocol_version == "2026-07-28"
