"""GeoTIFF tiles land where the raster is, in Web Mercator, and nowhere else."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from clio_agent.gact.artifacts import geotiff_tiles as tiles


def _lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def _dem(path: Path, *, crs: str = "EPSG:26910", dtype: str = "float32") -> Path:
    import rasterio
    from rasterio.transform import from_origin

    rows, cols = 400, 400
    values = (1900 + np.arange(cols)[None, :] * 0.5 + np.zeros((rows, 1))).astype(dtype)
    values[:50, :50] = -9999
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype=dtype,
        crs=crs,
        transform=from_origin(750000, 4320000, 5, 5),
        nodata=-9999,
    ) as out:
        out.write(values, 1)
        out.update_tags(1, units="m")
        out.set_band_description(1, "elevation")
    return path


def test_grid_info_reports_wgs84_bounds_units_and_range(tmp_path: Path) -> None:
    info = tiles.grid_info(_dem(tmp_path / "dem.tif"))
    west, south, east, north = info["bounds"]
    assert west == pytest.approx(-120.1138, abs=1e-3) and north == pytest.approx(38.993, abs=1e-3)
    assert info["shape"] == [400, 400] and info["crs"] == "EPSG:26910"
    assert info["variables"] == ["elevation"] and info["leading"] == []
    assert 1900 <= info["range"][0] < info["range"][1] <= 2100
    assert info["resampling"] == "bilinear"


def test_a_tile_over_the_raster_has_values_and_one_elsewhere_is_empty(tmp_path: Path) -> None:
    dem = _dem(tmp_path / "dem.tif")
    x, y = _lonlat_to_tile(-120.104, 38.985, 14)
    values = tiles.tile_values(dem, z=14, x=x, y=y)
    assert values.shape == (256, 256)
    finite = values[np.isfinite(values)]
    assert finite.size > 0 and 1900 <= finite.min() and finite.max() <= 2100
    far = tiles.tile_values(dem, z=14, x=0, y=0)
    assert np.isnan(far).all()


def test_nodata_stays_transparent(tmp_path: Path) -> None:
    dem = _dem(tmp_path / "dem.tif")
    x, y = _lonlat_to_tile(-120.1135, 38.9928, 16)  # the raster's north-west corner block
    values = tiles.tile_values(dem, z=16, x=x, y=y)
    assert not (values == -9999).any()


def test_integer_classes_are_not_interpolated(tmp_path: Path) -> None:
    dem = _dem(tmp_path / "classes.tif", dtype="int16")
    assert tiles.grid_info(dem)["resampling"] == "nearest"


def test_typed_refusals(tmp_path: Path) -> None:
    dem = _dem(tmp_path / "dem.tif")
    with pytest.raises(tiles.GeoTiffTileError) as bad_tile:
        tiles.tile_values(dem, z=3, x=99, y=0)
    assert bad_tile.value.reason == "tile_invalid"
    with pytest.raises(tiles.GeoTiffTileError) as bad_band:
        tiles.grid_info(dem, band=2)
    assert bad_band.value.reason == "raster_variable_required"
    assert tiles.is_geotiff("dem.TIF") and not tiles.is_geotiff("field.nc")
