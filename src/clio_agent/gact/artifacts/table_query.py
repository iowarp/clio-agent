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
see :func:`_apply_over_limit_stride`. Sorting runs AFTER downsampling (not
before): an explicit or automatic sample picks its rows from the
filtered/aggregated set first, and the caller's ``sort`` then orders exactly
the rows that made it into the response.

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
import time
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from clio_agent.gact.artifacts.lttb import lttb_indices
from clio_agent.gact.artifacts.table_query_models import (
    DEFAULT_LIMIT,
    Scalar,
    TableAggregate,
    TableDownsample,
    TableFilter,
    TableFormat,
    TableMetric,
    TableQueryError,
    TableQueryRequest,
    TableQueryTimeout,
    TableSort,
    table_format_for,
)


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

    if request.columns:
        for column in request.columns:
            add(column)
    elif request.aggregate is None:
        for column in available:
            add(column)
    for flt in request.filters:
        add(flt.column)
    if request.aggregate is not None:
        for key in request.aggregate.group_by:
            add(key)
        for metric in request.aggregate.metrics:
            add(metric.column)
    else:
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


def _x_as_epoch(column: str, values: pa.ChunkedArray) -> np.ndarray:
    """Convert an x column to float64 (temporal -> epoch seconds)."""

    dtype = values.type
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        converted = None
        for target in (pa.timestamp("us"), pa.timestamp("us", tz="UTC")):
            try:
                converted = pc.cast(values, target)
                break
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                continue
        if converted is None:
            raise TableQueryError(
                400,
                "invalid_downsample",
                f"x column {column!r} is neither numeric nor ISO-8601 datetimes",
                column=column,
            )
        values = converted
        dtype = values.type
    if pa.types.is_timestamp(dtype):
        per_second = {"s": 1, "ms": 1e3, "us": 1e6, "ns": 1e9}[dtype.unit]
        raw = pc.cast(values, pa.int64()).to_numpy(zero_copy_only=False)
        return _nullable_float(values, raw) / per_second
    if pa.types.is_date32(dtype):
        raw = pc.cast(values, pa.int32()).to_numpy(zero_copy_only=False)
        return _nullable_float(values, raw) * 86_400.0
    if pa.types.is_date64(dtype):
        raw = pc.cast(values, pa.int64()).to_numpy(zero_copy_only=False)
        return _nullable_float(values, raw) / 1e3
    return _numeric_float(column, values, role="x")


def _nullable_float(values: pa.ChunkedArray, raw: np.ndarray) -> np.ndarray:
    out = np.asarray(raw, dtype=np.float64)
    nulls = values.is_null().to_numpy(zero_copy_only=False)
    out[nulls] = np.nan
    return out


def _numeric_float(column: str, values: pa.ChunkedArray, *, role: str) -> np.ndarray:
    dtype = values.type
    if not (_is_numeric(dtype) or pa.types.is_boolean(dtype)):
        raise TableQueryError(
            400,
            "invalid_downsample",
            f"{role} column {column!r} must be numeric; it is {dtype}",
            column=column,
        )
    as_float = pc.cast(values, pa.float64())
    return as_float.to_numpy(zero_copy_only=False).astype(np.float64, copy=True)


def _even_indices(count: int, keep: int) -> np.ndarray:
    """Evenly spaced indices over ``range(count)`` including both ends."""

    if count <= keep:
        return np.arange(count, dtype=np.int64)
    if keep == 1:
        return np.zeros(1, dtype=np.int64)
    return np.unique(np.round(np.linspace(0, count - 1, keep)).astype(np.int64))


