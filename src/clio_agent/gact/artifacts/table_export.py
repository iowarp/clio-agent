"""CSV/JSON/Parquet export of a table-query's current or full result.

Backs ``POST /v1/artifacts/{artifact_id}/table-export`` (G0: "a server export
route ... next to the table-query route ... reuses the same filter/sort/
columns engine and streams CSV/JSON/Parquet for the current query or the full
dataset"). It is the download sibling of
:mod:`clio_agent.gact.artifacts.table_query`, which backs the small,
interactive, possibly-sampled JSON a chart/map/table renders; this module
never samples -- a download must match exactly what it claims to contain.

Two ``scope`` values:

* ``"current"`` -- the caller's ``columns``/``filter``/``aggregate``/
  ``downsample``/``sort`` apply, run through
  :func:`~clio_agent.gact.artifacts.table_query.compute_processed_table`
  exactly as table-query would process them, but with its protective
  over-limit stride DISABLED (``allow_over_limit_sampling=False``): past
  :func:`table_export_max_rows`, the export is refused with a typed
  ``export_too_large`` error rather than ever silently dropping rows. An
  explicit ``downsample`` the caller set still applies -- that is the
  "current view", not a transfer-guard artifact.
* ``"full"`` -- the raw source table, projected to ``columns`` (or every
  column); every filter/aggregate/downsample/sort is ignored outright.

This module owns no HTTP machinery (see
:mod:`clio_agent.gact.routes.artifact_table_export`) and no artifact custody;
it only ever reads the ``Path`` the route resolves and integrity-checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from clio_agent.gact.artifacts.table_query import (
    QueryCancellation,
    _column_values,
    _read_schema,
    _read_table,
    compute_processed_table,
)
from clio_agent.gact.artifacts.table_query_models import (
    TableFormat,
    TableQueryError,
    TableQueryRequest,
)

ExportFormat = Literal["csv", "json", "parquet"]
ExportScope = Literal["current", "full"]

#: The HTTP media type each export format is served as.
EXPORT_MEDIA_TYPES: dict[ExportFormat, str] = {
    "csv": "text/csv",
    "json": "application/json",
    "parquet": "application/vnd.apache.parquet",
}


@dataclass
class ExportResult:
    """The resolved table to serialize, and the row counts for a response header."""

    table: pa.Table
    columns: list[str]
    total_rows: int
    matched_rows: int


def _full_scope_result(
    source: Path,
    fmt: TableFormat,
    request: TableQueryRequest,
    *,
    cancellation: QueryCancellation,
) -> ExportResult:
    """The raw source table projected to ``request.columns`` (or every column).

    Every filter/aggregate/downsample/sort is ignored -- this is the "whole
    dataset" scope, independent of whatever the current view happens to show.
    """

    schema = _read_schema(source, fmt)
    available = list(schema.names)
    wanted = list(request.columns) if request.columns else available
    missing = [name for name in wanted if name not in available]
    if missing:
        raise TableQueryError(
            400,
            "columns_not_found",
            "one or more requested columns do not exist",
            missing=missing,
            available=available,
        )
    table = _read_table(source, fmt, wanted)
    cancellation.check()
    projected = table.select(wanted)
    return ExportResult(
        table=projected,
        columns=wanted,
        total_rows=projected.num_rows,
        matched_rows=projected.num_rows,
    )


def _current_scope_result(
    source: Path,
    fmt: TableFormat,
    request: TableQueryRequest,
    *,
    max_rows: int,
    cancellation: QueryCancellation,
) -> ExportResult:
    """The filtered/aggregated/sorted "current view", never silently sampled.

    Raises:
        TableQueryError: ``export_too_large`` when the processed result
            exceeds ``max_rows`` and the request set no explicit ``sort``/
            ``downsample`` that would otherwise justify its size (the same
            condition table-query would protectively stride-sample under --
            an export refuses instead, see
            :func:`~clio_agent.gact.artifacts.table_query.compute_processed_table`).
    """

    processed = compute_processed_table(
        source,
        fmt,
        request,
        limit=max_rows,
        cancellation=cancellation,
        allow_over_limit_sampling=False,
    )
    table = processed.table.select(processed.output_columns)
    return ExportResult(
        table=table,
        columns=processed.output_columns,
        total_rows=processed.total_rows,
        matched_rows=processed.matched_rows,
    )


def resolve_export_table(
    source: Path,
    fmt: TableFormat,
    request: TableQueryRequest,
    *,
    scope: ExportScope,
    max_rows: int,
    cancellation: QueryCancellation,
) -> ExportResult:
    """Resolve the table an export request should serialize, per ``scope``."""

    if scope == "full":
        return _full_scope_result(source, fmt, request, cancellation=cancellation)
    return _current_scope_result(source, fmt, request, max_rows=max_rows, cancellation=cancellation)


def _rows_as_json_bytes(table: pa.Table, columns: list[str]) -> bytes:
    """Row-oriented JSON (an array of objects) -- the portable "export" shape,
    distinct from table-query's own column-oriented wire format."""

    values = {name: _column_values(table.column(name)) for name in columns}
    rows = [{name: values[name][index] for name in columns} for index in range(table.num_rows)]
    return json.dumps(rows).encode("utf-8")


def serialize_export(result: ExportResult, fmt: ExportFormat) -> bytes:
    """Serialize ``result.table`` (already projected to ``result.columns``) to bytes."""

    table = result.table.select(result.columns)
    if fmt == "csv":
        sink = pa.BufferOutputStream()
        pacsv.write_csv(table, sink)
        return sink.getvalue().to_pybytes()
    if fmt == "json":
        return _rows_as_json_bytes(table, result.columns)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


def export_filename(artifact_name: str, scope: ExportScope, fmt: ExportFormat) -> str:
    """A reasonable default filename; the client names the download by the
    component's own title instead (G0), this is only the HTTP fallback."""

    stem = Path(artifact_name).stem or "export"
    return f"{stem}.{scope}.{fmt}"


__all__ = [
    "EXPORT_MEDIA_TYPES",
    "ExportFormat",
    "ExportResult",
    "ExportScope",
    "export_filename",
    "resolve_export_table",
    "serialize_export",
]
