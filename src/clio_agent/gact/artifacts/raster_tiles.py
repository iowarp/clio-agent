"""Web Mercator XYZ tiles and grid facts for registered gridded artifacts.

The layered map draws gridded fields (reanalyses, model output, altimetry)
as raster tiles rendered here, next to the data, so a multi-gigabyte field
never goes to the browser and never loads whole: each tile reads only the
index window it covers, strided down to at most ``_MAX_WINDOW`` cells per
axis, from a lazily opened NetCDF or zipped Zarr store.

The grid must have 1-D longitude and latitude coordinates (any order, 0..360
or -180..180); leading dimensions (time, level, member) are picked with the
same nearest-value selection as raster-query. Colours come from the same
perceptual ramps as the interface's data scales, with a fixed value range so
neighbouring tiles agree.
"""

from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from clio_agent.gact.artifacts.raster_query import RasterQueryError, _plane_of

TILE_SIZE = 256
_MAX_WINDOW = 1024
_MAX_ZOOM = 14
_LON_NAMES = ("longitude", "lon", "x", "nav_lon", "xh")
_LAT_NAMES = ("latitude", "lat", "y", "nav_lat", "yh")

#: The interface's data-scale ramps (gact-tui ``theme/data-scales.ts``), five stops each.
COLORMAPS: dict[str, tuple[str, ...]] = {
    "viridis": ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"),
    "magma": ("#000004", "#51127c", "#b73779", "#fc8961", "#fcfdbf"),
    "plasma": ("#0d0887", "#7e03a8", "#cb4679", "#f89441", "#f0f921"),
    "cividis": ("#00204c", "#424e6c", "#7c7b78", "#bba774", "#ffea46"),
    "turbo": ("#30123b", "#3e9eca", "#9cd93b", "#f99b21", "#7a0403"),
    "grayscale": ("#161616", "#4d4d4d", "#858585", "#bdbdbd", "#f5f5f5"),
    "balance": ("#2166ac", "#92c5de", "#f7f7f7", "#f4a582", "#b2182b"),
}

_OPEN: OrderedDict[tuple[str, float], tuple[Any, threading.Lock]] = OrderedDict()
_OPEN_LOCK = threading.Lock()
_RANGES: dict[tuple[Any, ...], tuple[float, float]] = {}


def _open(path: Path, name: str | None = None) -> tuple[Any, threading.Lock]:
    """A lazily opened dataset (cached per path and modification time) and its lock.

    ``name`` is the artifact's registered name, whose suffix says the format
    (the stored blob may not carry one); it defaults to the path's name.
    """
    import xarray as xr  # noqa: PLC0415

    key = (str(path), path.stat().st_mtime)
    with _OPEN_LOCK:
        if key in _OPEN:
            _OPEN.move_to_end(key)
            return _OPEN[key]
        name = (name or path.name).lower()
        try:
            if name.endswith(".zarr.zip"):
                from zarr.storage import ZipStore  # noqa: PLC0415

                dataset = xr.open_zarr(ZipStore(str(path), mode="r"), consolidated=False)
            elif name.endswith((".nc", ".netcdf", ".nc4")):
                dataset = xr.open_dataset(path)
            else:
                raise RasterQueryError(
                    "grid_format_unsupported", "Map tiles need a NetCDF or zipped Zarr grid."
                )
        except RasterQueryError:
            raise
        except Exception as exc:
            raise RasterQueryError("grid_source_invalid", f"Grid could not be read: {exc}") from exc
        _OPEN[key] = (dataset, threading.Lock())
        while len(_OPEN) > 8:
            _, (old, _lock) = _OPEN.popitem(last=False)
            old.close()
        return _OPEN[key]


def _axes(data: Any) -> tuple[str, str]:
    lon = next((n for n in _LON_NAMES if n in data.dims), None)
    lat = next((n for n in _LAT_NAMES if n in data.dims), None)
    if lon is None or lat is None:
        raise RasterQueryError(
            "grid_not_geographic",
            f"Map tiles need longitude and latitude dimensions; {list(data.dims)} has none.",
        )
    return lon, lat


def _variable(dataset: Any, variable: str | None) -> Any:
    names = [str(n) for n in dataset.data_vars]
    chosen = variable or (names[0] if len(names) == 1 else None)
    if chosen not in dataset:
        raise RasterQueryError(
            "raster_variable_required", f"Choose one variable from {names}; received {variable!r}."
        )
    data = dataset[chosen]
    lon, lat = _axes(data)
    order = [d for d in data.dims if d not in (lon, lat)] + [lat, lon]
    return data.transpose(*order)