def _apply_over_limit_stride(table: pa.Table, limit: int) -> tuple[pa.Table, dict[str, Any]]:
    """An UNREQUESTED, protective even-stride sample across the whole range.

    Applied only when a one-shot (non-paging: no ``offset``) request has no
    explicit ``downsample`` and still exceeds ``limit`` after filter/aggregate/
    sort. ``limit`` is a per-response TRANSFER guard, never a statement about
    what the caller wanted — a naive head slice would silently bias a chart
    or map toward the first rows only; an even stride keeps the whole range
    represented instead. A caller that wants every row un-sampled pages
    through it with ``offset`` (see :func:`run_table_query`).
    """

    picked = _even_indices(table.num_rows, limit)
    result = table.take(pa.array(picked, type=pa.int64()))
    info: dict[str, Any] = {
        "mode": "stride",
        "reason": "over_limit",
        "inputRows": table.num_rows,
        "outputRows": result.num_rows,
    }
    return result, info


def _entity_bounds(table: pa.Table, entity: str | None) -> list[tuple[int, int]]:
    """Contiguous ``[start, end)`` row ranges per entity (table must be entity-sorted)."""

    if table.num_rows == 0:
        return []
    if entity is None:
        return [(0, table.num_rows)]
    encoded = pc.dictionary_encode(table.column(entity)).combine_chunks()
    codes = pc.fill_null(encoded.indices, -1).to_numpy(zero_copy_only=False)
    change = np.flatnonzero(np.diff(codes)) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [table.num_rows]))
    return [(int(s), int(e)) for s, e in zip(starts, ends, strict=True)]


def _apply_downsample(
    table: pa.Table,
    downsample: TableDownsample,
    limit: int,
    deadline: float,
    timeout_s: float,
) -> tuple[pa.Table, dict[str, Any]]:
    info: dict[str, Any] = {"mode": downsample.mode, "inputRows": table.num_rows}
    entity = downsample.entity_column
    if downsample.mode == "none":
        info["outputRows"] = table.num_rows
        return table, info

    if downsample.mode == "stride" and entity is None:
        picked = _even_indices(table.num_rows, limit)
        result = table.take(pa.array(picked))
        info.update({"target": limit, "outputRows": result.num_rows})
        return result, info

    # Sort by entity then (for LTTB) by the numeric x the algorithm sees, so ISO
    # strings with mixed offsets still order chronologically. Arrow sorts are
    # stable, so stride keeps the source order within an entity.
    sort_keys: list[tuple[str, str]] = []
    if entity is not None:
        sort_keys.append((entity, "ascending"))
    epoch_key = None
    if downsample.mode == "per_entity_lttb":
        assert downsample.x is not None and downsample.y is not None
        epoch_key = "__clio_x_epoch__"
        while epoch_key in table.column_names:
            epoch_key += "_"
        epoch = _x_as_epoch(downsample.x, table.column(downsample.x))
        table = table.append_column(epoch_key, pa.array(epoch, type=pa.float64()))
        sort_keys.append((epoch_key, "ascending"))
    if sort_keys:
        table = table.sort_by(sort_keys, null_placement="at_end")

    x_values = y_values = usable = None
    if epoch_key is not None:
        assert downsample.y is not None
        x_values = table.column(epoch_key).to_numpy().astype(np.float64, copy=True)
        table = table.drop_columns([epoch_key])
        y_values = _numeric_float(downsample.y, table.column(downsample.y), role="y")
        usable = np.isfinite(x_values) & np.isfinite(y_values)

    keep: list[np.ndarray] = []
    bounds = _entity_bounds(table, entity)
    dropped = 0
    for start, end in bounds:
        _check_deadline(deadline, timeout_s)
        if downsample.mode == "stride":
            keep.append(start + _even_indices(end - start, downsample.max_per_entity))
            continue
        assert x_values is not None and y_values is not None and usable is not None
        rows = start + np.flatnonzero(usable[start:end])
        dropped += (end - start) - rows.size
        if rows.size == 0:
            continue
        chosen = lttb_indices(x_values[rows], y_values[rows], downsample.max_per_entity)
        keep.append(rows[chosen])
    picked = np.concatenate(keep) if keep else np.zeros(0, dtype=np.int64)
    result = table.take(pa.array(picked, type=pa.int64()))
    info.update(
        {
            "entityColumn": entity,
            "entities": len(bounds),
            "maxPerEntity": downsample.max_per_entity,
            "outputRows": result.num_rows,
        }
    )
    if downsample.mode == "per_entity_lttb":
        info.update({"x": downsample.x, "y": downsample.y, "droppedNullRows": int(dropped)})
    return result, info


