"""Select the tiles of a multi-tile NDP dataset that cover a region.

OpenTopography and USGS 3DEP datasets carry tens to a thousand raster tiles, and
the catalog says nothing about where each tile lies. This module reads that from
the data itself, without downloading any tile body:

* the dataset's own VRT mosaic, when it has one: one request gives every tile's
  footprint (``DstRect`` in the mosaic's geotransform);
* otherwise each tile's raster header through GDAL's ranged HTTP reads, within a
  per-call budget.

Tiles whose footprint cannot be read are reported as undetermined with the reason;
they are never silently dropped or silently included. The result's resource IDs
are exactly the ones ``connected_data_connect`` accepts.
"""

from __future__ import annotations

import concurrent.futures
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from clio_agent.gact.storage.ndp_catalog import NDPDataset, NDPResource
from clio_agent.gact.storage.ndp_source import credential_target
from clio_agent.gact.storage.ndp_tls import system_tls

# A mosaic description is XML; the largest seen (OpenTopography, 2,084 tiles) is < 2 MiB.
MAX_VRT_BYTES = 16 * 1024 * 1024
MAX_VRTS = 4
# Header reads cost one or two ranged requests each.
MAX_HEADER_READS = 200
HEADER_WORKERS = 8
RASTER_SUFFIXES = (".tif", ".tiff", ".img", ".adf", ".jp2", ".asc", ".dem", ".flt")

Bounds = tuple[float, float, float, float]


def _suffix(resource: NDPResource) -> str:
    path = unquote(urlsplit(resource.url).path).casefold()
    return "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""


def is_mosaic(resource: NDPResource) -> bool:
    """A GDAL virtual mosaic that names the dataset's tiles."""
    return _suffix(resource) == ".vrt" or resource.format.casefold() == "vrt"


def is_tile(resource: NDPResource) -> bool:
    """A raster tile, judged from its declared format or, when blank, its extension."""
    fmt = resource.format.casefold()
    return (
        any(token in fmt for token in ("tif", "geotiff", "arcgrid", "img"))
        or _suffix(resource) in RASTER_SUFFIXES
    )


def parse_vrt(text: str, vrt_url: str) -> tuple[str, list[tuple[str, Bounds]]]:
    """Return the mosaic CRS (WKT or authority string) and each source's footprint.

    Footprints are in the mosaic CRS. Raises ValueError for a rotated geotransform,
    which this reader does not interpret.
    """
    root = ET.fromstring(text)
    if root.tag != "VRTDataset":
        raise ValueError("not a VRTDataset")
    srs = (root.findtext("SRS") or "").strip()
    gt_text = root.findtext("GeoTransform")
    if not srs or not gt_text:
        raise ValueError("the mosaic declares no CRS or geotransform")
    gt = [float(value) for value in gt_text.replace(",", " ").split()]
    if len(gt) != 6 or gt[2] or gt[4]:
        raise ValueError("the mosaic geotransform is rotated or malformed")
    footprints: dict[str, Bounds] = {}
    for source in root.iter():
        name, rect = source.find("SourceFilename"), source.find("DstRect")
        if name is None or rect is None or not (name.text or "").strip():
            continue
        filename = (name.text or "").strip()
        if filename.startswith("/vsicurl/"):
            url = filename[len("/vsicurl/") :]
        elif name.get("relativeToVRT") == "1":
            url = urljoin(vrt_url, filename)
        elif filename.startswith(("http://", "https://")):
            url = filename
        else:
            continue  # an absolute local path on the producer's machine
        x0, y0 = float(rect.get("xOff", 0)), float(rect.get("yOff", 0))
        xs, ys = float(rect.get("xSize", 0)), float(rect.get("ySize", 0))
        xa, xb = gt[0] + x0 * gt[1], gt[0] + (x0 + xs) * gt[1]
        ya, yb = gt[3] + y0 * gt[5], gt[3] + (y0 + ys) * gt[5]
        bounds = (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))
        previous = footprints.get(url)
        footprints[url] = (
            bounds
            if previous is None
            else (
                min(previous[0], bounds[0]),
                min(previous[1], bounds[1]),
                max(previous[2], bounds[2]),
                max(previous[3], bounds[3]),
            )
        )
    return srs, list(footprints.items())


