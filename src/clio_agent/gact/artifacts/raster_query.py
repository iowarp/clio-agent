"""Bounded resampling of registered two-dimensional raster artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


class RasterQueryError(ValueError):
    """A typed source, shape, or extent failure for raster-query."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RasterGrid:
    """One spatial plane with bounds ordered left, top, right, bottom."""

    values: np.ndarray
    bounds: tuple[float, float, float, float]
    x_label: str = "Column"
    y_label: str = "Row"
    #: For a variable with more than two dimensions: the plane drawn along each leading one.
    frame: tuple[dict[str, Any], ...] = ()


_MAX_SOURCE_BYTES = 256 * 1024 * 1024
_MAX_CELLS = 40_000_000


def _numeric_plane(value: Any, *, variable: str | None) -> np.ndarray:
    array = np.ma.asarray(value)
    array = np.ma.squeeze(array)
    if array.ndim != 2:
        field = f" variable {variable!r}" if variable else ""
        raise RasterQueryError(
            "raster_shape_invalid",
            f"Raster{field} must have exactly two dimensions after selection.",
        )
    if array.size > _MAX_CELLS:
        raise RasterQueryError(
            "raster_too_large", "Raster has too many source cells for this query.", 413
        )
    try:
        plane = np.ma.filled(array.astype("float64"), np.nan)
    except (TypeError, ValueError) as exc:
        raise RasterQueryError("raster_values_invalid", "Raster cells must be numeric.") from exc
    if not np.isfinite(plane).any():
        raise RasterQueryError("raster_no_values", "Raster has no finite values to display.")
    return plane


def _plane_of(data: Any, select: dict[str, Any] | None) -> tuple[Any, tuple[dict[str, Any], ...]]:
    """The 2-D plane of ``data``: leading dimensions picked from ``select`` (nearest) or 0."""
    frame: list[dict[str, Any]] = []
    for dim in data.dims[:-2]:
        size = int(data.sizes[dim])
        if select and dim in select:
            try:
                picked = data.sel({dim: select[dim]}, method="nearest")
            except (KeyError, TypeError, ValueError) as exc:
                raise RasterQueryError(
                    "raster_selection_invalid",
                    f"Could not select {dim}={select[dim]!r}: {exc}",
                ) from exc
            index = (
                int(np.argmin(np.abs(data[dim].values - picked[dim].values)))
                if dim in data.coords
                else 0
            )
            data = picked
        else:
            index, data = 0, data.isel({dim: 0})
        value = np.asarray(data[dim].values) if dim in data.coords else np.asarray(index)
        text = (
            np.datetime_as_string(value, unit="s") + "Z"
            if np.issubdtype(value.dtype, np.datetime64)
            else str(value.item() if value.ndim == 0 else value)
        )
        frame.append({"dim": str(dim), "value": text, "index": index, "count": size})
    return data, tuple(frame)


