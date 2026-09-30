"""Bounded server-side query / downsample over registered tabular artifacts.

``POST /v1/artifacts/{artifact_id}/table-query`` lets a chart pull a projected,
filtered, aggregated and downsampled slice of a CSV or Parquet artifact instead
of the whole file (e.g. 360 entities x 100 time points). The route is the
sibling of ``GET .../table-preview`` and shares its custody rules: only
artifacts in the registry are addressable (no filesystem paths are accepted),
bytes come from the provider-owned store / CAS blob / contained workspace path,
and every read is re-hashed against the immutable version hash.

The query engine lives in :mod:`clio_agent.gact.artifacts.table_query`; this
module owns limits, the worker-thread wall-clock budget, a small result cache
keyed on ``(content sha256, canonical query)``, and the error envelope.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.artifacts.cas import sha256_file
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.table_query_models import (
    DEFAULT_LIMIT,
    TableQueryError,
    TableQueryRequest,
    TableQueryTimeout,
    table_format_for,
)
from clio_agent.gact.routes.artifact_table_preview import (
    _artifact_source,
    _error,
    _workspace_root,
)

# A cached response larger than this many cells is not retained, so the bounded
# entry count also bounds memory.
_CACHE_MAX_CELLS = 250_000


def table_query_max_rows() -> int:
    """Ceiling on the ``limit`` one table-query request may ask for.

    Config: ``artifacts.table_query_max_rows`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_MAX_ROWS`` (default 50000). A request whose
    ``limit`` exceeds it is refused with a typed 400.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_query_max_rows",
        env="CLIO_ARTIFACTS_TABLE_QUERY_MAX_ROWS",
        default=50_000,
        cast=conf.as_int,
    )


def table_query_max_source_bytes() -> int:
    """Largest tabular artifact, in bytes, the table-query route will read.

    Config: ``artifacts.table_query_max_source_bytes`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_MAX_SOURCE_BYTES`` (default 268435456 = 256
    MiB, the same cap as table-preview). The projected columns are loaded into
    memory, so this bounds both latency and peak memory.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_query_max_source_bytes",
        env="CLIO_ARTIFACTS_TABLE_QUERY_MAX_SOURCE_BYTES",
        default=256 * 1024 * 1024,
        cast=conf.as_int,
    )


