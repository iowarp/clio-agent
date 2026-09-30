"""Downsampling stage of the table-query engine (split out of table_query.py).

Split for the file-size ratchet (no-accretion ground rule) -- downsampling
(even stride, per-entity LTTB, and the two "still over the response limit"
corrections) is a self-contained concern with its own numpy/pyarrow helpers,
used by :mod:`clio_agent.gact.artifacts.table_query` but implemented here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from clio_agent.gact.artifacts.lttb import lttb_indices
from clio_agent.gact.artifacts.table_query_models import TableDownsample, TableQueryError

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.table_query import QueryCancellation


def _is_numeric(dtype: pa.DataType) -> bool:
    return pa.types.is_integer(dtype) or pa.types.is_floating(dtype) or pa.types.is_decimal(dtype)


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


def apply_over_limit_stride(table: pa.Table, limit: int) -> tuple[pa.Table, dict[str, Any]]:
    """An UNREQUESTED, protective even-stride sample across the whole range.

    Applied only when a one-shot (non-paging: no ``offset``) request has no
    explicit ``downsample`` and still exceeds ``limit`` after filter/aggregate/
    sort. ``limit`` is a per-response TRANSFER guard, never a statement about
    what the caller wanted — a naive head slice would silently bias a chart
    or map toward the first rows only; an even stride keeps the whole range
    represented instead. A caller that wants every row un-sampled pages
    through it with ``offset``.
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


def entity_bounds(table: pa.Table, entity: str | None) -> list[tuple[int, int]]:
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


def reduce_evenly_per_entity_for_limit(
    table: pa.Table, entity_column: str, limit: int
) -> tuple[pa.Table, dict[str, Any]]:
    """A SECOND, even reduction across entities for an explicit per-entity
    downsample (``per_entity_lttb``, or ``stride`` with ``entityColumn``)
    that still exceeds the per-response ``limit``.

    ``table`` must already be entity-grouped (:func:`apply_downsample`'s own
    output always is). A naive final head slice would drop whole entities
    outright (rows are grouped by entity, so cutting at ``limit`` keeps only
    the first few entities); this keeps every entity, at a smaller even
    per-entity cap instead, and reports the reduction rather than silently
    truncating.
    """

    bounds = entity_bounds(table, entity_column)
    num_entities = len(bounds)
    if num_entities == 0:
        return table, {}
    per_entity_cap = max(1, limit // num_entities)
    keep: list[np.ndarray] = [
        start + _even_indices(end - start, per_entity_cap) for start, end in bounds
    ]
    picked = np.concatenate(keep) if keep else np.zeros(0, dtype=np.int64)
    result = table.take(pa.array(picked, type=pa.int64()))
    return result, {
        "reducedForResponseLimit": True,
        "entities": num_entities,
        "perEntityCapAfterReduction": per_entity_cap,
        "outputRows": result.num_rows,
    }


def apply_downsample(
    table: pa.Table,
    downsample: TableDownsample,
    limit: int,
    cancellation: "QueryCancellation",
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
    bounds = entity_bounds(table, entity)
    dropped = 0
    for start, end in bounds:
        cancellation.check()
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


__all__ = [
    "apply_downsample",
    "apply_over_limit_stride",
    "entity_bounds",
    "reduce_evenly_per_entity_for_limit",
]
