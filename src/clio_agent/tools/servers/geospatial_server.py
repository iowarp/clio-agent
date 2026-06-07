"""Geospatial inspection and map-rendering tools."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from fastmcp import FastMCP

from clio_agent.tools.clio_kit_bridge import call_clio_kit_tool
from clio_agent.tools.file_policy import FilePolicyError, validate_read_path

geospatial_server = FastMCP("geospatial")


def _iter_positions(geometry: dict[str, Any]) -> list[tuple[float, float]]:
    """Return all lon/lat positions from a GeoJSON geometry."""
    geom_type = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates")

    def walk(value: Any) -> list[tuple[float, float]]:
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[0], int | float)
            and isinstance(value[1], int | float)
        ):
            return [(float(value[0]), float(value[1]))]
        points: list[tuple[float, float]] = []
        if isinstance(value, list):
            for item in value:
                points.extend(walk(item))
        return points

    if geom_type == "GeometryCollection":
        points: list[tuple[float, float]] = []
        for child in geometry.get("geometries") or []:
            if isinstance(child, dict):
                points.extend(_iter_positions(child))
        return points
    return walk(coordinates)


@geospatial_server.tool()
def inspect_geojson(filepath: str, max_features: int = 25) -> dict[str, Any]:
    """Inspect a GeoJSON file for feature, geometry, bounds, and property metadata.

    Agent story: Use this when a user asks whether spatial features are
    analysis-ready, whether coordinates look sane, or what a geospatial dataset
    covers.

    Args:
        filepath: Path to a GeoJSON file.
        max_features: Maximum representative feature rows to include.

    Returns:
        Dictionary with feature counts, geometry type counts, bounding box,
        property keys, representative feature summaries, and coordinate warnings.
    """
    try:
        safe_path = validate_read_path(filepath)
        max_features = max(1, min(int(max_features or 25), 200))
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
        if payload.get("type") == "FeatureCollection":
            features = payload.get("features") or []
        elif payload.get("type") == "Feature":
            features = [payload]
        else:
            features = [{"type": "Feature", "properties": {}, "geometry": payload}]

        geometry_counts: Counter[str] = Counter()
        property_keys: Counter[str] = Counter()
        all_positions: list[tuple[float, float]] = []
        summaries: list[dict[str, Any]] = []
        invalid_coordinate_count = 0

        for index, feature in enumerate(features):
            if not isinstance(feature, dict):
                continue
            geometry = feature.get("geometry") or {}
            properties = feature.get("properties") or {}
            if not isinstance(geometry, dict):
                continue
            if not isinstance(properties, dict):
                properties = {}
            geom_type = str(geometry.get("type") or "Unknown")
            positions = _iter_positions(geometry)
            geometry_counts[geom_type] += 1
            property_keys.update(str(key) for key in properties)
            all_positions.extend(positions)
            invalid_coordinate_count += sum(
                1 for lon, lat in positions if lon < -180 or lon > 180 or lat < -90 or lat > 90
            )
            if len(summaries) < max_features:
                summaries.append(
                    {
                        "index": index,
                        "geometry_type": geom_type,
                        "position_count": len(positions),
                        "properties": {
                            key: properties[key]
                            for key in sorted(properties)[:8]
                        },
                    }
                )

        bbox: list[float] = []
        if all_positions:
            lons = [lon for lon, _lat in all_positions]
            lats = [lat for _lon, lat in all_positions]
            bbox = [min(lons), min(lats), max(lons), max(lats)]

        return {
            "filepath": str(safe_path),
            "geojson_type": str(payload.get("type") or ""),
            "feature_count": len(features),
            "geometry_types": dict(geometry_counts),
            "property_keys": sorted(property_keys),
            "bbox": bbox,
            "coordinate_count": len(all_positions),
            "invalid_coordinate_count": invalid_coordinate_count,
            "representative_features": summaries,
            "features_truncated": len(features) > max_features,
            "ok": True,
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}


@geospatial_server.tool()
async def render_feature_map(
    layers: list[dict[str, Any]],
    output_path: str = "map.png",
    title: str = "",
    basemap: bool = True,
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    """Render GeoJSON layers (polygons/lines/points) into one map PNG.

    Agent story: Use this when an analysis has assembled spatial features —
    fire perimeters, hazard polygons, monitoring stations, regions — and the
    user wants to *see* them together on a map. Pass each result set as a layer
    of GeoJSON with a style; get back one basemap image. This is the spatial
    counterpart to the CSV/timeseries plotters.

    Rendering runs in the separate clio-kit ``geo`` MCP, so its geospatial
    dependencies stay out of CLIO core.

    Args:
        layers: Ordered layers (later draw on top). Each is a dict with
            ``geojson`` (FeatureCollection/Feature/geometry/list/JSON/path),
            optional ``name``, and optional ``style`` supporting ``facecolor``,
            ``edgecolor``, ``alpha``, ``linewidth``, ``color``, ``markersize``,
            ``zorder``, ``color_by``, ``scale`` (``"epa_aqi"`` or a colormap
            name), ``category_colors``, and ``legend``.
        output_path: Destination PNG path.
        title: Figure title.
        basemap: Add a web-tile basemap (needs network; degrades gracefully).
        bbox: Optional view window ``[min_lon, min_lat, max_lon, max_lat]``.

    Returns:
        Dict with ``status``, ``output_path``, ``bounds``, ``basemap``, and
        per-layer feature counts, or an ``error`` dict if rendering failed.
    """
    resolved = [_resolve_layer_geojson(layer) for layer in (layers or [])]
    renderable = [layer for layer in resolved if _layer_is_renderable(layer)]
    dropped = [str(layer.get("name") or "?") for layer in resolved if not _layer_is_renderable(layer)]
    if not renderable:
        return {
            "error": "No renderable layers: every layer's geojson was missing or unresolvable.",
            "code": "geo_no_layers",
            "dropped_layers": dropped,
        }
    args: dict[str, Any] = {
        "layers": renderable,
        "output_path": _safe_artifact_path(output_path),
        "title": title,
        "basemap": basemap,
    }
    if bbox is not None:
        args["bbox"] = bbox
    result = await call_clio_kit_tool("geo", "render_feature_map", args)
    if dropped and isinstance(result, dict):
        result["dropped_layers"] = dropped  # surface partial-map honesty
    return result


def _layer_is_renderable(layer: dict[str, Any]) -> bool:
    """A layer renders if its geojson is inline JSON or an existing file path."""
    from pathlib import Path

    gj = layer.get("geojson")
    if isinstance(gj, (dict, list)):
        return True
    if isinstance(gj, str):
        text = gj.strip()
        if text[:1] in ("{", "["):
            return True
        return Path(text).is_file()
    return False


def _resolve_layer_geojson(layer: dict[str, Any]) -> dict[str, Any]:
    """Resolve a layer's ``geojson`` when it's a bare filename.

    Acquisition saves each layer to a conventional file in the artifact root
    (e.g. ``fire_perimeter.geojson``). The renderer runs in a separate
    subprocess with a different cwd, so a bare filename must be resolved to its
    absolute path in the artifact root before rendering. Inline GeoJSON
    (dict/list/JSON string) and existing absolute paths are passed through.
    """
    import os
    from pathlib import Path

    import re

    gj = layer.get("geojson")
    if not isinstance(gj, str):
        return layer
    text = gj.strip()
    if not text or text[0] in "{[":
        return layer  # inline JSON
    if Path(text).is_file():
        return layer  # already a usable path
    if "/" in text or "\\" in text:
        return layer  # explicit (if missing) path — leave for the renderer to report
    root = Path(os.environ.get("CLIO_ARTIFACTS_ROOT") or (Path.cwd() / ".clio" / "artifacts" / "geo"))
    if not root.is_dir():
        return layer
    candidate = root / text
    if candidate.is_file():
        return {**layer, "geojson": str(candidate.resolve())}
    # Models drift on exact filenames (fire_perimeter vs active_fire_perimeter).
    # Resolve by keyword from the requested name + the layer name.
    stem = Path(text).stem.lower()
    name = str(layer.get("name") or "").lower()
    keywords = {k for k in re.split(r"[^a-z]+", f"{stem} {name}") if len(k) >= 3}
    for f in sorted(root.glob("*.geojson")):
        fl = f.name.lower()
        if any(k in fl for k in keywords):
            return {**layer, "geojson": str(f.resolve())}
    return layer


def _safe_artifact_path(requested: str) -> str:
    """Force the render output into a writable workspace artifact dir.

    Models routinely invent unwritable absolute paths (e.g. ``/workspace/x.png``).
    The render runs in a separate clio-kit subprocess, so the path must be an
    absolute, writable location. We keep the caller's *filename* but relocate it
    under the artifact root (``CLIO_ARTIFACTS_ROOT`` or ``<cwd>/.clio/artifacts/geo``).
    """
    import os
    from pathlib import Path

    root = Path(os.environ.get("CLIO_ARTIFACTS_ROOT") or (Path.cwd() / ".clio" / "artifacts" / "geo"))
    root.mkdir(parents=True, exist_ok=True)
    name = Path(str(requested or "map.png")).name or "map.png"
    if not name.lower().endswith(".png"):
        name += ".png"
    return str((root / name).resolve())


@geospatial_server.tool()
async def points_in_polygons(
    points_geojson: str,
    polygons_geojson: str,
    buffer_km: float = 0.0,
    point_label_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Spatial overlap: which GeoJSON points fall within (optionally buffered) polygons.

    Agent story: Use this to *compute* impact instead of guessing — e.g. which
    AirNow monitor points lie inside the smoke-forecast polygons. Pass the saved
    layer files (bare filenames resolve in the artifact dir); get back the
    matched points with their properties and a matched_count.

    Runs in the separate clio-kit ``geo`` MCP.
    """
    points = _resolve_layer_geojson({"geojson": points_geojson, "name": "points"})["geojson"]
    polys = _resolve_layer_geojson({"geojson": polygons_geojson, "name": "polygons"})["geojson"]
    args: dict[str, Any] = {
        "points_geojson": points,
        "polygons_geojson": polys,
        "buffer_km": buffer_km,
    }
    if point_label_fields is not None:
        args["point_label_fields"] = point_label_fields
    return await call_clio_kit_tool("geo", "points_in_polygons", args)
