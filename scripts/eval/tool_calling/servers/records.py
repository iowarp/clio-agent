"""``records``: paginated station readings reachable only through tool calls (P5 to P7).

``list_records`` returns ids, stations and times but no readings; the reading
is only in ``get_record``. A seeded subset of readings is negative, so the
question "which records have a negative reading, and how many?" needs every
record fetched. :func:`generate_records` is also the answer-key source.
"""

from __future__ import annotations

import math
import random
from typing import Any

try:
    from .toolkit import CallLog, ToolSpec, build_server, p, t
except ImportError:  # run from a plugin folder
    from toolkit import CallLog, ToolSpec, build_server, p, t  # type: ignore[no-redef]

SERVER_NAME = "records"
MAX_PAGE_SIZE = 100
_STATIONS = ("alder", "birch", "cedar", "elm", "fir", "hazel", "larch", "maple", "oak", "pine")

SPECS: list[ToolSpec] = [
    t(
        "list_records",
        "list",
        "List stored records one page at a time: id, station and time for each.",
        p("page", "int", "Page number, starting at 1.", 1),
        p("page_size", "int", f"Records per page, at most {MAX_PAGE_SIZE}.", 50),
    ),
    t(
        "get_record",
        "read",
        "Every field of one record, including its reading.",
        p("record_id", "str", "Record id, e.g. REC-00017."),
    ),
]


def generate_records(count: int, seed: int) -> list[dict[str, Any]]:
    """``count`` records; about 8% (at least two) have a negative reading."""
    rng = random.Random(seed * 100_003 + count)
    negatives = set(rng.sample(range(count), max(2, round(count * 0.08))))
    rows = []
    for index in range(count):
        magnitude = round(rng.uniform(0.5, 98.0), 2)
        rows.append(
            {
                "record_id": f"REC-{index + 1:05d}",
                "station": rng.choice(_STATIONS),
                "recorded_at": f"2026-10-{1 + index % 28:02d}T{index % 24:02d}:{index % 60:02d}:00Z",
                "reading": -magnitude if index in negatives else magnitude,
                "unit": "kPa",
                "quality": rng.choice(("good", "good", "good", "suspect")),
            }
        )
    return rows


def negative_answer(count: int, seed: int) -> dict[str, Any]:
    """The answer key: ids with a negative reading, and how many."""
    ids = [row["record_id"] for row in generate_records(count, seed) if row["reading"] < 0]
    return {"count": len(ids), "ids": ids, "total_records": count}


class _Records:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.by_id = {row["record_id"]: row for row in rows}

    def __call__(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        if spec.name == "get_record":
            row = self.by_id.get(str(args.get("record_id") or ""))
            if row is None:
                return {"ok": False, "error": f"no record {args.get('record_id')!r}"}
            return dict(row)
        size = max(1, min(MAX_PAGE_SIZE, int(args.get("page_size") or 50)))
        page = max(1, int(args.get("page") or 1))
        pages = max(1, math.ceil(len(self.rows) / size))
        chunk = self.rows[(page - 1) * size : page * size]
        return {
            "page": page,
            "page_size": size,
            "total": len(self.rows),
            "total_pages": pages,
            "records": [
                {k: row[k] for k in ("record_id", "station", "recorded_at")} for row in chunk
            ],
        }


def build(count: int, seed: int, log: CallLog) -> Any:
    """The ``records`` FastMCP server over ``count`` seeded records."""
    return build_server(
        SERVER_NAME,
        "Stored station records. list_records pages through ids; get_record returns one "
        "record with its reading.",
        SPECS,
        _Records(generate_records(count, seed)),
        log,
    )