def grid_info(
    path: Path, *, variable: str | None, select: dict[str, Any] | None, name: str | None = None
) -> dict[str, Any]:
    """Variables, extent, leading-dimension values and a value range for a legend."""
    dataset, lock = _open(path, name)
    with lock:
        data = _variable(dataset, variable)
        lon, lat = _axes(data)
        lons = np.asarray(data[lon].values, dtype=float)
        lats = np.asarray(data[lat].values, dtype=float)
        leading = []
        for dim in data.dims[:-2]:
            values = (
                np.asarray(data[dim].values) if dim in data.coords else np.arange(data.sizes[dim])
            )
            shown = values[:5000]
            text = (
                [np.datetime_as_string(v, unit="s") + "Z" for v in shown]
                if np.issubdtype(values.dtype, np.datetime64)
                else [v.item() if hasattr(v, "item") else v for v in shown]
            )
            leading.append(
                {
                    "dim": str(dim),
                    "count": int(data.sizes[dim]),
                    "values": text,
                    "truncated": len(values) > 5000,
                }
            )
        low, high = _range(path, data, select)
        wrapped = np.where(lons > 180, lons - 360, lons)
        return {
            "variable": str(data.name),
            "variables": [str(n) for n in dataset.data_vars],
            "units": str(data.attrs.get("units", "")),
            "longName": str(data.attrs.get("long_name", data.attrs.get("standard_name", ""))),
            "bounds": [
                float(wrapped.min()),
                float(lats.min()),
                float(wrapped.max()),
                float(lats.max()),
            ],
            "shape": [int(data.sizes[lat]), int(data.sizes[lon])],
            "leading": leading,
            "range": [low, high],
            "colormaps": sorted(COLORMAPS),
        }


