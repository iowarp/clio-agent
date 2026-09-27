"""Request models, limits and typed errors for the artifact table query.

Kept free of numpy/pyarrow so the route can register (and FastAPI can build
its request schema) without importing the query engine; the engine in
:mod:`clio_agent.gact.artifacts.table_query` is imported only when a query runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_COLUMNS = 64
MAX_IN_VALUES = 10_000
MAX_PER_ENTITY = 2_000
DEFAULT_LIMIT = 5_000
DEFAULT_MAX_PER_ENTITY = 500

TableFormat = Literal["csv", "parquet"]
_SUFFIX_FORMATS: dict[str, TableFormat] = {
    ".csv": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
}

Scalar = str | int | float | bool


class TableQueryError(Exception):
    """A typed, client-facing refusal raised while planning or running a query."""

    def __init__(self, status_code: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class TableQueryTimeout(TableQueryError):
    """The query overran its wall-clock budget."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            504,
            "table_query_timeout",
            "table query exceeded its wall-clock budget",
            timeout_s=timeout_s,
        )


# --------------------------------------------------------------------------- #
# Request model
# --------------------------------------------------------------------------- #


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TableFilter(_Strict):
    """One predicate; all predicates in a request are AND-ed together.

    * ``eq`` — ``value`` is a non-null scalar.
    * ``in`` — ``value`` is a non-empty list of non-null scalars.
    * ``range`` — ``value`` is ``[min, max]``, inclusive; either side may be null.
    * ``isnull`` — ``value`` is omitted/``true`` (match nulls, NaN included) or
      ``false`` (match non-nulls).
    """

    column: str = Field(min_length=1)
    op: Literal["eq", "in", "range", "isnull"]
    value: Any = None

    @model_validator(mode="after")
    def _check_value(self) -> TableFilter:
        value = self.value
        if self.op == "eq":
            if not _is_scalar(value):
                raise ValueError("eq filter requires a non-null scalar value")
        elif self.op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
                raise ValueError(f"in filter requires a list of 1..{MAX_IN_VALUES} values")
            if not all(_is_scalar(item) for item in value):
                raise ValueError("in filter values must be non-null scalars")
        elif self.op == "range":
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("range filter requires [min, max]")
            if not all(item is None or _is_scalar(item) for item in value):
                raise ValueError("range bounds must be scalars or null")
        elif value is not None and not isinstance(value, bool):
            raise ValueError("isnull filter value must be a boolean when given")
        return self


class TableMetric(_Strict):
    """One aggregate output column, named ``{column}_{fn}``."""

    column: str = Field(min_length=1)
    fn: Literal["mean", "min", "max", "count", "sum", "median"]

    @property
    def output_name(self) -> str:
        """The result column name this metric produces."""

        return f"{self.column}_{self.fn}"


class TableAggregate(_Strict):
    """Group rows by ``groupBy`` (may be empty: one global group) and reduce."""

    group_by: list[str] = Field(default_factory=list, alias="groupBy", max_length=MAX_COLUMNS)
    metrics: list[TableMetric] = Field(min_length=1, max_length=MAX_COLUMNS)

    @model_validator(mode="after")
    def _check_names(self) -> TableAggregate:
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("groupBy columns must be distinct")
        names = [metric.output_name for metric in self.metrics]
        if len(set(names)) != len(names):
            raise ValueError("aggregate metrics must be distinct")
        clash = sorted(set(names) & set(self.group_by))
        if clash:
            raise ValueError(f"metric output names collide with groupBy columns: {clash}")
        return self


class TableDownsample(_Strict):
    """How to thin the (filtered / aggregated) rows before the limit applies.

    * ``none`` — keep every row.
    * ``stride`` — keep evenly spaced rows (both ends included): per entity up
      to ``maxPerEntity`` when ``entityColumn`` is set, else overall up to the
      request ``limit``.
    * ``per_entity_lttb`` — Largest-Triangle-Three-Buckets on ``(x, y)`` per
      entity (the whole table is one series without ``entityColumn``), keeping
      at most ``maxPerEntity`` points and always both endpoints. ``x`` may be
      numeric, temporal, or an ISO-8601 string; ``y`` must be numeric.
    """

    mode: Literal["none", "stride", "per_entity_lttb"] = "none"
    entity_column: str | None = Field(default=None, alias="entityColumn", min_length=1)
    x: str | None = Field(default=None, min_length=1)
    y: str | None = Field(default=None, min_length=1)
    max_per_entity: int = Field(
        default=DEFAULT_MAX_PER_ENTITY, alias="maxPerEntity", ge=1, le=MAX_PER_ENTITY
    )

    @model_validator(mode="after")
    def _check_mode(self) -> TableDownsample:
        if self.mode == "per_entity_lttb" and (self.x is None or self.y is None):
            raise ValueError("per_entity_lttb requires both x and y")
        return self


class TableQueryRequest(_Strict):
    """``POST /v1/artifacts/{artifact_id}/table-query`` request body."""

    columns: list[str] = Field(min_length=1, max_length=MAX_COLUMNS)
    filters: list[TableFilter] = Field(default_factory=list, alias="filter", max_length=64)
    aggregate: TableAggregate | None = None
    downsample: TableDownsample | None = None
    limit: int | None = Field(default=None, ge=1)
    format: Literal["json"] = "json"

    @model_validator(mode="after")
    def _check_columns(self) -> TableQueryRequest:
        if any(not column for column in self.columns):
            raise ValueError("column names must be non-empty")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must be distinct")
        return self

    def canonical_json(self, effective_limit: int) -> str:
        """Stable JSON identity of this query (cache key component)."""

        payload = self.model_dump(mode="json", by_alias=True)
        payload["limit"] = effective_limit
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and value is not None


# --------------------------------------------------------------------------- #
# Planning helpers
# --------------------------------------------------------------------------- #


def table_format_for(name: str) -> TableFormat | None:
    """The tabular format implied by an artifact name, or ``None`` if not tabular."""

    return _SUFFIX_FORMATS.get(Path(name).suffix.lower())