def _fetch_vrt(url: str) -> str:
    with httpx.Client(
        timeout=httpx.Timeout(30, connect=10), follow_redirects=True, verify=system_tls()
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(65536):
                body.extend(chunk)
                if len(body) > MAX_VRT_BYTES:
                    raise ValueError("the mosaic description exceeds 16 MiB")
    return body.decode("utf-8", errors="replace")


def _header(url: str) -> tuple[str, Bounds]:
    import rasterio  # noqa: PLC0415 - GDAL is loaded only when a header must be read

    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="20", GDAL_HTTP_MAX_RETRY="1"
    ):
        with rasterio.open("/vsicurl/" + url) as dataset:
            if dataset.crs is None:
                raise ValueError("the tile declares no CRS")
            return dataset.crs.to_wkt(), tuple(dataset.bounds)  # type: ignore[return-value]


def _to_wgs84(crs: str, bounds: Bounds) -> Bounds:
    from rasterio.warp import transform_bounds  # noqa: PLC0415

    return tuple(transform_bounds(crs, "EPSG:4326", *bounds, densify_pts=21))  # type: ignore[return-value]


def _intersects(a: Bounds, b: Bounds) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _resources_for(url: str, tiles: list[NDPResource]) -> list[NDPResource]:
    """Tiles named by a mosaic source: the file itself, or an ArcGrid directory's files.

    ArcGrid mosaics name a coverage directory, sometimes as a glob (``dem_be/*.adf``).
    """
    if "*" in url.rsplit("/", 1)[-1]:
        url = url.rsplit("/", 1)[0]
    prefix = url.rstrip("/") + "/"
    return [item for item in tiles if item.url == url or item.url.startswith(prefix)]


def tiles_covering(
    dataset: NDPDataset, region: Bounds, *, header_offset: int = 0
) -> dict[str, Any]:
    """Classify every tile of ``dataset`` against a WGS84 ``region`` (west, south, east, north)."""
    west, south, east, north = region
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Give the region as west < east and south < north in degrees")
    if header_offset < 0:
        raise ValueError("header_offset must be nonnegative")
    tiles = [item for item in dataset.resources if is_tile(item)]
    mosaics = [item for item in dataset.resources if is_mosaic(item)]
    readable = lambda item: urlsplit(item.url).scheme in {"http", "https"} and not (  # noqa: E731
        credential_target(dataset.endpoint, item.url)
    )
    footprint: dict[str, Bounds] = {}
    undetermined: dict[str, str] = {}
    methods: list[str] = []
    notes: list[str] = []

    for mosaic in mosaics[:MAX_VRTS]:
        if not readable(mosaic):
            notes.append(f"{mosaic.name}: not read; it is served behind a signed-in Endpoint")
            continue
        try:
            crs, sources = parse_vrt(_fetch_vrt(mosaic.url), mosaic.url)
            for url, extent in sources:
                matched = _resources_for(url, tiles)
                if not matched:
                    continue
                wgs = _to_wgs84(crs, extent)
                for item in matched:
                    footprint.setdefault(item.id, wgs)
            methods.append(f"mosaic {mosaic.name} ({len(sources)} sources)")
        except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
            status = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else str(exc) or type(exc).__name__
            )
            notes.append(f"{mosaic.name}: mosaic could not be read ({status})")
    if len(mosaics) > MAX_VRTS:
        notes.append(f"Only the first {MAX_VRTS} of {len(mosaics)} mosaics were read")

    pending = [item for item in tiles if item.id not in footprint]
    # ArcGrid companion files share one footprint, read from their directory, not each file.
    pending_headers = [item for item in pending if readable(item) and _suffix(item) != ".adf"]
    for item in pending:
        if not readable(item):
            undetermined[item.id] = "served behind a signed-in Endpoint; header not read"
        elif _suffix(item) == ".adf":
            undetermined[item.id] = "ArcGrid file outside any readable mosaic"
    batch = pending_headers[header_offset : header_offset + MAX_HEADER_READS]
    for item in pending_headers[:header_offset]:
        undetermined[item.id] = "header not read in this call (earlier header page)"
    for item in pending_headers[header_offset + MAX_HEADER_READS :]:
        undetermined[item.id] = "header not read in this call; ask for the next header page"
    if batch:
        methods.append(f"{len(batch)} raster headers")
        with concurrent.futures.ThreadPoolExecutor(HEADER_WORKERS) as pool:
            futures = {pool.submit(_header, item.url): item for item in batch}
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    crs, native = future.result()
                    footprint[item.id] = _to_wgs84(crs, native)
                except (OSError, ValueError, RuntimeError) as exc:  # recorded per tile
                    undetermined[item.id] = f"header unreadable: {str(exc)[:160]}"

    by_id = {item.id: item for item in tiles}
    covering, outside = [], 0
    for item in tiles:
        bounds = footprint.get(item.id)
        if bounds is None:
            continue
        if _intersects(bounds, region):
            covering.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "path": item.path,
                    "bounds": [round(value, 6) for value in bounds],
                }
            )
        else:
            outside += 1
    remaining = len(pending_headers) - header_offset - len(batch)
    return {
        "dataset": {
            "id": dataset.id,
            "title": dataset.title,
            "organization": dataset.organization,
            "endpoint_id": dataset.endpoint.id,
        },
        "region": {"west": west, "south": south, "east": east, "north": north},
        "tiles": len(tiles),
        "covering": covering,
        "outside": outside,
        "undetermined": [
            {"id": key, "name": by_id[key].name, "reason": reason}
            for key, reason in undetermined.items()
        ],
        "other_resources": len(dataset.resources) - len(tiles),
        "methods": methods,
        "notes": notes,
        "next_header_offset": header_offset + len(batch) if remaining > 0 else None,
    }


