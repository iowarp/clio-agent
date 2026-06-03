"""Tests for terrain DEM and point-cloud tools."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fastmcp import Client

from clio_agent.tools.catalog import get_tool_entry
from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.terrain_server import dem_terrain, pointcloud_read, terrain_server


def _parse_result(result):
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
def dem_csv(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    path = tmp_path / "dem.csv"
    np.savetxt(
        path,
        np.array(
            [
                [100.0, 101.0, 102.0, 103.0],
                [100.0, 101.5, 103.0, 104.0],
                [99.0, 100.5, 102.5, 104.5],
                [98.0, 99.0, 101.0, 103.0],
            ]
        ),
        delimiter=",",
    )
    return path


@pytest.fixture
def pointcloud_csv(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    path = tmp_path / "points.csv"
    path.write_text(
        "x,y,z\n"
        "0,0,100\n"
        "1,0,101\n"
        "2,0,103\n"
        "0,1,99\n"
        "1,1,100\n"
        "2,1,102\n"
        "0,2,97\n"
        "1,2,99\n"
        "2,2,101\n",
        encoding="utf-8",
    )
    return path


def test_dem_terrain_returns_suitability_summary(dem_csv: Path) -> None:
    result = dem_terrain(
        str(dem_csv),
        cell_size=1.0,
        elevation_min=99.0,
        elevation_max=104.0,
        slope_max_degrees=60.0,
    )

    assert result["ok"] is True
    assert result["shape"] == [4, 4]
    assert result["valid_cell_count"] == 16
    assert result["suitable_cell_count"] > 0
    assert result["suitable_fraction"] <= 1.0
    assert result["elevation"]["min"] == 98.0
    assert result["elevation"]["max"] == 104.5
    assert result["slope_degrees"]["count"] == 16
    assert result["representative_suitable_cells"]


def test_pointcloud_read_grids_csv_and_writes_dem(pointcloud_csv: Path, tmp_path: Path) -> None:
    output = tmp_path / "gridded_dem.csv"

    result = pointcloud_read(str(pointcloud_csv), grid_cell_size=1.0, output_dem_path=str(output))

    assert result["ok"] is True
    assert result["point_count"] == 9
    assert result["grid_shape"] == [3, 3]
    assert result["filled_cell_count"] == 9
    assert result["empty_cell_count"] == 0
    assert result["output_dem_path"] == str(output)
    assert output.exists()

    terrain = dem_terrain(str(output), slope_max_degrees=70.0)
    assert terrain["ok"] is True
    assert terrain["valid_cell_count"] == 9


@pytest.mark.asyncio
async def test_terrain_gateway_exposes_tools(dem_csv: Path, pointcloud_csv: Path, tmp_path: Path) -> None:
    output = tmp_path / "via_gateway.csv"

    async with Client(terrain_server) as client:
        direct = await client.call_tool("dem_terrain", {"filepath": str(dem_csv)})
    async with Client(gateway) as client:
        via_gateway = await client.call_tool(
            "terrain_pointcloud_read",
            {
                "filepath": str(pointcloud_csv),
                "grid_cell_size": 1.0,
                "output_dem_path": str(output),
            },
        )

    assert _parse_result(direct)["ok"] is True
    assert _parse_result(via_gateway)["ok"] is True
    assert output.exists()


def test_optional_geospatial_dependencies_return_structured_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"not a real tif")
    las = tmp_path / "points.las"
    las.write_bytes(b"not a real las")

    dem_result = dem_terrain(str(tif))
    pc_result = pointcloud_read(str(las))

    assert dem_result["ok"] is False
    assert dem_result["error"]["type"] == "dependency_missing"
    assert dem_result["error"]["details"]["package"] == "rasterio"
    assert pc_result["ok"] is False
    assert pc_result["error"]["type"] == "dependency_missing"
    assert pc_result["error"]["details"]["package"] == "laspy"


def test_terrain_tools_are_in_catalog() -> None:
    dem_entry = get_tool_entry("terrain_dem_terrain")
    point_entry = get_tool_entry("terrain_pointcloud_read")

    assert dem_entry is not None
    assert dem_entry.owner == "terrain_derivation"
    assert "visualization" in dem_entry.visible_to
    assert point_entry is not None
    assert "point-cloud" in point_entry.tags
