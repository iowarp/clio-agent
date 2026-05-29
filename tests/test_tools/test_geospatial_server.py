"""Tests for the geospatial MCP server."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.geospatial_server import geospatial_server


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
def geojson_file(tmp_path):
    path = tmp_path / "sites.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"site_id": "north_ridge", "kind": "sensor"},
                        "geometry": {"type": "Point", "coordinates": [-105.27, 40.01]},
                    },
                    {
                        "type": "Feature",
                        "properties": {"site_id": "study_boundary", "kind": "boundary"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-105.29, 39.98],
                                    [-105.24, 39.98],
                                    [-105.24, 40.03],
                                    [-105.29, 40.03],
                                    [-105.29, 39.98],
                                ]
                            ],
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_inspect_geojson_returns_spatial_summary(geojson_file):
    async with Client(geospatial_server) as client:
        result = await client.call_tool("inspect_geojson", {"filepath": str(geojson_file)})
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["feature_count"] == 2
    assert data["geometry_types"] == {"Point": 1, "Polygon": 1}
    assert data["property_keys"] == ["kind", "site_id"]
    assert data["bbox"] == [-105.29, 39.98, -105.24, 40.03]
    assert data["invalid_coordinate_count"] == 0


@pytest.mark.asyncio
async def test_gateway_exposes_geospatial_tool(geojson_file):
    async with Client(gateway) as client:
        result = await client.call_tool(
            "geospatial_inspect_geojson", {"filepath": str(geojson_file)}
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["geometry_types"]["Polygon"] == 1
