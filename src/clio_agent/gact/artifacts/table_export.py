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
import tempfile
from collections.abc import Iterator
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
    max_rows: int,
    cancellation: QueryCancellation,
) -> ExportResult:
    """The raw source table projected to ``request.columns`` (or every column).

    Every filter/aggregate/downsample/sort is ignored -- this is the "whole
    dataset" scope, independent of whatever the current view happens to show.
    Still bounded by ``max_rows`` like the current-view scope: "full" means
    "ignore the view's own filter," never "ignore the row ceiling."

    Raises:
        TableQueryError: ``export_too_large`` when the source has more than
            ``max_rows`` rows.
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
    if projected.num_rows > max_rows:
        raise TableQueryError(
            413,
            "export_too_large",
            "export exceeds the configured row ceiling; narrow it with filters",
            rows=projected.num_rows,
            max_rows=max_rows,
        )
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
        return _full_scope_result(
            source, fmt, request, max_rows=max_rows, cancellation=cancellation
        )
    return _current_scope_result(source, fmt, request, max_rows=max_rows, cancellation=cancellation)


#: Rows per chunk while streaming CSV/JSON/Parquet out -- bounds the memory any
#: ONE step holds to roughly one batch's worth of data, never the whole export.
_STREAM_BATCH_ROWS = 8_192
#: A Parquet write buffer larger than this spills from memory to a temp file on
#: disk (``tempfile.SpooledTemporaryFile``) -- Parquet's footer is only valid
#: once the writer closes, so this bounds PEAK MEMORY, not time-to-first-byte.
_PARQUET_SPOOL_MAX_BYTES = 16 * 1024 * 1024


class _ChunkSink:
    """A minimal binary file-like object pyarrow's writers write into.

    Buffers each ``write()`` and hands it back via :meth:`drain` -- the
    bridge that turns a synchronous, whole-buffer pyarrow writer API into an
    incremental generator a ``StreamingResponse`` can send one chunk at a
    time, instead of buffering an entire export in memory before the first
    byte goes out.
    """

    closed = False

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def write(self, data: bytes) -> int:
        chunk = bytes(data)
        self._chunks.append(chunk)
        self._pos += len(chunk)
        return len(chunk)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def drain(self) -> bytes:
        """Return (and clear) everything written since the last drain."""

        if not self._chunks:
            return b""
        joined = b"".join(self._chunks)
        self._chunks.clear()
        return joined


def _stream_csv(result: ExportResult, *, cancellation: QueryCancellation) -> Iterator[bytes]:
    """Yield CSV bytes incrementally, one row-batch at a time."""

    table = result.table.select(result.columns)
    sink = _ChunkSink()
    writer = pacsv.CSVWriter(pa.PythonFile(sink, mode="w"), table.schema)
    try:
        for batch in table.to_batches(max_chunksize=_STREAM_BATCH_ROWS):
            cancellation.check()
            writer.write_batch(batch)
            chunk = sink.drain()
            if chunk:
                yield chunk
    finally:
        writer.close()
    tail = sink.drain()
    if tail:
        yield tail


def _stream_json(result: ExportResult, *, cancellation: QueryCancellation) -> Iterator[bytes]:
    """Yield a row-oriented JSON array (the portable "export" shape, distinct
    from table-query's own column-oriented wire format) incrementally, one
    row-batch at a time."""

    table = result.table.select(result.columns)
    columns = result.columns
    yield b"["
    first = True
    for batch in table.to_batches(max_chunksize=_STREAM_BATCH_ROWS):
        cancellation.check()
        values = {name: _column_values(batch.column(index)) for index, name in enumerate(columns)}
        encoded_rows = [
            json.dumps({name: values[name][row] for name in columns})
            for row in range(batch.num_rows)
        ]
        if not encoded_rows:
            continue
        prefix = "" if first else ","
        yield (prefix + ",".join(encoded_rows)).encode("utf-8")
        first = False
    yield b"]"


def _stream_parquet(result: ExportResult, *, cancellation: QueryCancellation) -> Iterator[bytes]:
    """Yield Parquet bytes: written row-group by row-group to a size-bounded
    spooled temp file, then streamed back once the writer (and so the
    footer) closes.
    """

    table = result.table.select(result.columns)
    with tempfile.SpooledTemporaryFile(max_size=_PARQUET_SPOOL_MAX_BYTES) as spooled:
        writer = pq.ParquetWriter(spooled, table.schema)
        try:
            for batch in table.to_batches(max_chunksize=_STREAM_BATCH_ROWS):
                cancellation.check()
                writer.write_table(
                    pa.Table.from_batches([batch], schema=table.schema),
                    row_group_size=batch.num_rows or 1,
                )
        finally:
            writer.close()
        cancellation.check()
        spooled.seek(0)
        while chunk := spooled.read(65_536):
            yield chunk


def stream_export(
    result: ExportResult, fmt: ExportFormat, *, cancellation: QueryCancellation
) -> Iterator[bytes]:
    """Serialize ``result.table`` (already projected to ``result.columns``) as
    an incremental stream of bytes, checking ``cancellation`` between chunks
    so an abandoned download stops promptly instead of finishing unread work.
    """

    if fmt == "csv":
        yield from _stream_csv(result, cancellation=cancellation)
    elif fmt == "json":
        yield from _stream_json(result, cancellation=cancellation)
    else:
        yield from _stream_parquet(result, cancellation=cancellation)


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
    "stream_export",
]