def tile_coverage_presentation(
    args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """Row: tiles covering a region; expansion: the covering tiles, gaps and method."""
    from clio_agent.gact.storage.ndp_presentations import _row, _table, _text  # noqa: PLC0415

    row = _row(result, structured)
    if "covering" not in row:
        return {
            "summary": "Tile coverage unavailable",
            "blocks": [
                _text(
                    "unavailable",
                    "Result",
                    row.get("message") or "No tile coverage result was recorded.",
                    severity="warning",
                )
            ],
        }
    dataset, region = row.get("dataset", {}), row.get("region", {})
    covering, undetermined = row.get("covering", []), row.get("undetermined", [])
    title = dataset.get("title") or dataset.get("id", "dataset")
    box = ", ".join(
        f"{region.get(key)}" for key in ("west", "south", "east", "north") if key in region
    )
    blocks = [
        _text(
            "request",
            "Region",
            f"{title}\nWest, south, east, north: {box}"
            + (f"\nOrganization: {dataset['organization']}" if dataset.get("organization") else ""),
        ),
        _text(
            "counts",
            "Coverage",
            f"{len(covering)} of {row.get('tiles', 0)} tiles cover the region; "
            f"{row.get('outside', 0)} lie outside; {len(undetermined)} undetermined."
            + (f"\nRead from: {'; '.join(row['methods'])}" if row.get("methods") else ""),
            **({"severity": "warning"} if undetermined else {}),
        ),
    ]
    if covering:
        blocks.append(
            {
                "id": "covering",
                "type": "markdown",
                "label": "Covering tiles",
                "text": _table(
                    ["Tile", "West", "South", "East", "North", "Resource ID"],
                    [
                        [item.get("name", ""), *item.get("bounds", []), item.get("id", "")]
                        for item in sorted(covering, key=lambda c: c.get("name", ""))
                    ],
                ),
            }
        )
    elif not undetermined:
        blocks.append(
            _text("empty", "Covering tiles", "No tile of this dataset covers the region.")
        )
    if undetermined:
        blocks.append(
            {
                "id": "undetermined",
                "type": "markdown",
                "label": "Undetermined tiles",
                "text": _table(
                    ["Tile", "Reason"],
                    [[item.get("name", ""), item.get("reason", "")] for item in undetermined],
                ),
            }
        )
    blocks.extend(
        _text(f"note-{index}", "Note", note, severity="warning")
        for index, note in enumerate(row.get("notes", []))
    )
    if row.get("next_header_offset") is not None:
        blocks.append(
            _text(
                "next-page",
                "More tiles",
                "More tile headers remain; call again with header_offset="
                f"{row['next_header_offset']}.",
            )
        )
    if row.get("other_resources"):
        blocks.append(
            _text(
                "other",
                "Other resources",
                f"{row['other_resources']} non-tile resources (mosaics, footprints, pages) "
                "were not classified.",
            )
        )
    return {
        "summary": f"{len(covering)} of {row.get('tiles', 0)} tiles cover the region",
        "blocks": blocks,
        "subject": "request",
    }


__all__ = [
    "is_mosaic",
    "is_tile",
    "parse_vrt",
    "tile_coverage_presentation",
    "tiles_covering",
]
