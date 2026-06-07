"""Tests for the geospatial render_feature_map tool and its gateway wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent.tools import clio_kit_bridge
from clio_agent.tools.catalog import TOOL_CATALOG
from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers import geospatial_server as geo_module
from clio_agent.tools.servers.geospatial_server import geospatial_server

POLY = {
    "type": "Polygon",
    "coordinates": [[[-118.5, 34.0], [-118.0, 34.0], [-118.0, 34.5], [-118.5, 34.0]]],
}


@pytest.mark.asyncio
async def test_render_tool_registered_on_server() -> None:
    async with Client(geospatial_server) as client:
        names = {t.name for t in await client.list_tools()}
    assert "render_feature_map" in names


@pytest.mark.asyncio
async def test_render_tool_namespaced_on_gateway() -> None:
    async with Client(gateway) as client:
        names = {t.name for t in await client.list_tools()}
    assert "geospatial_render_feature_map" in names


def test_render_tool_has_catalog_entry() -> None:
    entry = TOOL_CATALOG["geospatial_render_feature_map"]
    assert entry.owner == "geospatial"
    assert "visualization" in entry.visible_to


@pytest.mark.asyncio
async def test_render_forwards_args_to_clio_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_call(server_name: str, tool_name: str, args: dict) -> dict:
        captured["server"] = server_name
        captured["tool"] = tool_name
        captured["args"] = args
        return {"status": "success", "output_path": args["output_path"]}

    monkeypatch.setattr(geo_module, "call_clio_kit_tool", fake_call)

    async with Client(geospatial_server) as client:
        result = await client.call_tool(
            "render_feature_map",
            {"layers": [{"geojson": POLY, "style": {"facecolor": "red"}}],
             "output_path": "out.png", "title": "T", "basemap": False},
        )

    assert captured["server"] == "geo"
    assert captured["tool"] == "render_feature_map"
    assert captured["args"]["output_path"] == "out.png"
    assert captured["args"]["title"] == "T"
    assert captured["args"]["basemap"] is False
    assert "bbox" not in captured["args"]  # omitted when not provided
    assert result.data["status"] == "success"


@pytest.mark.asyncio
async def test_render_includes_bbox_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_call(server_name: str, tool_name: str, args: dict) -> dict:
        captured["args"] = args
        return {"status": "success"}

    monkeypatch.setattr(geo_module, "call_clio_kit_tool", fake_call)
    async with Client(geospatial_server) as client:
        await client.call_tool(
            "render_feature_map",
            {"layers": [{"geojson": POLY}], "bbox": [-119.0, 33.5, -117.5, 35.0]},
        )
    assert captured["args"]["bbox"] == [-119.0, 33.5, -117.5, 35.0]


@pytest.mark.asyncio
async def test_render_surfaces_clio_kit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_call(server_name: str, tool_name: str, args: dict) -> dict:
        return {"error": "boom", "code": "clio_kit_unavailable"}

    monkeypatch.setattr(geo_module, "call_clio_kit_tool", fake_call)
    async with Client(geospatial_server) as client:
        result = await client.call_tool("render_feature_map", {"layers": [{"geojson": POLY}]})
    assert result.data["code"] == "clio_kit_unavailable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_render_real_clio_kit_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: launch the real clio-kit geo MCP and render a PNG.

    Requires a local clio-kit checkout next to this repo and a working uvx.
    """
    checkout = Path("../clio-kit").resolve()
    if not checkout.exists():
        pytest.skip("clio-kit checkout not available")
    monkeypatch.setenv("CLIO_KIT_PATH", str(checkout))
    out = tmp_path / "real.png"
    result = await clio_kit_bridge.call_clio_kit_tool(
        "geo",
        "render_feature_map",
        {"layers": [{"geojson": POLY, "style": {"facecolor": "red"}}],
         "output_path": str(out), "basemap": False},
    )
    assert result.get("status") == "success", result
    assert out.is_file() and out.stat().st_size > 0
