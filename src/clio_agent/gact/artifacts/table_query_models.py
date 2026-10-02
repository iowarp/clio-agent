"""Request models, limits and typed errors for the artifact table query.

Kept free of numpy/pyarrow so the route can register (and FastAPI can build
its request schema) without importing the query engine; the engine in
:mod:`clio_agent.gact.artifacts.table_query` is imported only when a query runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: No upper bound on ``maxPerEntity`` (owner ruling: no cap that kneecaps
#: intent) — an explicit per-entity downsample that still exceeds the
#: per-response transfer guard is reduced evenly ACROSS entities instead
#: (``_reduce_evenly_per_entity_for_limit``, keeps every entity), so the real
#: guard is the per-response row cap (``table_query_max_rows`` / ``limit``,
#: a pure transfer/memory bound), never this shape count.
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


class TableQueryStalled(TableQueryError):
    """The query's worker thread stopped working while its client still waited.

    A query is waited for while the one thread running it keeps consuming CPU
    (:mod:`clio_agent.runtime.progress`), so a large query on a slow
    machine is never cut off by a fixed clock. ``reason`` is ``no_progress`` (a
    whole ``artifacts.table_query_no_progress_s`` window without an answer and
    without CPU work: the query is blocked) or ``ceiling`` (still working at
    ``artifacts.table_query_max_wait_s``). The client disconnecting is the other
    way a query stops (:class:`TableQueryCancelled`).
    """

    def __init__(self, reason: str, waited_s: float, no_progress_s: float) -> None:
        super().__init__(
            504,
            "table_query_stalled",
            "table query stopped making progress"
            if reason == "no_progress"
            else "table query was still working at its ceiling",
            reason=reason,
            waited_s=round(waited_s, 1),
            no_progress_s=no_progress_s,
        )


class TableQueryCancelled(TableQueryError):
    """The query's own HTTP client disconnected before it finished.

    The PRIMARY cancellation path (owner ruling: a long query should be
    cancellable by the client, not killed by a fixed deadline): the route
    watches ``request.is_disconnected()`` and the engine checks a shared
    cancel signal between (and, in the per-entity downsample loop, WITHIN)
    stages, so an abandoned query stops promptly instead of running to
    completion for no one.
    """

    def __init__(self) -> None:
        super().__init__(
            499,
            "table_query_client_disconnected",
            "table query cancelled: the requesting client disconnected",
        )


# --------------------------------------------------------------------------- #
# Request model
# --------------------------------------------------------------------------- #


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TableFilter(_Strict):
    """One predicate; all predicates in a request are AND-ed together — including
    several predicates on the SAME column (e.g. an agent's own base ``dataQuery``
    filter plus a UI-added one): each is evaluated independently against the
    source values and combined, never merged/overwritten by column name.

    * ``eq`` — ``value`` is a non-null scalar.
    * ``in`` — ``value`` is a non-empty list of non-null scalars.
    * ``range`` — ``value`` is ``[min, max]``, inclusive; either side may be null.
    * ``contains`` — ``value`` is a non-empty string; case-insensitive
      substring match on a string column.
    * ``isnull`` — ``value`` is omitted/``true`` (match nulls, NaN included) or
      ``false`` (match non-nulls).
    """

    column: str = Field(min_length=1)
    op: Literal["eq", "in", "range", "contains", "isnull"]
    value: Any = None

    @model_validator(mode="after")
    def _check_value(self) -> TableFilter:
        value = self.value
        if self.op == "eq":
            if not _is_scalar(value):
                raise ValueError("eq filter requires a non-null scalar value")
        elif self.op == "in":
            if not isinstance(value, list) or len(value) < 1:
                raise ValueError("in filter requires a non-empty list of values")
            if not all(_is_scalar(item) for item in value):
                raise ValueError("in filter values must be non-null scalars")
        elif self.op == "range":
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("range filter requires [min, max]")
            if not all(item is None or _is_scalar(item) for item in value):
                raise ValueError("range bounds must be scalars or null")
        elif self.op == "contains":
            if not isinstance(value, str) or not value:
                raise ValueError("contains filter requires a non-empty string value")
        elif value is not None and not isinstance(value, bool):
            raise ValueError("isnull filter value must be a boolean when given")
        return self


class TableSort(_Strict):
    """One sort key; multiple keys apply in order (a stable, compound sort)."""

    column: str = Field(min_length=1)
    desc: bool = False


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

    group_by: list[str] = Field(default_factory=list, alias="groupBy")
    # min_length=1 is a CORRECTNESS requirement (an aggregate with no metric
    # reduces nothing), not an intent cap; no upper bound.
    metrics: list[TableMetric] = Field(min_length=1)

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
    max_per_entity: int = Field(default=DEFAULT_MAX_PER_ENTITY, alias="maxPerEntity", ge=1)

    @model_validator(mode="after")
    def _check_mode(self) -> TableDownsample:
        if self.mode == "per_entity_lttb" and (self.x is None or self.y is None):
            raise ValueError("per_entity_lttb requires both x and y")
        return self


class TableQueryRequest(_Strict):
    """``POST /v1/artifacts/{artifact_id}/table-query`` request body.

    ``columns`` may be omitted when ``aggregate`` is not set: the server then
    returns every column in the source table (rows still bounded by
    ``limit``) instead of refusing the request. ``aggregate`` still requires
    ``columns`` explicitly (its own ``groupBy``/metric columns are read
    regardless, but the request must still name what it wants alongside
    them, matching every other structured request shape here).

    ``limit`` is a per-response TRANSFER guard, never a limit on the data a
    caller can reach: ``offset`` pages through the full (filtered/sorted)
    result — a caller that sets it gets a plain, un-sampled page (capped to
    ``limit`` if the page itself is oversized, reported via ``truncated``).
    Omitting ``offset`` keeps the one-shot, whole-range contract: if the
    result still exceeds ``limit`` and ``downsample`` was not set, the engine
    applies its own even-stride sample across the WHOLE range (never a head
    slice) rather than silently biasing toward the first rows.
    """

    columns: list[str] = Field(default_factory=list)
    filters: list[TableFilter] = Field(default_factory=list, alias="filter")
    aggregate: TableAggregate | None = None
    downsample: TableDownsample | None = None
    sort: list[TableSort] = Field(default_factory=list)
    offset: Optional[int] = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1)
    format: Literal["json"] = "json"

    @model_validator(mode="after")
    def _check_columns(self) -> TableQueryRequest:
        if any(not column for column in self.columns):
            raise ValueError("column names must be non-empty")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must be distinct")
        if self.aggregate is not None and not self.columns:
            raise ValueError("columns is required when aggregate is set")
        return self

    def canonical_json(self, effective_limit: int) -> str:
        """Stable JSON identity of this query (cache key component)."""

        payload = self.model_dump(mode="json", by_alias=True)
        payload["limit"] = effective_limit
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def canonical_json_for_processing(self, effective_limit: int) -> str:
        """Stable identity of the OFFSET-LESS processed result this query
        produces (``compute_processed_table``'s cache key): every page of
        the same query shares one entry, since ``offset`` only selects which
        page of an already-processed result to serialize. ``paging``
        (whether ``offset`` was given AT ALL, not its value) is kept in the
        key because it changes PROCESSING itself -- a one-shot (no offset)
        over-limit query is sampled; a paging one never is.
        """

        payload = self.model_dump(mode="json", by_alias=True)
        payload["limit"] = effective_limit
        payload["offset"] = None
        payload["paging"] = self.offset is not None
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and value is not None


# --------------------------------------------------------------------------- #
# Planning helpers
# --------------------------------------------------------------------------- #


def table_format_for(name: str) -> TableFormat | None:
    """The tabular format implied by an artifact name, or ``None`` if not tabular."""

    return _SUFFIX_FORMATS.get(Path(name).suffix.lower())
