"""Bounded server-side query / downsample engine over one tabular artifact.

Backs ``POST /v1/artifacts/{artifact_id}/table-query``: a chart asks for a few
columns of a CSV or Parquet artifact, optionally filtered, aggregated and
downsampled, and receives a small column-oriented JSON payload instead of the
whole file. Execution is pyarrow-only and runs in a fixed order, matching the
shared ``DataQuery`` $def's own documented contract (clio-schemas):

    read (projected) -> filter -> aggregate -> downsample -> sort -> offset/limit

``limit`` is a per-response TRANSFER guard, never a ceiling on what a caller
can reach: an ``offset`` request plainly PAGES the full (filtered/sorted)
result (never sampled, only capped if the page itself overflows); a
one-shot request (no ``offset``) that still exceeds ``limit`` gets an
even-stride sample across the WHOLE range instead of a biased head slice —
see :func:`~clio_agent.gact.artifacts.table_query_downsample.apply_over_limit_stride`.
Sorting runs AFTER downsampling (not before): an explicit or automatic
sample picks its rows from the filtered/aggregated set first, and the
caller's ``sort`` then orders exactly the rows that made it into the
response. A query's own client disconnecting, or a configured wall-clock
backstop, cancels it between stages (see :class:`QueryCancellation`).

This module owns no HTTP machinery: it raises :class:`TableQueryError` with a
status code and a typed error code, and the route turns that into the standard
GACT error envelope. The caller resolves (and integrity-checks) the artifact
bytes; this module only ever reads the ``Path`` it is handed.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from clio_agent.gact.artifacts.table_query_downsample import (
    apply_downsample,
    apply_over_limit_stride,
    reduce_evenly_per_entity_for_limit,
)
from clio_agent.gact.artifacts.table_query_models import (
    DEFAULT_LIMIT,
    RowKey,
    Scalar,
    TableAggregate,
    TableDownsample,
    TableFilter,
    TableFormat,
    TableMetric,
    TableQueryCancelled,
    TableQueryError,
    TableQueryRequest,
    TableQueryTimeout,
    TableSort,
    table_format_for,
)


@dataclass
class QueryCancellation:
    """The two ways one table-query execution stops early.

    ``cancel_event`` (PRIMARY, owner ruling): set by the route when its own
    HTTP client disconnects -- checked between EVERY stage and, in the
    per-entity downsample loop, on every iteration (not only between
    stages), so an abandoned query stops promptly. ``deadline`` (SECONDARY):
    a configured wall-clock backstop (``artifacts.table_query_timeout_s``)
    for a client that is still connected and waiting but has run too long --
    never the primary mechanism, and never a statement about how much data
    is reachable.
    """

    deadline: float
    timeout_s: float
    cancel_event: "threading.Event | None" = None

    def check(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise TableQueryCancelled()
        if time.monotonic() > self.deadline:
            raise TableQueryTimeout(self.timeout_s)


def _read_schema(source: Path, fmt: TableFormat) -> pa.Schema:
    try:
        if fmt == "parquet":
            return pq.read_schema(source)
        reader = pacsv.open_csv(source)
        try:
            return reader.schema
        finally:
            reader.close()
    except (pa.ArrowInvalid, OSError) as exc:
        raise TableQueryError(
            422, "table_read_failed", f"{fmt} artifact could not be parsed", detail=str(exc)
        ) from exc


def _source_columns_needed(request: TableQueryRequest, available: list[str]) -> list[str]:
    """Every source column the query touches, in first-use order (for projection).

    An omitted ``columns`` (only possible without ``aggregate`` — the model
    validator requires it otherwise) means "every column": ``available`` (the
    source table's own schema) is used instead, so the server returns the
    whole row set (bounded by ``limit``) rather than refusing the request.
    """

    wanted: list[str] = []

    def add(name: str | None) -> None:
        if name is not None and name not in wanted:
            wanted.append(name)

    if request.aggregate is not None:
        # With aggregate set, ``columns`` selects OUTPUT columns (checked
        # against the aggregate's real output names AFTER it runs, in
        # run_table_query) -- e.g. "value_mean" never exists in the SOURCE
        # table, so it must never be requested as a source projection.
        # Only the aggregate's own inputs need reading from the source.
        for key in request.aggregate.group_by:
            add(key)
        for metric in request.aggregate.metrics:
            add(metric.column)
    elif request.columns:
        for column in request.columns:
            add(column)
    else:
        for column in available:
            add(column)
    for flt in request.filters:
        add(flt.column)
    if request.aggregate is None:
        # With aggregate set, downsample/sort target the AGGREGATE'S OWN
        # output columns (validated separately, stage="aggregated") -- they
        # are not necessarily raw source columns, so never added here.
        if request.downsample is not None:
            add(request.downsample.entity_column)
            add(request.downsample.x)
            add(request.downsample.y)
        for entry in request.sort:
            add(entry.column)
    return wanted


def _validate_downsample_columns(
    request: TableQueryRequest, available: list[str], *, stage: str
) -> None:
    downsample = request.downsample
    if downsample is None:
        return
    missing = [
        name
        for name in (downsample.entity_column, downsample.x, downsample.y)
        if name is not None and name not in available
    ]
    if missing:
        raise TableQueryError(
            400,
            "columns_not_found",
            f"downsample references columns absent from the {stage} table",
            missing=missing,
            available=available,
        )


def _validate_sort_columns(request: TableQueryRequest, available: list[str], *, stage: str) -> None:
    missing = [entry.column for entry in request.sort if entry.column not in available]
    if missing:
        raise TableQueryError(
            400,
            "columns_not_found",
            f"sort references columns absent from the {stage} table",
            missing=missing,
            available=available,
        )


def _validate_requested_output_columns(
    request: TableQueryRequest, available: list[str], *, stage: str
) -> None:
    """With ``aggregate`` set, ``columns`` selects OUTPUT columns -- checked
    against the aggregate's REAL output names (``available``), never the
    source schema (a metric output like ``value_mean`` never exists there)."""

    missing = [name for name in request.columns if name not in available]
    if missing:
        raise TableQueryError(
            400,
            "columns_not_found",
            f"one or more requested columns do not exist in the {stage} result",
            missing=missing,
            available=available,
        )


# --------------------------------------------------------------------------- #
# Execution stages
# --------------------------------------------------------------------------- #


def _read_table(source: Path, fmt: TableFormat, columns: list[str]) -> pa.Table:
    try:
        if fmt == "parquet":
            table = pq.read_table(source, columns=columns)
        else:
            table = pacsv.read_csv(
                source,
                # Empty / NA cells are nulls in every column, text included.
                convert_options=pacsv.ConvertOptions(
                    include_columns=columns, strings_can_be_null=True
                ),
            )
    except (pa.ArrowInvalid, OSError) as exc:
        raise TableQueryError(
            422, "table_read_failed", f"{fmt} artifact could not be parsed", detail=str(exc)
        ) from exc
    # Dictionary (categorical) columns decode to their value type so every
    # compute kernel below sees plain arrays.
    for index, field in enumerate(table.schema):
        if pa.types.is_dictionary(field.type):
            decoded = pc.cast(table.column(index), field.type.value_type)
            table = table.set_column(index, field.name, decoded)
    return table


def _is_numeric(dtype: pa.DataType) -> bool:
    return pa.types.is_integer(dtype) or pa.types.is_floating(dtype) or pa.types.is_decimal(dtype)


def _scalar_for(column: str, dtype: pa.DataType, value: Scalar) -> pa.Scalar:
    """Coerce a JSON scalar to something comparable against a column of ``dtype``."""

    scalar = pa.scalar(value)
    if scalar.type == dtype:
        return scalar
    if _is_numeric(dtype) and _is_numeric(scalar.type):
        return scalar  # compute kernels promote mixed numeric comparisons
    try:
        return scalar.cast(dtype)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as exc:
        raise TableQueryError(
            400,
            "invalid_filter_value",
            f"filter value {value!r} is not comparable with column {column!r}",
            column=column,
            type=str(dtype),
        ) from exc


def _is_in_mask(column: str, values: pa.ChunkedArray, candidates: list[Scalar]) -> pa.Array:
    dtype = values.type
    try:
        value_set = pa.array(candidates)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise TableQueryError(
            400,
            "invalid_filter_value",
            "in filter values must share one type",
            column=column,
        ) from exc
    try:
        if value_set.type != dtype:
            if _is_numeric(dtype) and _is_numeric(value_set.type):
                values = pc.cast(values, pa.float64())
                value_set = pc.cast(value_set, pa.float64())
            else:
                value_set = pc.cast(value_set, dtype)
        return pc.is_in(values, value_set=value_set)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as exc:
        raise TableQueryError(
            400,
            "invalid_filter_value",
            f"in filter values are not comparable with column {column!r}",
            column=column,
            type=str(dtype),
        ) from exc


def _filter_mask(table: pa.Table, flt: TableFilter) -> Any:
    values = table.column(flt.column)
    dtype = values.type
    try:
        if flt.op == "eq":
            return pc.equal(values, _scalar_for(flt.column, dtype, flt.value))
        if flt.op == "in":
            return _is_in_mask(flt.column, values, list(flt.value))
        if flt.op == "range":
            low, high = flt.value
            mask: Any = pc.is_valid(values)
            if pa.types.is_floating(dtype):
                mask = pc.and_(mask, pc.invert(pc.is_nan(values)))
            if low is not None:
                mask = pc.and_(mask, pc.greater_equal(values, _scalar_for(flt.column, dtype, low)))
            if high is not None:
                mask = pc.and_(mask, pc.less_equal(values, _scalar_for(flt.column, dtype, high)))
            return mask
        if flt.op == "contains":
            return pc.match_substring(values, str(flt.value), ignore_case=True)
        null_mask = pc.is_null(values, nan_is_null=True)
        return null_mask if flt.value in (None, True) else pc.invert(null_mask)
    except (pa.ArrowNotImplementedError, pa.ArrowTypeError, pa.ArrowInvalid) as exc:
        raise TableQueryError(
            400,
            "invalid_filter_value",
            f"filter {flt.op!r} is not supported on column {flt.column!r}",
            column=flt.column,
            type=str(dtype),
        ) from exc


def _apply_filters(table: pa.Table, filters: list[TableFilter]) -> pa.Table:
    """AND every predicate together, including several on the SAME column.

    Each filter is evaluated independently against ``table``'s own values and
    combined with ``pc.and_`` — a caller-composed request (e.g. an agent's own
    base ``dataQuery`` filter plus a UI-added one on the same column) narrows
    correctly, never overwrites or shadows an earlier predicate on that column.
    """

    if not filters:
        return table
    mask: Any = None
    for flt in filters:
        current = _filter_mask(table, flt)
        mask = current if mask is None else pc.and_(mask, current)
    return table.filter(mask, null_selection_behavior="drop")


#: The synthetic stable row-key column name added to every non-aggregated
#: query result (G0: "views over the SAME dataUri link automatically, through
#: a stable server row key"). It identifies a row by its position in the
#: freshly-READ, pre-filter/sort table -- i.e. the underlying dataset row, not
#: this particular query's filtered/sorted position -- so two different
#: queries over the SAME artifact (different columns, different filters)
#: still agree on which row is "row 7" and can link a selection across them.
ROW_KEY_BASE_NAME = "__row"


def _row_key_column_name(existing: list[str]) -> str:
    """A column name for the synthetic row key that never collides with a real one."""

    name = ROW_KEY_BASE_NAME
    while name in existing:
        name += "_"
    return name


def _apply_sort(table: pa.Table, sort: list[TableSort]) -> pa.Table:
    """Apply a (possibly compound) sort; a no-op when ``sort`` is empty."""

    if not sort:
        return table
    keys = [(entry.column, "descending" if entry.desc else "ascending") for entry in sort]
    try:
        return table.sort_by(keys)
    except (pa.ArrowNotImplementedError, pa.ArrowTypeError, pa.ArrowInvalid) as exc:
        raise TableQueryError(
            400, "invalid_sort", "sort is not supported on these columns", detail=str(exc)
        ) from exc


_ARROW_AGG = {"mean": "mean", "min": "min", "max": "max", "count": "count", "sum": "sum"}


def _median_of_lists(lists: pa.ChunkedArray | pa.Array) -> pa.Array:
    """Exact median of each list, ignoring nulls (empty -> null)."""

    combined = lists.combine_chunks() if isinstance(lists, pa.ChunkedArray) else lists
    offsets = combined.offsets.to_numpy(zero_copy_only=False)
    flat = combined.values
    flat_values = pc.cast(flat, pa.float64()).to_numpy(zero_copy_only=False)
    valid = ~flat.is_null().to_numpy(zero_copy_only=False)
    out: list[float | None] = []
    for start, end in zip(offsets[:-1], offsets[1:], strict=True):
        chunk = flat_values[start:end][valid[start:end]]
        out.append(float(np.median(chunk)) if chunk.size else None)
    return pa.array(out, type=pa.float64())


def _apply_aggregate(table: pa.Table, aggregate: TableAggregate) -> pa.Table:
    keys = list(aggregate.group_by)
    for metric in aggregate.metrics:
        dtype = table.column(metric.column).type
        if metric.fn not in ("count", "min", "max") and not _is_numeric(dtype):
            if not pa.types.is_boolean(dtype):
                raise TableQueryError(
                    400,
                    "invalid_aggregate",
                    f"{metric.fn} requires a numeric column; {metric.column!r} is {dtype}",
                    column=metric.column,
                    fn=metric.fn,
                )

    work = table
    global_key = None
    if not keys:
        global_key = "__clio_group__"
        while global_key in work.column_names:
            global_key += "_"
        work = work.append_column(global_key, pa.array(np.zeros(work.num_rows, dtype=np.int8)))
        group_keys = [global_key]
    else:
        group_keys = keys

    specs: list[tuple[str, str]] = []
    for metric in aggregate.metrics:
        arrow_fn = "list" if metric.fn == "median" else _ARROW_AGG[metric.fn]
        spec = (metric.column, arrow_fn)
        if spec not in specs:
            specs.append(spec)
    try:
        grouped = work.group_by(group_keys, use_threads=False).aggregate(specs)
    except (pa.ArrowNotImplementedError, pa.ArrowTypeError, pa.ArrowInvalid) as exc:
        raise TableQueryError(
            400, "invalid_aggregate", "aggregate is not supported on these columns", detail=str(exc)
        ) from exc

    arrays: list[Any] = [grouped.column(key) for key in keys]
    names = list(keys)
    for metric in aggregate.metrics:
        if metric.fn == "median":
            arrays.append(_median_of_lists(grouped.column(f"{metric.column}_list")))
        else:
            arrays.append(grouped.column(f"{metric.column}_{_ARROW_AGG[metric.fn]}"))
        names.append(metric.output_name)
    result = pa.table(arrays, names=names)

    if global_key is not None and result.num_rows == 0:
        # SQL semantics: a global aggregate over zero rows is one row.
        empty: dict[str, Any] = {}
        for metric, name in zip(aggregate.metrics, names, strict=True):
            dtype = result.column(name).type
            empty[name] = pa.array([0 if metric.fn == "count" else None], type=dtype)
        result = pa.table(empty)
    if keys and result.num_rows:
        result = result.sort_by([(key, "ascending") for key in keys])
    return result


# --------------------------------------------------------------------------- #
# JSON-safe serialisation
# --------------------------------------------------------------------------- #


def _json_safe(value: Any) -> Any:
    """Convert one Python value from ``to_pylist`` into strict-JSON-safe data."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        as_float = float(value)
        return as_float if math.isfinite(as_float) else None
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _column_values(values: pa.ChunkedArray) -> list[Any]:
    dtype = values.type
    if pa.types.is_timestamp(dtype) and dtype.unit == "ns":
        # Python datetimes carry microseconds; ns would otherwise need pandas.
        values = pc.cast(values, pa.timestamp("us", tz=dtype.tz), safe=False)
    elif pa.types.is_duration(dtype) and dtype.unit == "ns":
        values = pc.cast(values, pa.duration("us"), safe=False)
    return [_json_safe(item) for item in values.to_pylist()]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


@dataclass
class ProcessedTable:
    """Everything a query touches EXCEPT the final per-page offset/limit slice.

    Split out so a caller (the route) can cache this by ``(artifact sha,
    query-without-offset)``: paging through a large result then costs one
    cheap ``.slice()`` per page instead of re-reading, re-filtering,
    re-aggregating, re-downsampling and re-sorting the source file for
    every page (owner ruling: avoid materializing/redoing the whole
    pipeline per page when possible).
    """

    table: pa.Table
    output_columns: list[str]
    total_rows: int
    matched_rows: int
    downsample_info: dict[str, Any]
    #: The row-key column's name in ``table`` (see :data:`ROW_KEY_BASE_NAME`),
    #: or ``None`` when the request aggregated (grouped rows have no single
    #: underlying source row to key by).
    row_key_column: str | None = None


def compute_processed_table(
    source: Path,
    fmt: TableFormat,
    request: TableQueryRequest,
    *,
    limit: int,
    cancellation: "QueryCancellation",
    allow_over_limit_sampling: bool = True,
) -> ProcessedTable:
    """Run every stage up to (and including) sort; never applies ``offset``/``limit``.

    ``limit`` is the already-bounded PER-RESPONSE transfer cap: a one-shot
    request (no ``offset``) that would exceed it and set no ``downsample``
    gets an even-stride sample of the WHOLE range instead (``downsample:
    {mode: "stride", reason: "over_limit"}``), never a biased head slice --
    that sampling happens HERE (before any page is sliced), never twice.
    ``cancellation`` is checked between every stage (and, for a per-entity
    downsample, on every entity): the requesting client disconnecting stops
    the query immediately (:class:`TableQueryCancelled`); a configured
    wall-clock backstop (:class:`TableQueryTimeout`) is the secondary guard
    for a client that is still connected but has waited too long.

    ``allow_over_limit_sampling`` (default ``True``) gates the automatic
    over-limit stride above: table-query wants it (an interactive response
    must stay small), an export never does (:mod:`clio_agent.gact.artifacts.
    table_export` sets it ``False`` so a download is never silently
    truncated -- it raises ``export_too_large`` instead).

    Raises:
        TableQueryError: for unknown columns, incomparable filter/sort
            values, unsupported aggregates/downsample inputs, or unparsable
            files.
    """

    schema = _read_schema(source, fmt)
    available = list(schema.names)
    needed = _source_columns_needed(request, available)
    missing = [name for name in needed if name not in available]
    if missing:
        raise TableQueryError(
            400,
            "columns_not_found",
            "one or more requested columns do not exist",
            missing=missing,
            available=available,
        )
    if request.aggregate is None:
        _validate_downsample_columns(request, available, stage="source")
        _validate_sort_columns(request, available, stage="source")

    table = _read_table(source, fmt, needed)
    total_rows = table.num_rows
    cancellation.check()

    # The stable row key, added BEFORE filter/downsample/sort so its values
    # always identify the row's position in the freshly-read, untouched
    # table -- every later stage (`_apply_filters`'s mask, `.take()` in the
    # downsample helpers, `_apply_sort`) carries a real table column through
    # unchanged, so surviving rows keep their original key. Aggregation
    # collapses many source rows into one group, which has no single row to
    # key by, so it is skipped entirely when `request.aggregate` is set.
    row_key_column: str | None = None
    if request.aggregate is None:
        # Derived from the DATASET'S OWN schema (`available`, read once
        # above), not this query's own projected/needed columns: the
        # synthetic name must be stable for a given dataset regardless of
        # which columns any particular query happens to request, or two
        # queries projecting different columns could compute two different
        # synthetic key names for the exact same concept (#1551 review).
        row_key_column = _row_key_column_name(available)
        table = table.append_column(
            row_key_column, pa.array(np.arange(table.num_rows, dtype=np.int64))
        )

    table = _apply_filters(table, request.filters)
    cancellation.check()

    if request.aggregate is not None:
        table = _apply_aggregate(table, request.aggregate)
        aggregate_output = list(table.column_names)
        if request.columns:
            _validate_requested_output_columns(request, aggregate_output, stage="aggregated")
        output_columns = list(request.columns) if request.columns else aggregate_output
        _validate_downsample_columns(request, aggregate_output, stage="aggregated")
        _validate_sort_columns(request, aggregate_output, stage="aggregated")
        cancellation.check()
    else:
        # An omitted columns list means "every column" — already what ``needed``
        # (and therefore this table's own schema) resolved to above. The
        # synthetic row-key column is never one of "every column": it rides
        # along in `table` for the downsample/sort stages below, but it is
        # reported separately (`rowKey`, see `page_processed_table`), never
        # folded into the caller-visible `columns`/`schema`.
        output_columns = (
            list(request.columns)
            if request.columns
            else [name for name in table.column_names if name != row_key_column]
        )
    matched_rows = table.num_rows

    # Downsample BEFORE sort (the shared DataQuery contract's own documented
    # order): an explicit or automatic sample picks its rows from the
    # filtered/aggregated set first, and the caller's sort then orders
    # exactly the rows that made it into the response.
    paging = request.offset is not None
    downsample_info: dict[str, Any] = {"mode": "none"}
    if request.downsample is not None:
        table, downsample_info = apply_downsample(table, request.downsample, limit, cancellation)
        cancellation.check()
        entity_column = request.downsample.entity_column
        if entity_column is not None and not paging and table.num_rows > limit:
            # An explicit PER-ENTITY downsample (per_entity_lttb, or stride
            # with entityColumn) can still exceed the response limit when
            # there are many entities -- a naive final slice would then
            # silently DROP whole entities (a head slice by row order groups
            # entities together). Reduce evenly ACROSS entities instead, so
            # every entity keeps at least one point, and report it.
            table, reduction_info = reduce_evenly_per_entity_for_limit(table, entity_column, limit)
            downsample_info.update(reduction_info)
    elif not paging and table.num_rows > limit:
        # One-shot request, no explicit downsample, still over the transfer
        # guard. `allow_over_limit_sampling=False` (an export) refuses
        # OUTRIGHT here regardless of `sort` -- an export is never paged or
        # sliced by `limit` afterward (unlike table-query's own paging step),
        # so a sorted-but-unsampled oversized table would otherwise flow
        # through completely unbounded. Only interactive table-query
        # (`allow_over_limit_sampling=True`) gets the SORT exclusion below:
        # a sort+limit query there is a top-/bottom-N request, and sampling
        # before sorting would silently corrupt it -- the full matched set
        # flows through, gets sorted for real, and `page_processed_table`
        # slices it to `limit` afterward.
        if not allow_over_limit_sampling:
            raise TableQueryError(
                413,
                "export_too_large",
                "export exceeds the configured row ceiling; narrow it with filters",
                rows=table.num_rows,
                max_rows=limit,
            )
        if not request.sort:
            table, downsample_info = apply_over_limit_stride(table, limit)

    table = _apply_sort(table, request.sort)
    cancellation.check()

    return ProcessedTable(
        table=table,
        output_columns=output_columns,
        total_rows=total_rows,
        matched_rows=matched_rows,
        downsample_info=downsample_info,
        row_key_column=row_key_column,
    )


def page_processed_table(processed: ProcessedTable, *, offset: int, limit: int) -> dict[str, Any]:
    """Slice+serialize ONE page of an already-:func:`compute_processed_table` result.

    Cheap and side-effect-free (no file I/O, no re-filtering/-aggregating/
    -downsampling/-sorting) — the route calls this once per request, reusing
    a cached :class:`ProcessedTable` across every page of the SAME query.

    Response shape (``dict[str, Any]``, no separate Pydantic model -- matches
    every other table-query/preview route): adds an OPTIONAL ``rowKey`` field
    (:class:`~clio_agent.gact.artifacts.table_query_models.RowKey`), sized and
    ordered exactly like ``columns`` (one value per returned row) -- the
    client's stable key for linking a selection across views of the same
    artifact, independent of which columns each view happened to request.
    Present whenever the query did not aggregate (see
    :attr:`ProcessedTable.row_key_column`); absent otherwise.
    """

    table = processed.table
    truncated = (table.num_rows - offset) > limit
    sliced = table.slice(offset, limit)
    page = sliced.select(processed.output_columns)
    result: dict[str, Any] = {
        "schema": [{"name": field.name, "type": str(field.type)} for field in page.schema],
        "columns": {name: _column_values(page.column(name)) for name in processed.output_columns},
        "totalRows": processed.total_rows,
        "matchedRows": processed.matched_rows,
        "returnedRows": page.num_rows,
        "truncated": truncated,
        "offset": offset,
        "downsample": processed.downsample_info,
    }
    if processed.row_key_column is not None:
        result["rowKey"] = RowKey(
            column=processed.row_key_column,
            values=_column_values(sliced.column(processed.row_key_column)),
        ).model_dump(mode="json")
    return result


def run_table_query(
    source: Path,
    fmt: TableFormat,
    request: TableQueryRequest,
    *,
    limit: int,
    cancellation: "QueryCancellation",
) -> dict[str, Any]:
    """Convenience one-shot API: :func:`compute_processed_table` + one page.

    The route calls the two halves directly instead, so it can cache the
    :class:`ProcessedTable` across a query's pages; kept here for direct/test
    callers that just want one full result.
    """

    processed = compute_processed_table(
        source, fmt, request, limit=limit, cancellation=cancellation
    )
    return page_processed_table(processed, offset=request.offset or 0, limit=limit)


__all__ = [
    "DEFAULT_LIMIT",
    "ProcessedTable",
    "QueryCancellation",
    "TableAggregate",
    "TableDownsample",
    "TableFilter",
    "TableFormat",
    "TableMetric",
    "TableQueryError",
    "TableQueryRequest",
    "TableQueryTimeout",
    "TableSort",
    "compute_processed_table",
    "page_processed_table",
    "run_table_query",
    "table_format_for",
]
