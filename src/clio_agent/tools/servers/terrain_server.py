"""Terrain analysis tools for DEM and point-cloud suitability workflows."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np
from fastmcp import FastMCP

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path, validate_write_path

terrain_server = FastMCP("terrain")

_MAX_DEM_CELLS = 2_000_000
_MAX_POINT_CLOUD_POINTS = 500_000


def _dependency_error(name: str, package: str, next_action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "type": "dependency_missing",
            "code": f"{name}_not_available",
            "message": f"Optional dependency {package!r} is required for this file type.",
            "next_action": next_action,
            "details": {"package": package},
        },
    }


def _finite_values(array: np.ndarray) -> np.ndarray:
    values = np.asarray(array, dtype=float).ravel()
    return values[np.isfinite(values)]


def _summary_stats(values: np.ndarray) -> dict[str, float | int | None]:
    if values.size == 0:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "std": None,
        }
    return {
        "count": int(values.size),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "std": float(np.std(values)),
    }


def _load_dem(path: Path, *, nodata: float | None) -> tuple[np.ndarray, dict[str, Any]]:
    suffix = path.suffix.lower()
    metadata: dict[str, Any] = {"source_format": suffix.lstrip(".") or "unknown"}
    if suffix in {".tif", ".tiff"}:
        try:
            import rasterio  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError(
                "__dependency__:rasterio:Install clio-agent with a geospatial extra or provide a CSV/NPY/NPZ DEM fixture."
            ) from exc
        with rasterio.open(path) as dataset:
            dem = dataset.read(1).astype(float)
            metadata.update(
                {
                    "crs": str(dataset.crs) if dataset.crs else None,
                    "bounds": list(dataset.bounds),
                    "transform": tuple(dataset.transform),
                    "nodata": dataset.nodata,
                }
            )
            if nodata is None and dataset.nodata is not None:
                nodata = float(dataset.nodata)
    elif suffix == ".npy":
        dem = np.load(path).astype(float)
    elif suffix == ".npz":
        payload = np.load(path)
        key = "dem" if "dem" in payload else sorted(payload.files)[0]
        dem = np.asarray(payload[key], dtype=float)
        metadata["array_key"] = key
    else:
        dem = np.loadtxt(path, delimiter=",", dtype=float)

    if dem.ndim != 2:
        raise ValueError(f"DEM must be a two-dimensional grid, got shape {tuple(dem.shape)}.")
    if dem.size > _MAX_DEM_CELLS:
        raise ValueError(
            f"DEM has {dem.size} cells, above the {_MAX_DEM_CELLS} cell safety limit."
        )
    if nodata is not None:
        dem = dem.astype(float, copy=True)
        dem[np.isclose(dem, float(nodata), equal_nan=False)] = np.nan
        metadata["nodata"] = float(nodata)
    return dem, metadata


def _slope_degrees(dem: np.ndarray, cell_size: float) -> np.ndarray:
    filled = np.array(dem, dtype=float, copy=True)
    if not np.isfinite(filled).all():
        finite = _finite_values(filled)
        fill_value = float(np.median(finite)) if finite.size else 0.0
        filled[~np.isfinite(filled)] = fill_value
    gy, gx = np.gradient(filled, float(cell_size), float(cell_size))
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def _aspect_degrees(dem: np.ndarray, cell_size: float) -> np.ndarray:
    filled = np.array(dem, dtype=float, copy=True)
    finite = _finite_values(filled)
    filled[~np.isfinite(filled)] = float(np.median(finite)) if finite.size else 0.0
    gy, gx = np.gradient(filled, float(cell_size), float(cell_size))
    aspect = np.degrees(np.arctan2(-gx, gy))
    return (aspect + 360.0) % 360.0


def _suitability_mask(
    dem: np.ndarray,
    slope: np.ndarray,
    *,
    elevation_min: float | None,
    elevation_max: float | None,
    slope_max_degrees: float | None,
) -> np.ndarray:
    mask = np.isfinite(dem) & np.isfinite(slope)
    if elevation_min is not None:
        mask &= dem >= float(elevation_min)
    if elevation_max is not None:
        mask &= dem <= float(elevation_max)
    if slope_max_degrees is not None:
        mask &= slope <= float(slope_max_degrees)
    return mask


@terrain_server.tool()
def dem_terrain(
    filepath: str,
    cell_size: float = 1.0,
    elevation_min: float | None = None,
    elevation_max: float | None = None,
    slope_max_degrees: float | None = None,
    nodata: float | None = None,
) -> dict[str, Any]:
    """Analyze a DEM grid for elevation, slope, aspect, and site suitability.

    Use this after an agent has a ready DEM or a gridded point-cloud output.
    Supported base formats are CSV numeric grids, NPY, and NPZ with a `dem`
    array. GeoTIFF is supported when optional rasterio is installed; otherwise
    the tool returns a structured dependency result.
    """
    try:
        if cell_size <= 0:
            raise ValueError("cell_size must be positive.")
        safe_path = validate_read_path(filepath)
        try:
            dem, metadata = _load_dem(safe_path, nodata=nodata)
        except RuntimeError as exc:
            marker = "__dependency__:"
            message = str(exc)
            if message.startswith(marker):
                _prefix, package, next_action = message.split(":", 2)
                return _dependency_error(package, package, next_action)
            raise
        slope = _slope_degrees(dem, cell_size)
        aspect = _aspect_degrees(dem, cell_size)
        mask = _suitability_mask(
            dem,
            slope,
            elevation_min=elevation_min,
            elevation_max=elevation_max,
            slope_max_degrees=slope_max_degrees,
        )
        valid_cell_count = int(np.isfinite(dem).sum())
        suitable_cell_count = int(mask.sum())
        suitable_fraction = suitable_cell_count / valid_cell_count if valid_cell_count else 0.0
        rows, cols = np.where(mask)
        examples = [
            {
                "row": int(row),
                "col": int(col),
                "elevation": float(dem[row, col]),
                "slope_degrees": float(slope[row, col]),
                "aspect_degrees": float(aspect[row, col]),
            }
            for row, col in list(zip(rows, cols, strict=False))[:10]
        ]
        return {
            "ok": True,
            "filepath": str(safe_path),
            "shape": [int(dem.shape[0]), int(dem.shape[1])],
            "cell_size": float(cell_size),
            "metadata": metadata,
            "criteria": {
                "elevation_min": elevation_min,
                "elevation_max": elevation_max,
                "slope_max_degrees": slope_max_degrees,
            },
            "valid_cell_count": valid_cell_count,
            "nodata_cell_count": int(dem.size - valid_cell_count),
            "suitable_cell_count": suitable_cell_count,
            "suitable_fraction": suitable_fraction,
            "elevation": _summary_stats(_finite_values(dem)),
            "slope_degrees": _summary_stats(_finite_values(slope)),
            "aspect_degrees": _summary_stats(_finite_values(aspect)),
            "representative_suitable_cells": examples,
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _read_csv_points(path: Path, max_points: int) -> np.ndarray:
    with path.open("r", encoding="utf-8", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        has_header = csv.Sniffer().has_header(sample)
        if has_header:
            reader = csv.DictReader(handle)
            rows = []
            for index, row in enumerate(reader):
                if index >= max_points:
                    break
                rows.append((float(row["x"]), float(row["y"]), float(row["z"])))
            return np.asarray(rows, dtype=float)
        return np.loadtxt(handle, delimiter=",", dtype=float, max_rows=max_points)


def _load_point_cloud(path: Path, *, max_points: int) -> tuple[np.ndarray, dict[str, Any]]:
    suffix = path.suffix.lower()
    metadata: dict[str, Any] = {"source_format": suffix.lstrip(".") or "unknown"}
    if suffix in {".las", ".laz"}:
        try:
            import laspy  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError(
                "__dependency__:laspy:Install clio-agent with a geospatial extra or provide CSV/NPY/NPZ x,y,z points."
            ) from exc
        las = laspy.read(path)
        total = len(las.x)
        take = min(total, max_points)
        points = np.column_stack((las.x[:take], las.y[:take], las.z[:take])).astype(float)
        metadata.update({"point_count_total": int(total), "point_count_sampled": int(take)})
    elif suffix == ".npy":
        points = np.asarray(np.load(path), dtype=float)[:max_points]
    elif suffix == ".npz":
        payload = np.load(path)
        if {"x", "y", "z"}.issubset(payload.files):
            points = np.column_stack((payload["x"], payload["y"], payload["z"])).astype(float)
        else:
            key = "points" if "points" in payload else sorted(payload.files)[0]
            points = np.asarray(payload[key], dtype=float)
            metadata["array_key"] = key
        points = points[:max_points]
    else:
        points = _read_csv_points(path, max_points)
    if points.ndim != 2 or points.shape[1] < 3:
        raise ValueError("Point cloud must have at least three columns: x, y, z.")
    points = np.asarray(points[:, :3], dtype=float)
    finite = np.isfinite(points).all(axis=1)
    return points[finite], metadata


def _grid_points(points: np.ndarray, grid_cell_size: float) -> tuple[np.ndarray, dict[str, Any]]:
    if points.size == 0:
        raise ValueError("Point cloud has no finite x,y,z points.")
    x = points[:, 0]
    y = points[:, 1]
    z = points[:, 2]
    min_x, max_x = float(np.min(x)), float(np.max(x))
    min_y, max_y = float(np.min(y)), float(np.max(y))
    width = int(math.floor((max_x - min_x) / grid_cell_size)) + 1
    height = int(math.floor((max_y - min_y) / grid_cell_size)) + 1
    if width * height > _MAX_DEM_CELLS:
        raise ValueError(
            f"Requested grid has {width * height} cells, above the {_MAX_DEM_CELLS} cell safety limit."
        )
    xi = np.floor((x - min_x) / grid_cell_size).astype(int)
    yi = np.floor((y - min_y) / grid_cell_size).astype(int)
    sums = np.zeros((height, width), dtype=float)
    counts = np.zeros((height, width), dtype=int)
    np.add.at(sums, (yi, xi), z)
    np.add.at(counts, (yi, xi), 1)
    dem = np.full((height, width), np.nan, dtype=float)
    valid = counts > 0
    dem[valid] = sums[valid] / counts[valid]
    return dem, {
        "bounds": {"min_x": min_x, "min_y": min_y, "max_x": float(max_x), "max_y": float(max_y)},
        "grid_shape": [height, width],
        "filled_cell_count": int(valid.sum()),
        "empty_cell_count": int((~valid).sum()),
    }


@terrain_server.tool()
def pointcloud_read(
    filepath: str,
    grid_cell_size: float = 1.0,
    max_points: int = 100_000,
    output_dem_path: str | None = None,
) -> dict[str, Any]:
    """Read an x/y/z point cloud and grid it into a DEM-like surface.

    Supported base formats are CSV with x,y,z columns, NPY, and NPZ. LAS/LAZ is
    supported when optional laspy is installed; otherwise the tool returns a
    structured dependency result. If output_dem_path is provided, the gridded
    surface is written as a CSV DEM for downstream terrain analysis.
    """
    try:
        if grid_cell_size <= 0:
            raise ValueError("grid_cell_size must be positive.")
        max_points = max(1, min(int(max_points or 100_000), _MAX_POINT_CLOUD_POINTS))
        safe_path = validate_read_path(filepath)
        try:
            points, metadata = _load_point_cloud(safe_path, max_points=max_points)
        except RuntimeError as exc:
            marker = "__dependency__:"
            message = str(exc)
            if message.startswith(marker):
                _prefix, package, next_action = message.split(":", 2)
                return _dependency_error(package, package, next_action)
            raise
        dem, grid = _grid_points(points, grid_cell_size)
        output_path = None
        if output_dem_path:
            safe_output = validate_write_path(output_dem_path)
            safe_output.parent.mkdir(parents=True, exist_ok=True)
            np.savetxt(safe_output, dem, delimiter=",", fmt="%.8g")
            output_path = str(safe_output)
        return {
            "ok": True,
            "filepath": str(safe_path),
            "point_count": int(points.shape[0]),
            "grid_cell_size": float(grid_cell_size),
            "metadata": metadata,
            "bounds": grid["bounds"],
            "grid_shape": grid["grid_shape"],
            "filled_cell_count": grid["filled_cell_count"],
            "empty_cell_count": grid["empty_cell_count"],
            "output_dem_path": output_path,
            "x": _summary_stats(points[:, 0]),
            "y": _summary_stats(points[:, 1]),
            "z": _summary_stats(points[:, 2]),
            "gridded_elevation": _summary_stats(_finite_values(dem)),
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