def _range(path: Path, data: Any, select: dict[str, Any] | None) -> tuple[float, float]:
    """2nd-98th percentile of a strided sample of the selected plane (cached)."""
    key = (str(path), path.stat().st_mtime, str(data.name), tuple(sorted((select or {}).items())))
    if key in _RANGES:
        return _RANGES[key]
    plane, _ = _plane_of(data, select)
    lon, lat = _axes(plane)
    step_y = max(1, plane.sizes[lat] // 256)
    step_x = max(1, plane.sizes[lon] // 256)
    sample = np.asarray(
        plane.isel({lat: slice(None, None, step_y), lon: slice(None, None, step_x)}).values,
        dtype=float,
    )
    finite = sample[np.isfinite(sample)]
    if finite.size == 0:
        raise RasterQueryError("raster_no_values", "The selected plane has no finite values.")
    low, high = (float(v) for v in np.percentile(finite, [2, 98]))
    if low == high:
        low, high = low - 0.5, high + 0.5
    _RANGES[key] = (low, high)
    return low, high


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of an XYZ tile in degrees."""
    n = 2**z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _pixel_centres(z: int, x: int, y: int) -> tuple[np.ndarray, np.ndarray]:
    n = 2**z
    px = (x + (np.arange(TILE_SIZE) + 0.5) / TILE_SIZE) / n
    py = (y + (np.arange(TILE_SIZE) + 0.5) / TILE_SIZE) / n
    lons = px * 360.0 - 180.0
    lats = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * py))))
    return lons, lats


def _nearest(coord: np.ndarray, wanted: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Index of the nearest coordinate cell for each wanted value, and inside-mask."""
    order = np.argsort(coord)
    ordered = coord[order]
    pos = np.clip(np.searchsorted(ordered, wanted), 1, len(ordered) - 1)
    left, right = ordered[pos - 1], ordered[pos]
    pick = np.where(np.abs(wanted - left) <= np.abs(wanted - right), pos - 1, pos)
    half = np.abs(np.diff(ordered)).max() / 2 if len(ordered) > 1 else 0.5
    inside = (wanted >= ordered[0] - half) & (wanted <= ordered[-1] + half)
    return order[pick], inside


def _colour(
    values: np.ndarray, colormap: str, low: float, high: float, opacity: float
) -> np.ndarray:
    stops = np.array(
        [[int(h[i : i + 2], 16) for i in (1, 3, 5)] for h in COLORMAPS[colormap]], dtype=float
    )
    finite = np.isfinite(values)
    fraction = np.clip((np.where(finite, values, low) - low) / (high - low), 0, 0.9999)
    scaled = fraction * (len(stops) - 1)
    index = np.floor(scaled).astype(int)
    position = (scaled - index)[..., None]
    rgb = stops[index] + (stops[np.minimum(index + 1, len(stops) - 1)] - stops[index]) * position
    alpha = np.where(finite, round(255 * opacity), 0)
    rgba = np.concatenate([np.nan_to_num(rgb), alpha[..., None]], axis=-1)
    return rgba.astype(np.uint8)


def render_tile(
    path: Path,
    *,
    z: int,
    x: int,
    y: int,
    variable: str | None,
    select: dict[str, Any] | None,
    colormap: str,
    value_range: tuple[float, float] | None,
    opacity: float = 1.0,
    name: str | None = None,
) -> bytes:
    """One 256 x 256 PNG tile of the selected plane, transparent outside the grid."""
    from PIL import Image  # noqa: PLC0415

    if not 0 <= z <= _MAX_ZOOM or not (0 <= x < 2**z and 0 <= y < 2**z):
        raise RasterQueryError("tile_invalid", f"Tile {z}/{x}/{y} is outside the tile pyramid.")
    if colormap not in COLORMAPS:
        raise RasterQueryError("colormap_invalid", f"Colormap must be one of {sorted(COLORMAPS)}.")
    dataset, lock = _open(path, name)
    with lock:
        data = _variable(dataset, variable)
        lon, lat = _axes(data)
        low, high = value_range or _range(path, data, select)
        plane, _ = _plane_of(data, select)
        lons = np.asarray(plane[lon].values, dtype=float)
        lats = np.asarray(plane[lat].values, dtype=float)
        want_lon, want_lat = _pixel_centres(z, x, y)
        if lons.max() > 180:
            want_lon = np.mod(want_lon, 360.0)
        ix, inside_x = _nearest(lons, want_lon)
        iy, inside_y = _nearest(lats, want_lat)
        if not inside_x.any() or not inside_y.any():
            values = np.full((TILE_SIZE, TILE_SIZE), np.nan)
        else:
            x0, x1 = int(ix[inside_x].min()), int(ix[inside_x].max()) + 1
            y0, y1 = int(iy[inside_y].min()), int(iy[inside_y].max()) + 1
            step_x = max(1, (x1 - x0) // _MAX_WINDOW)
            step_y = max(1, (y1 - y0) // _MAX_WINDOW)
            window = np.asarray(
                plane.isel({lat: slice(y0, y1, step_y), lon: slice(x0, x1, step_x)}).values,
                dtype=float,
            )
            local_x = np.clip((ix - x0) // step_x, 0, window.shape[1] - 1)
            local_y = np.clip((iy - y0) // step_y, 0, window.shape[0] - 1)
            values = window[np.ix_(local_y, local_x)]
            values[~inside_y, :] = np.nan
            values[:, ~inside_x] = np.nan
    image = Image.fromarray(_colour(values, colormap, low, high, opacity), mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def section_sample(
    path: Path, *, variable: str | None, width: int, height: int, name: str | None = None
) -> dict[str, Any]:
    """A section (distance x vertical) resampled to at most width x height.

    Sections come from the ocean and weather servers: dimensions ``distance``
    and the vertical named in the ``vertical`` attribute (pressure, depth,
    level), with per-column latitude/longitude and, from profiles, the
    station distances and profile ids.
    """
    dataset, lock = _open(path, name)
    with lock:
        names = [
            str(n)
            for n in dataset.data_vars
            if "distance" in dataset[n].dims and dataset[n].ndim == 2
        ]
        chosen = variable or (names[0] if names else None)
        if chosen not in names:
            raise RasterQueryError(
                "section_variable_required",
                f"Choose a section variable from {names}; received {variable!r}.",
            )
        data = dataset[chosen]
        vertical = next(d for d in data.dims if d != "distance")
        data = data.transpose(vertical, "distance")
        distance = np.asarray(data["distance"].values, dtype=float)
        levels = (
            np.asarray(data[vertical].values, dtype=float)
            if vertical in data.coords
            else np.arange(data.sizes[vertical], dtype=float)
        )
        step_x = max(1, math.ceil(len(distance) / width))
        step_y = max(1, math.ceil(len(levels) / height))
        values = np.asarray(
            data.isel(
                {"distance": slice(None, None, step_x), vertical: slice(None, None, step_y)}
            ).values,
            dtype=float,
        )
        stations: list[dict[str, Any]] = []
        if "station_distance" in dataset:
            ids = (
                dataset["station_profile"].values
                if "station_profile" in dataset
                else [None] * dataset.sizes["station"]
            )
            stations = [
                {"distance": float(d), "profile": None if p is None else str(p)}
                for d, p in zip(dataset["station_distance"].values, ids, strict=True)
            ]
        elif "profile" in data.coords:
            stations = [
                {"distance": float(d), "profile": str(p)}
                for d, p in zip(distance, data["profile"].values, strict=True)
            ]
        finite = values[np.isfinite(values)]
        positive_down = str(
            data[vertical].attrs.get("positive", "")
        ).lower() == "down" or vertical in (
            "pressure",
            "depth",
            "level",
            "pressure_level",
            "isobaricInhPa",
            "z_l",
        )
        return {
            "variable": chosen,
            "variables": names,
            "units": str(data.attrs.get("units", "")),
            "vertical": str(vertical),
            "verticalUnits": str(data[vertical].attrs.get("units", ""))
            if vertical in data.coords
            else "",
            "positiveDown": bool(positive_down),
            "distance": distance[::step_x].tolist(),
            "levels": levels[::step_y].tolist(),
            "values": [[float(v) if np.isfinite(v) else None for v in row] for row in values],
            "min": float(finite.min()) if finite.size else None,
            "max": float(finite.max()) if finite.size else None,
            "stations": stations,
            "path": str(dataset.attrs.get("path", "")),
            "latitude": np.asarray(data["latitude"].values, dtype=float)[::step_x].tolist()
            if "latitude" in data.coords
            else [],
            "longitude": np.asarray(data["longitude"].values, dtype=float)[::step_x].tolist()
            if "longitude" in data.coords
            else [],
        }
