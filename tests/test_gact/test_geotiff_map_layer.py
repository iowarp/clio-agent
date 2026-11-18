"""A registered GeoTIFF draws on the layered map through the same tile routes as grids."""

from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.raster_query import RasterQueryError
from clio_agent.gact.artifacts.raster_tiles import grid_info, render_tile


def _dem(path: Path) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    values = (1900 + np.arange(400)[None, :] * 0.5 + np.zeros((400, 1))).astype("float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=400,
        width=400,
        count=1,
        dtype="float32",
        crs="EPSG:26910",
        transform=from_origin(750000, 4320000, 5, 5),
        nodata=-9999,
    ) as out:
        out.write(values, 1)
        out.set_band_description(1, "elevation")
    return path


def _tile_of(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    return int((lon + 180) / 360 * n), int(
        (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    )


def test_grid_info_and_tiles_dispatch_to_geotiff(tmp_path: Path) -> None:
    dem = _dem(tmp_path / "dem.tif")
    info = grid_info(dem, variable="elevation", select=None)
    assert info["variables"] == ["elevation"] and info["leading"] == []
    assert "viridis" in info["colormaps"]
    x, y = _tile_of(-120.104, 38.985, 14)
    png = render_tile(
        dem, z=14, x=x, y=y, variable=None, select=None, colormap="viridis", value_range=None
    )
    image = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    assert (image[..., 3] > 0).any()
    empty = render_tile(
        dem, z=14, x=0, y=0, variable=None, select=None, colormap="viridis", value_range=None
    )
    assert (np.asarray(Image.open(io.BytesIO(empty)).convert("RGBA"))[..., 3] == 0).all()


def test_wrong_band_is_a_typed_refusal(tmp_path: Path) -> None:
    dem = _dem(tmp_path / "dem.tif")
    try:
        grid_info(dem, variable="slope", select=None)
    except RasterQueryError as exc:
        assert "raster_variable_required" in repr(exc.__dict__) or "slope" in str(exc)
    else:
        raise AssertionError("a band the raster lacks must be refused")


def test_registered_geotiff_routes(tmp_path: Path) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    workspace = client.post("/v1/workspaces", json={"name": "dem", "root_path": str(tmp_path)})
    session = client.post("/v1/sessions", json={"workspace_id": workspace.json()["id"]})
    _dem(tmp_path / "dem.tif")
    pinned = client.post(
        f"/v1/sessions/{session.json()['id']}/artifacts/pin", json={"path": "dem.tif"}
    )
    assert pinned.status_code == 200, pinned.text
    artifact_id = pinned.json()["pinned"]["artifact_id"]
    info = client.get(f"/v1/artifacts/{artifact_id}/grid-info")
    assert info.status_code == 200, info.text
    assert info.json()["crs"] == "EPSG:26910" and info.json()["shape"] == [400, 400]
    x, y = _tile_of(-120.104, 38.985, 14)
    tile = client.get(f"/v1/artifacts/{artifact_id}/tiles/14/{x}/{y}.png")
    assert tile.status_code == 200, tile.text
    assert tile.headers["content-type"] == "image/png"


def test_a_geotiff_registered_under_any_name_still_draws(tmp_path: Path) -> None:
    """The stored blob and its registered name may carry no suffix ("Elevation DTM")."""
    from clio_agent.gact.artifacts.geotiff_tiles import is_geotiff

    blob = _dem(tmp_path / "dem.tif").rename(tmp_path / "blob")
    assert is_geotiff("Elevation DTM", blob)
    info = grid_info(blob, variable=None, select=None, name="Elevation DTM")
    assert info["variables"] == ["elevation"]
    other = tmp_path / "notes"
    other.write_text("not a raster")
    assert not is_geotiff("notes", other)
    assert is_geotiff("mosaic.vrt") and not is_geotiff("table.csv", None)
