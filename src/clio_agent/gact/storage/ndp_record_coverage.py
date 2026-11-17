"""Where and when an NDP record says its data is, read from whichever field it uses.

NDP organizations store location as ``spatial`` (GeoJSON), ``dataBbox`` or not at
all, and time as ``temporal``, ``start_time``/``end_time``,
``startDateTime``/``endDateTime``, only ``issueDate``, or nowhere. Discovery dropped
the CKAN extras that hold them, so an agent checking coverage had to fetch each
record again through another server. This module copies those facts into the
search result as the record states them: the source field is named, values are not
reinterpreted, and nothing is decided from them. A box that spans most of the globe
is reported with its span so the agent can see it says nothing about a region.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

LOCATION_FIELDS = ("spatial", "dataBbox", "bbox", "spatial_bbox", "ext_bbox")
TIME_PAIRS = (
    ("start_time", "end_time"),
    ("startDateTime", "endDateTime"),
    ("temporal_start", "temporal_end"),
)
MAX_TEXT = 200


def _fields(record: Mapping[str, Any]) -> dict[str, Any]:
    values = {k: v for k, v in record.items() if k != "extras"}
    extras = record.get("extras")
    if isinstance(extras, list):
        for item in extras:
            if isinstance(item, Mapping) and isinstance(item.get("key"), str):
                values.setdefault(item["key"], item.get("value"))
    return values


def _points(value: Any) -> Iterable[tuple[float, float]]:
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
            yield float(value[0]), float(value[1])
        else:
            for item in value:
                yield from _points(item)


def _bbox(raw: Any) -> list[float] | None:
    """A [west, south, east, north] box from GeoJSON or a comma-separated box."""
    value = raw
    if isinstance(value, str):
        text = value.strip()
        try:
            value = json.loads(text)
        except ValueError:
            parts = [p for p in text.replace(";", ",").split(",") if p.strip()]
            try:
                numbers = [float(p) for p in parts]
            except ValueError:
                return None
            return numbers if len(numbers) == 4 else None
    if isinstance(value, Mapping):
        if "bbox" in value and isinstance(value["bbox"], list) and len(value["bbox"]) >= 4:
            box = value["bbox"]
            return [float(box[0]), float(box[1]), float(box[-2]), float(box[-1])]
        coordinates = value.get("coordinates")
        if coordinates is None and value.get("type") == "Feature":
            coordinates = (value.get("geometry") or {}).get("coordinates")
        points = list(_points(coordinates))
        if points:
            xs, ys = zip(*points, strict=True)
            return [min(xs), min(ys), max(xs), max(ys)]
    if isinstance(value, list) and len(value) == 4:
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return None
    return None


def _clip(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return text[:MAX_TEXT]


def record_coverage(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record's own location and time statements, with the field each came from."""
    fields = _fields(record)
    location: dict[str, Any] | None = None
    for name in LOCATION_FIELDS:
        if fields.get(name) in (None, "", [], {}):
            continue
        box = _bbox(fields[name])
        location = {"field": name}
        if box is None:
            location["unparsed"] = _clip(fields[name])
        else:
            location["bbox"] = [round(v, 6) for v in box]
            location["span_degrees"] = [round(box[2] - box[0], 3), round(box[3] - box[1], 3)]
        break
    time: dict[str, Any] | None = None
    temporal = fields.get("temporal")
    if temporal not in (None, "", {}):
        parsed = temporal
        if isinstance(temporal, str):
            try:
                parsed = json.loads(temporal)
            except ValueError:
                parsed = temporal
        if isinstance(parsed, Mapping):
            time = {
                "field": "temporal",
                "start": _clip(parsed.get("startTime") or parsed.get("start") or ""),
                "end": _clip(parsed.get("endTime") or parsed.get("end") or ""),
            }
        else:
            time = {"field": "temporal", "text": _clip(parsed)}
    if time is None:
        for start, end in TIME_PAIRS:
            if fields.get(start) or fields.get(end):
                time = {
                    "field": f"{start}/{end}",
                    "start": _clip(fields.get(start) or ""),
                    "end": _clip(fields.get(end) or ""),
                }
                break
    if time is None and fields.get("issueDate"):
        time = {"field": "issueDate", "issued": _clip(fields["issueDate"])}
    return {"location": location, "time": time}
