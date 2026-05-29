"""Tests for the imaging MCP server."""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastmcp import Client
from PIL import Image

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.imaging_server import imaging_server


def _parse_result(result):
    """Extract a dict from FastMCP CallToolResult."""
    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {"raw": data}
    return {"raw": str(data)}


@pytest.fixture
def png_file(tmp_path):
    arr = np.zeros((12, 16), dtype=np.uint8)
    arr[2:5, 3:7] = 120
    arr[7:10, 10:14] = 220
    path = tmp_path / "cells.png"
    Image.fromarray(arr, mode="L").save(path)
    return path


@pytest.mark.asyncio
async def test_inspect_png_returns_image_summary(png_file):
    async with Client(imaging_server) as client:
        result = await client.call_tool("inspect_png", {"filepath": str(png_file), "threshold": 32})
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["format"] == "PNG"
    assert data["mode"] == "L"
    assert data["width"] == 16
    assert data["height"] == 12
    assert data["foreground_pixels"] == 24
    assert data["foreground_bbox"] == [3, 2, 13, 9]
    assert data["connected_regions"] == 2
    assert data["intensity"]["max"] == 220


@pytest.mark.asyncio
async def test_inspect_png_accepts_null_threshold(png_file):
    async with Client(imaging_server) as client:
        result = await client.call_tool(
            "inspect_png",
            {"filepath": str(png_file), "threshold": None},
        )
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["threshold"] == 32
    assert data["connected_regions"] == 2


@pytest.mark.asyncio
async def test_gateway_exposes_imaging_tool(png_file):
    async with Client(gateway) as client:
        result = await client.call_tool("imaging_inspect_png", {"filepath": str(png_file)})

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["connected_regions"] == 2