def table_query_timeout_s() -> float:
    """Wall-clock budget, in seconds, for one table-query request.

    Config: ``artifacts.table_query_timeout_s`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_TIMEOUT_S`` (default 10). An overrun answers a
    typed 504 ``table_query_timeout``.

    This is a SERVER-PROTECTION backstop, never a statement about how much
    data a caller can reach (owner ruling: no cap that kneecaps intent) — it
    is configurable (raise it for a genuinely large one-shot query) and the
    reason is always typed, never a silent drop. True client-abort
    cancellation of an in-flight query is not implemented here: pyarrow's
    synchronous C++ reads/computes are not preemptible mid-call without
    re-implementing the engine as chunked/incremental reads, which is out of
    this edge module's scope; the practical mitigation is ``offset`` paging
    (a bounded, cheap page per request) instead of one unbounded one-shot
    query.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_query_timeout_s",
        env="CLIO_ARTIFACTS_TABLE_QUERY_TIMEOUT_S",
        default=10.0,
        cast=conf.as_float,
    )


def table_query_cache_entries() -> int:
    """How many table-query results the per-app LRU cache keeps (0 disables it).

    Config: ``artifacts.table_query_cache_entries`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_CACHE_ENTRIES`` (default 16).
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_query_cache_entries",
        env="CLIO_ARTIFACTS_TABLE_QUERY_CACHE_ENTRIES",
        default=16,
        cast=conf.as_int,
    )


class TableQueryCache:
    """A small thread-safe LRU of query results keyed on content hash + query."""

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str, str]) -> dict[str, Any] | None:
        """Return a cached result (refreshing its recency), or ``None``."""

        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
            return found

    def put(self, key: tuple[str, str, str], value: dict[str, Any], capacity: int) -> None:
        """Insert ``value`` and evict least-recently-used entries beyond ``capacity``."""

        if capacity <= 0:
            return
        cells = int(value.get("returnedRows", 0)) * max(1, len(value.get("columns", {})))
        if cells > _CACHE_MAX_CELLS:
            return
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > capacity:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _cache_for(app: FastAPI) -> TableQueryCache:
    cache = getattr(app.state, "table_query_cache", None)
    if cache is None:
        cache = TableQueryCache()
        app.state.table_query_cache = cache
    return cache


def _owned_error(
    *, status_code: int, error: str, message: str, details: dict[str, Any] | None = None
) -> HTTPException:
    """Adapt the storage-owner error seam onto this route's envelope."""

    return _error(status_code, error, message, **(details or {}))


def _table_source(app: FastAPI, record: ArtifactRecord, version: ArtifactVersion) -> Path:
    """Resolve verified bytes exactly as ``/bytes`` does: owned store first, then CAS/path."""

    from clio_agent.gact.artifacts.storage import (  # noqa: PLC0415
        resolve_owned_artifact_or_raise,
    )

    root = _workspace_root(app, record.workspace_id)
    owned = resolve_owned_artifact_or_raise(app, version, workspace_root=root, error=_owned_error)
    if owned is None:
        return _artifact_source(app, record, version)
    if version.sha256:
        actual = sha256_file(owned)
        if actual != version.sha256:
            raise _error(
                409,
                "integrity_violation",
                "artifact store bytes do not match the immutable version hash",
                artifact_id=version.artifact_id,
                recorded_sha256=version.sha256,
                actual_sha256=actual,
            )
    return owned


def _effective_limit(requested: int | None) -> int:
    ceiling = table_query_max_rows()
    if requested is None:
        return max(1, min(DEFAULT_LIMIT, ceiling))
    if requested > ceiling:
        raise _error(
            400,
            "limit_exceeds_ceiling",
            "requested limit exceeds the table-query row ceiling",
            limit=requested,
            max_rows=ceiling,
        )
    return requested


def _table_query(
    app: FastAPI,
    record: ArtifactRecord,
    version: ArtifactVersion,
    request: TableQueryRequest,
    limit: int,
    deadline: float,
    timeout_s: float,
) -> dict[str, Any]:
    """Resolve, bound-check and run one query (worker thread)."""

    fmt = table_format_for(record.name)
    if fmt is None:
        raise _error(
            415,
            "unsupported_media_type",
            "table query supports registered CSV and Parquet artifacts",
            artifact_id=version.artifact_id,
            name=record.name,
        )
    source = _table_source(app, record, version)
    source_size = source.stat().st_size
    max_source_bytes = table_query_max_source_bytes()
    if source_size > max_source_bytes:
        raise _error(
            413,
            "artifact_too_large",
            "tabular artifact exceeds the bounded table-query size",
            artifact_id=version.artifact_id,
            size_bytes=source_size,
            max_bytes=max_source_bytes,
        )

    cache = _cache_for(app)
    key = (version.sha256, fmt, request.canonical_json(limit)) if version.sha256 else None
    if key is not None:
        cached = cache.get(key)
        if cached is not None:
            return {**cached, "cached": True}

    try:
        # numpy/pyarrow load only when a query runs, not when the app registers routes.
        from clio_agent.gact.artifacts.table_query import run_table_query  # noqa: PLC0415

        result = run_table_query(
            source, fmt, request, limit=limit, deadline=deadline, timeout_s=timeout_s
        )
    except TableQueryError as exc:
        raise _error(
            exc.status_code, exc.code, exc.message, artifact_id=version.artifact_id, **exc.details
        ) from exc

    body = {"artifact_id": version.artifact_id, "name": record.name, **result}
    if key is not None:
        cache.put(key, body, table_query_cache_entries())
    return {**body, "cached": False}


def register_artifact_table_query_routes(app: FastAPI) -> None:
    """Register the bounded tabular query endpoint used by data-backed charts."""

    @app.post("/v1/artifacts/{artifact_id}/table-query")
    async def artifact_table_query(artifact_id: str, body: TableQueryRequest) -> dict[str, Any]:
        """Filter -> aggregate -> downsample -> limit over a CSV/Parquet artifact."""

        limit = _effective_limit(body.limit)
        timeout_s = table_query_timeout_s()
        deadline = time.monotonic() + timeout_s
        registry = await asyncio.to_thread(get_registry, app)
        found = registry.get_by_artifact_id(artifact_id)
        if found is None:
            raise _error(
                404,
                "not_found",
                f"artifact not found: {artifact_id}",
                artifact_id=artifact_id,
            )
        record, version = found
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _table_query, app, record, version, body, limit, deadline, timeout_s
                ),
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except TimeoutError as exc:
            timeout = TableQueryTimeout(timeout_s)
            raise _error(
                timeout.status_code,
                timeout.code,
                timeout.message,
                artifact_id=artifact_id,
                **timeout.details,
            ) from exc


__all__ = [
    "register_artifact_table_query_routes",
    "table_query_cache_entries",
    "table_query_max_rows",
    "table_query_max_source_bytes",
    "table_query_timeout_s",
]