def _coordinate_bounds(
    data: Any, plane: np.ndarray
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    """Cell-edge bounds from 1-D numeric coordinates, rows ordered top (largest y) first."""
    y_dim, x_dim = data.dims[-2:]
    if y_dim not in data.coords or x_dim not in data.coords:
        return None
    y, x = np.asarray(data[y_dim].values), np.asarray(data[x_dim].values)
    if (
        y.ndim != 1
        or x.ndim != 1
        or y.size < 2
        or x.size < 2
        or y.dtype.kind not in "iuf"
        or x.dtype.kind not in "iuf"
    ):
        return None
    dy, dx = np.diff(y), np.diff(x)
    if not ((dy > 0).all() or (dy < 0).all()) or not ((dx > 0).all() or (dx < 0).all()):
        return None
    if dy[0] > 0:
        plane, y = plane[::-1, :], y[::-1]
    if dx[0] < 0:
        plane, x = plane[:, ::-1], x[::-1]
    half_y, half_x = abs(float(dy[0])) / 2, abs(float(dx[0])) / 2
    return plane, (
        float(x[0]) - half_x,
        float(y[0]) + half_y,
        float(x[-1]) + half_x,
        float(y[-1]) - half_y,
    )


def _xarray_grid(
    path: Path, *, variable: str | None, zarr_archive: bool, select: dict[str, Any] | None = None
) -> RasterGrid:
    import xarray as xr  # noqa: PLC0415

    store: Any = None
    try:
        if zarr_archive:
            from zarr.storage import ZipStore  # noqa: PLC0415

            store = ZipStore(str(path), mode="r")
            dataset = xr.open_zarr(store, consolidated=False)
        else:
            dataset = xr.open_dataset(path)
        with dataset:
            names = list(dataset.data_vars)
            chosen = variable or (names[0] if len(names) == 1 else None)
            if chosen not in dataset:
                raise RasterQueryError(
                    "raster_variable_required",
                    f"Choose one raster variable from {names}; received {variable!r}.",
                )
            data = dataset[chosen]
            if data.ndim < 2:
                raise RasterQueryError(
                    "raster_shape_invalid",
                    f"Raster variable {chosen!r} has dimensions {list(data.dims)}; it needs two.",
                )
            data, frame = _plane_of(data, select)
            plane = _numeric_plane(data.values, variable=chosen)
            y_label, x_label = data.dims[-2:]
            located = _coordinate_bounds(data, plane)
            if located is not None:
                plane, bounds = located
                return RasterGrid(plane, bounds, str(x_label), str(y_label), frame)
            return RasterGrid(
                plane,
                (0.0, 0.0, float(plane.shape[1]), float(plane.shape[0])),
                str(x_label),
                str(y_label),
                frame,
            )
    except RasterQueryError:
        raise
    except Exception as exc:
        raise RasterQueryError(
            "raster_source_invalid", f"Raster dataset could not be read: {exc}"
        ) from exc
    finally:
        if store is not None:
            store.close()


def load_raster(
    path: Path, *, name: str, variable: str | None, band: int, select: dict[str, Any] | None = None
) -> RasterGrid:
    """Decode a registered NPY, numeric CSV, GeoTIFF, NetCDF, or zipped Zarr grid."""

    if path.stat().st_size > _MAX_SOURCE_BYTES:
        raise RasterQueryError(
            "raster_too_large", "Raster artifact exceeds the 256 MiB source limit.", 413
        )
    suffix = name.lower()
    try:
        if suffix.endswith(".npy"):
            plane = _numeric_plane(np.load(path, allow_pickle=False), variable=variable)
        elif suffix.endswith(".csv"):
            plane = _numeric_plane(np.genfromtxt(path, delimiter=","), variable=variable)
        elif suffix.endswith((".tif", ".tiff", ".geotiff")):
            import rasterio  # noqa: PLC0415

            with rasterio.open(path) as dataset:
                if band < 1 or band > dataset.count:
                    raise RasterQueryError(
                        "raster_band_invalid", f"Band {band} is outside 1–{dataset.count}."
                    )
                plane = _numeric_plane(dataset.read(band, masked=True), variable=variable)
                bounds = dataset.bounds
                return RasterGrid(
                    plane,
                    (
                        float(bounds.left),
                        float(bounds.top),
                        float(bounds.right),
                        float(bounds.bottom),
                    ),
                    "X" if dataset.crs else "Column",
                    "Y" if dataset.crs else "Row",
                )
        elif suffix.endswith((".nc", ".netcdf")):
            return _xarray_grid(path, variable=variable, zarr_archive=False, select=select)
        elif suffix.endswith(".zarr.zip"):
            return _xarray_grid(path, variable=variable, zarr_archive=True, select=select)
        else:
            raise RasterQueryError(
                "raster_format_unsupported",
                "Raster artifact must be NPY, numeric-grid CSV, GeoTIFF, NetCDF, or a zipped Zarr store.",
            )
    except RasterQueryError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise RasterQueryError(
            "raster_source_invalid", f"Raster source could not be read: {exc}"
        ) from exc
    return RasterGrid(plane, (0.0, 0.0, float(plane.shape[1]), float(plane.shape[0])))


def resample_raster(
    grid: RasterGrid,
    *,
    width: int,
    height: int,
    extent: tuple[float, float, float, float] | None,
) -> dict[str, Any]:
    """Sample the visible world extent into a bounded row-major value array."""

    left, top, right, bottom = extent or grid.bounds
    source_left, source_top, source_right, source_bottom = grid.bounds
    if not all(np.isfinite((left, top, right, bottom))) or left >= right or top == bottom:
        raise RasterQueryError(
            "raster_extent_invalid", "Extent must contain finite left, top, right, bottom bounds."
        )
    if (bottom - top) * (source_bottom - source_top) <= 0:
        raise RasterQueryError(
            "raster_extent_invalid", "Extent vertical bounds are in the wrong order."
        )
    x_centers = left + (np.arange(width) + 0.5) * (right - left) / width
    y_centers = top + (np.arange(height) + 0.5) * (bottom - top) / height
    x_index = np.floor(
        (x_centers - source_left) / (source_right - source_left) * grid.values.shape[1]
    ).astype(int)
    y_index = np.floor(
        (y_centers - source_top) / (source_bottom - source_top) * grid.values.shape[0]
    ).astype(int)
    inside_x = (x_index >= 0) & (x_index < grid.values.shape[1])
    inside_y = (y_index >= 0) & (y_index < grid.values.shape[0])
    x_index = np.clip(x_index, 0, grid.values.shape[1] - 1)
    y_index = np.clip(y_index, 0, grid.values.shape[0] - 1)
    sampled = grid.values[np.ix_(y_index, x_index)].astype("float64", copy=True)
    sampled[~inside_y, :] = np.nan
    sampled[:, ~inside_x] = np.nan
    finite = sampled[np.isfinite(sampled)]
    output: list[float | None] = [
        float(value) if np.isfinite(value) else None for value in sampled.ravel()
    ]
    return {
        "width": width,
        "height": height,
        "extent": [left, top, right, bottom],
        "sourceBounds": list(grid.bounds),
        "sourceShape": list(grid.values.shape),
        "xLabel": grid.x_label,
        "yLabel": grid.y_label,
        **({"frame": list(grid.frame)} if grid.frame else {}),
        "min": float(finite.min()) if finite.size else None,
        "max": float(finite.max()) if finite.size else None,
        "values": output,
    }