def _check_deadline(deadline: float, timeout_s: float) -> None:
    if time.monotonic() > deadline:
        raise TableQueryTimeout(timeout_s)


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


def run_table_query(
    source: Path,
    fmt: TableFormat,
    request: TableQueryRequest,
    *,
    limit: int,
    deadline: float,
    timeout_s: float,
) -> dict[str, Any]:
    """Execute ``request`` against the tabular file at ``source``.

    ``limit`` is the already-bounded PER-RESPONSE transfer cap, never a
    ceiling on what a caller can reach — pass ``request.offset`` to page
    through the full (filtered/sorted) result a plain, un-sampled page at a
    time (capped to ``limit`` if the page itself overflows, reported via
    ``truncated``); a one-shot request (no ``offset``) that still exceeds
    ``limit`` and set no ``downsample`` gets an even-stride sample of the
    WHOLE range instead (``downsample: {mode: "stride", reason:
    "over_limit"}``), never a biased head slice. ``deadline`` is a
    ``time.monotonic()`` instant after which the query stops at the next
    stage boundary with :class:`TableQueryTimeout` — a configured
    SERVER-PROTECTION backstop (pyarrow's synchronous reads are not
    preemptible mid-call), never a statement about how much data is
    reachable: raise ``artifacts.table_query_timeout_s`` for a genuinely
    large one-shot query, or page it with ``offset`` instead.

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
    _check_deadline(deadline, timeout_s)

    table = _apply_filters(table, request.filters)
    _check_deadline(deadline, timeout_s)

    if request.aggregate is not None:
        table = _apply_aggregate(table, request.aggregate)
        output_columns = list(table.column_names)
        _validate_downsample_columns(request, output_columns, stage="aggregated")
        _validate_sort_columns(request, output_columns, stage="aggregated")
        _check_deadline(deadline, timeout_s)
    else:
        # An omitted columns list means "every column" — already what ``needed``
        # (and therefore this table's own schema) resolved to above.
        output_columns = list(request.columns) if request.columns else list(table.column_names)
    matched_rows = table.num_rows

    # Downsample BEFORE sort (the shared DataQuery contract's own documented
    # order): an explicit or automatic sample picks its rows from the
    # filtered/aggregated set first, and the caller's sort then orders
    # exactly the rows that made it into the response.
    paging = request.offset is not None
    downsample_info: dict[str, Any] = {"mode": "none"}
    if request.downsample is not None:
        table, downsample_info = _apply_downsample(
            table, request.downsample, limit, deadline, timeout_s
        )
        _check_deadline(deadline, timeout_s)
    elif not paging and table.num_rows > limit:
        # One-shot request, no explicit downsample, still over the transfer
        # guard: sample the WHOLE range instead of silently biasing toward
        # the first rows (owner ruling — see _apply_over_limit_stride).
        table, downsample_info = _apply_over_limit_stride(table, limit)

    table = _apply_sort(table, request.sort)
    _check_deadline(deadline, timeout_s)

    offset = request.offset or 0
    truncated = (table.num_rows - offset) > limit
    table = table.slice(offset, limit)
    table = table.select(output_columns)

    return {
        "schema": [{"name": field.name, "type": str(field.type)} for field in table.schema],
        "columns": {name: _column_values(table.column(name)) for name in output_columns},
        "totalRows": total_rows,
        "matchedRows": matched_rows,
        "returnedRows": table.num_rows,
        "truncated": truncated,
        "offset": offset,
        "downsample": downsample_info,
    }


__all__ = [
    "DEFAULT_LIMIT",
    "TableAggregate",
    "TableDownsample",
    "TableFilter",
    "TableFormat",
    "TableMetric",
    "TableQueryError",
    "TableQueryRequest",
    "TableQueryTimeout",
    "TableSort",
    "run_table_query",
    "table_format_for",
]
