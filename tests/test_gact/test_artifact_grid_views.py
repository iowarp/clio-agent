"""Map tiles, grid facts and section samples of registered gridded artifacts."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient
from PIL import Image

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.raster_query import RasterQueryError
from clio_agent.gact.artifacts.raster_tiles import (
    COLORMAPS,
    grid_info,
    render_tile,
    section_sample,
    tile_bounds,
)


def _global_grid(path: Path, *, lon360: bool = False) -> Path:
    """Two days on a 1-degree global grid: value = latitude on day 1, -latitude on day 2."""
    lat = np.arange(-89.5, 90, 1.0)[::-1]  # descending, as ERA5
    lon = np.arange(0.5, 360, 1.0) if lon360 else np.arange(-179.5, 180, 1.0)
    field = np.broadcast_to(lat[:, None], (lat.size, lon.size))
    data = np.stack([field, -field])
    data[0, :, :] = np.where(np.abs(lat)[:, None] > 80, np.nan, data[0])
    xr.Dataset(
        {"t": (("time", "latitude", "longitude"), data, {"units": "K"})},
        coords={
            "time": np.array(["2022-07-01", "2022-07-02"], dtype="datetime64[ns]"),
            "latitude": lat,
            "longitude": lon,
        },
    ).to_netcdf(path)
    return path


def _pixel(png: bytes, x: int, y: int) -> tuple[int, int, int, int]:
    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel((x, y))


def _hex(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize("lon360", [False, True])
def test_world_tile_colours_follow_latitude(tmp_path: Path, lon360: bool) -> None:
    path = _global_grid(tmp_path / "g.nc", lon360=lon360)
    png = render_tile(
        path,
        z=0,
        x=0,
        y=0,
        variable="t",
        select=None,
        colormap="viridis",
        value_range=(-90.0, 90.0),
    )
    # Equator sits at the middle row; 45N at Mercator row ~ 0.36 of the tile.
    mid = _pixel(png, 128, 128)
    assert mid[3] == 255
    assert abs(mid[0] - _hex(COLORMAPS["viridis"][2])[0]) < 20
    north = _pixel(png, 128, 92)
    south = _pixel(png, 128, 164)
    assert north != south
    # Above 80N the first day is missing: transparent.
    assert _pixel(png, 128, 2)[3] == 0


def test_time_selection_flips_the_field(tmp_path: Path) -> None:
    path = _global_grid(tmp_path / "g.nc")
    day1 = render_tile(
        path,
        z=0,
        x=0,
        y=0,
        variable="t",
        select={"time": "2022-07-01"},
        colormap="balance",
        value_range=(-90.0, 90.0),
    )
    day2 = render_tile(
        path,
        z=0,
        x=0,
        y=0,
        variable="t",
        select={"time": "2022-07-02T00:00:00Z"},
        colormap="balance",
        value_range=(-90.0, 90.0),
    )
    # Mercator rows mirror about the equator: row r and row 255 - r.
    assert _pixel(day1, 128, 92)[:3] == _pixel(day2, 128, 163)[:3]


def test_deep_zoom_reads_only_its_window(tmp_path: Path) -> None:
    path = _global_grid(tmp_path / "g.nc")
    west, south, east, north = tile_bounds(6, 33, 22)
    png = render_tile(
        path,
        z=6,
        x=33,
        y=22,
        variable="t",
        select={"time": "2022-07-01"},
        colormap="viridis",
        value_range=(-90.0, 90.0),
    )
    assert Image.open(io.BytesIO(png)).size == (256, 256)
    assert west < east and south < north


def test_grid_info_and_errors(tmp_path: Path) -> None:
    path = _global_grid(tmp_path / "g.nc")
    info = grid_info(path, variable="t", select=None)
    assert info["units"] == "K"
    assert info["leading"][0]["values"] == ["2022-07-01T00:00:00Z", "2022-07-02T00:00:00Z"]
    assert info["bounds"] == [-179.5, -89.5, 179.5, 89.5]
    with pytest.raises(RasterQueryError, match="Colormap"):
        render_tile(
            path, z=0, x=0, y=0, variable="t", select=None, colormap="rainbow", value_range=None
        )
    with pytest.raises(RasterQueryError, match="outside the tile pyramid"):
        render_tile(
            path, z=2, x=5, y=0, variable="t", select=None, colormap="viridis", value_range=None
        )
    flat = tmp_path / "flat.nc"
    xr.Dataset({"v": (("a", "b"), np.zeros((3, 3)))}).to_netcdf(flat)
    with pytest.raises(RasterQueryError, match="longitude and latitude"):
        grid_info(flat, variable="v", select=None)


def test_section_sample_carries_stations_and_orientation(tmp_path: Path) -> None:
    distance = np.linspace(0, 500, 11)
    pressure = np.arange(0, 1000, 10.0)
    ds = xr.Dataset(
        {"CT": (("distance", "pressure"), np.add.outer(distance / 100, -pressure / 100))},
        coords={
            "distance": distance,
            "pressure": ("pressure", pressure, {"units": "dbar", "positive": "down"}),
            "latitude": ("distance", np.linspace(30, 35, 11)),
            "longitude": ("distance", np.full(11, -40.0)),
            "profile": ("distance", [f"p{i}" for i in range(11)]),
        },
        attrs={"path": json.dumps({"type": "LineString", "coordinates": [[-40, 30], [-40, 35]]})},
    )
    path = tmp_path / "section.nc"
    ds.to_netcdf(path)
    out = section_sample(path, variable=None, width=6, height=25)
    assert out["variable"] == "CT" and out["vertical"] == "pressure" and out["positiveDown"]
    assert len(out["distance"]) == 6 and len(out["levels"]) == 25
    assert [s["profile"] for s in out["stations"]][:3] == ["p0", "p1", "p2"]
    assert "LineString" in out["path"]


def test_registered_grid_routes(tmp_path: Path) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    workspace = client.post("/v1/workspaces", json={"name": "grid", "root_path": str(tmp_path)})
    session = client.post("/v1/sessions", json={"workspace_id": workspace.json()["id"]})
    _global_grid(tmp_path / "field.nc")
    pinned = client.post(
        f"/v1/sessions/{session.json()['id']}/artifacts/pin", json={"path": "field.nc"}
    )
    assert pinned.status_code == 200, pinned.text
    artifact_id = pinned.json()["pinned"]["artifact_id"]
    tile = client.get(
        f"/v1/artifacts/{artifact_id}/tiles/1/0/0.png",
        params={
            "variable": "t",
            "select": json.dumps({"time": "2022-07-02"}),
            "colormap": "balance",
            "vmin": -90,
            "vmax": 90,
        },
    )
    assert tile.status_code == 200, tile.text
    assert tile.headers["content-type"] == "image/png"
    info = client.get(f"/v1/artifacts/{artifact_id}/grid-info", params={"variable": "t"})
    assert info.status_code == 200 and info.json()["shape"] == [180, 360]
    bad = client.get(
        f"/v1/artifacts/{artifact_id}/tiles/0/0/0.png", params={"variable": "t", "colormap": "nope"}
    )
    assert bad.status_code == 422 and "colormap_invalid" in bad.text
    missing = client.get("/v1/artifacts/artifact_missing/grid-info")
    assert missing.status_code == 404
