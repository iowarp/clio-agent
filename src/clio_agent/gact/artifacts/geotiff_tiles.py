"""Web Mercator tile values and grid facts for registered GeoTIFF/COG/VRT rasters.

The layered map (``clio.layered-map.v1``, kind ``grid``) draws NetCDF and Zarr
grids through the GACT tile service. NDP's terrain, fuels and canopy layers are
GeoTIFFs in projected CRSs (UTM, California Albers), so this module supplies the
same two facts for them:

* :func:`tile_values` - a 256 x 256 float array for one XYZ tile, warped from the
  raster's own CRS to EPSG:3857 by GDAL. Only the source blocks under the tile are
  read, and GDAL uses the file's overviews when the tile is coarser than the data,
  so a multi-gigabyte DEM is never read whole. Cells outside the data are NaN.
* :func:`grid_info` - bands, units, WGS84 bounds, size and a 2nd-98th percentile
  value range from a decimated read, in the shape the tile service's grid-info
  returns for NetCDF.

Colouring stays with the tile service, so every layer kind shares one set of ramps.
Integer rasters (classes, fuel models) are resampled with nearest, continuous ones
with bilinear, and the choice is reported.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

TILE_SIZE = 256
MAX_ZOOM = 18
SUFFIXES = (".tif", ".tiff", ".vrt", ".img")
_WEB_MERCATOR_HALF = 20037508.342789244

_RANGES: OrderedDict[tuple[str, float, int], tuple[float, float]] = OrderedDict()
_LOCK = threading.Lock()


class GeoTiffTileError(ValueError):
    """A typed refusal: ``reason`` is a stable code, the message says what to do."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def is_geotiff(name: str) -> bool:
    """Whether an artifact's registered name is a raster this module reads."""
    return name.lower().endswith(SUFFIXES)


def _open(path: Path) -> Any:
    import rasterio  # noqa: PLC0415

    try:
        dataset = rasterio.open(path)
    except rasterio.errors.RasterioIOError as exc:
        raise GeoTiffTileError("grid_source_invalid", f"Raster could not be read: {exc}") from exc
    if dataset.crs is None:
        dataset.close()
        raise GeoTiffTileError(
            "grid_not_geographic", "The raster declares no CRS, so it cannot be placed on a map."
        )
    return dataset


def _band(dataset: Any, band: int | None) -> int:
    chosen = band or 1
    if not 1 <= chosen <= dataset.count:
        raise GeoTiffTileError(
            "raster_variable_required", f"Choose a band from 1 to {dataset.count}; got {band}."
        )
    return chosen


def _categorical(dataset: Any, band: int) -> bool:
    return bool(np.issubdtype(np.dtype(dataset.dtypes[band - 1]), np.integer))


def tile_mercator_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(left, bottom, right, top) of an XYZ tile in EPSG:3857 metres."""
    size = 2 * _WEB_MERCATOR_HALF / 2**z
    left = -_WEB_MERCATOR_HALF + x * size
    top = _WEB_MERCATOR_HALF - y * size
    return left, top - size, left + size, top


def tile_values(path: Path, *, z: int, x: int, y: int, band: int | None = None) -> np.ndarray:
    """Values of one tile in EPSG:3857, NaN where the raster has no data."""
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.transform import from_bounds  # noqa: PLC0415
    from rasterio.warp import reproject  # noqa: PLC0415

    if not 0 <= z <= MAX_ZOOM or not (0 <= x < 2**z and 0 <= y < 2**z):
        raise GeoTiffTileError("tile_invalid", f"Tile {z}/{x}/{y} is outside the tile pyramid.")
    with _open(path) as dataset:
        chosen = _band(dataset, band)
        out = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype="float32")
        if not _overlaps_tile(dataset, z, x, y):
            return out
        reproject(
            source=_band_handle(dataset, chosen),
            destination=out,
            src_transform=dataset.transform,
            src_crs=dataset.crs,
            src_nodata=dataset.nodata,
            dst_transform=from_bounds(*tile_mercator_bounds(z, x, y), TILE_SIZE, TILE_SIZE),
            dst_crs="EPSG:3857",
            dst_nodata=np.nan,
            resampling=Resampling.nearest if _categorical(dataset, chosen) else Resampling.bilinear,
        )
    return out


def _band_handle(dataset: Any, band: int) -> Any:
    import rasterio  # noqa: PLC0415

    return rasterio.band(dataset, band)


def _overlaps_tile(dataset: Any, z: int, x: int, y: int) -> bool:
    from rasterio.warp import transform_bounds  # noqa: PLC0415

    left, bottom, right, top = transform_bounds(
        dataset.crs, "EPSG:3857", *dataset.bounds, densify_pts=21
    )
    t_left, t_bottom, t_right, t_top = tile_mercator_bounds(z, x, y)
    return left < t_right and t_left < right and bottom < t_top and t_bottom < top


def value_range(path: Path, band: int | None = None) -> tuple[float, float]:
    """2nd-98th percentile from a decimated read (overviews when present), cached."""
    key = (str(path), path.stat().st_mtime, band or 1)
    with _LOCK:
        if key in _RANGES:
            return _RANGES[key]
    with _open(path) as dataset:
        chosen = _band(dataset, band)
        step = max(1, math.ceil(max(dataset.width, dataset.height) / 512))
        sample = dataset.read(
            chosen,
            out_shape=(max(1, dataset.height // step), max(1, dataset.width // step)),
            masked=True,
        )
    finite = np.asarray(sample.compressed(), dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise GeoTiffTileError("raster_no_values", "The raster has no valid values.")
    low, high = (float(v) for v in np.percentile(finite, [2, 98]))
    if high <= low:
        high = low + 1.0
    with _LOCK:
        _RANGES[key] = (low, high)
        while len(_RANGES) > 64:
            _RANGES.popitem(last=False)
    return low, high


def grid_info(path: Path, *, band: int | None = None) -> dict[str, Any]:
    """Bands, units, WGS84 bounds, size and value range, as grid-info reports them."""
    from rasterio.warp import transform_bounds  # noqa: PLC0415

    with _open(path) as dataset:
        chosen = _band(dataset, band)
        west, south, east, north = transform_bounds(
            dataset.crs, "EPSG:4326", *dataset.bounds, densify_pts=21
        )
        names = [dataset.descriptions[i] or f"band{i + 1}" for i in range(dataset.count)]
        info = {
            "variable": names[chosen - 1],
            "variables": names,
            "units": (dataset.units or [""] * dataset.count)[chosen - 1] or "",
            "longName": dataset.descriptions[chosen - 1] or "",
            "bounds": [float(west), float(south), float(east), float(north)],
            "shape": [int(dataset.height), int(dataset.width)],
            "leading": [],
            "crs": dataset.crs.to_string(),
            "resolution": [abs(dataset.res[0]), abs(dataset.res[1])],
            "resampling": "nearest" if _categorical(dataset, chosen) else "bilinear",
            "overviews": len(dataset.overviews(chosen)),
        }
    info["range"] = list(value_range(path, chosen))
    return info
