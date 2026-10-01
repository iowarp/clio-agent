"""Bounded server-side query / downsample over registered tabular artifacts.

``POST /v1/artifacts/{artifact_id}/table-query`` lets a chart pull a projected,
filtered, aggregated and downsampled slice of a CSV or Parquet artifact instead
of the whole file (e.g. 360 entities x 100 time points). The route is the
sibling of ``GET .../table-preview`` and shares its custody rules: only
artifacts in the registry are addressable (no filesystem paths are accepted),
bytes come from the provider-owned store / CAS blob / contained workspace path,
and every read is re-hashed against the immutable version hash.

The query engine lives in :mod:`clio_agent.gact.artifacts.table_query`; this
module owns limits, cancellation (client-disconnect PRIMARY, a configured
wall-clock backstop secondary), a concurrency guard over the shared worker
thread pool, two small result caches (the exact final response, and the
processed-but-unpaged result so paging through a large query does not
re-read/re-process the source file per page), and the error envelope.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.table_query_models import (
    DEFAULT_LIMIT,
    TableQueryError,
    TableQueryRequest,
    table_format_for,
)
from clio_agent.gact.routes.artifact_table_preview import _error
from clio_agent.gact.routes.table_route_shared import (
    import_table_engine_once,
    report_watcher_failure,
    table_source,
    watch_for_disconnect,
)

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.table_query import ProcessedTable, QueryCancellation

logger = logging.getLogger(__name__)

# A cached response larger than this many cells is not retained, so the bounded
# entry count also bounds memory.
_CACHE_MAX_CELLS = 250_000
# A cached PROCESSED (pre-page) table larger than this many bytes is not
# retained -- it is heavier than a final JSON result, so its own bound.
_PROCESSED_CACHE_MAX_BYTES = 64 * 1024 * 1024


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

    This is the SECONDARY guard, for a client that is still connected but has
    waited too long — never a statement about how much data a caller can
    reach (owner ruling: no cap that kneecaps intent); it is configurable
    (raise it for a genuinely large one-shot query) and the reason is always
    typed, never a silent drop. The PRIMARY cancellation path is the
    requesting client disconnecting (see ``table_route_shared.watch_for_disconnect``): checked
    between every stage and, in the per-entity downsample loop, on every
    entity, not only once between stage boundaries.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_query_timeout_s",
        env="CLIO_ARTIFACTS_TABLE_QUERY_TIMEOUT_S",
        default=10.0,
        cast=conf.as_float,
    )


def table_query_max_concurrency() -> int:
    """Max table-query executions running at once, sharing the worker thread pool.

    Config: ``artifacts.table_query_max_concurrency`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_MAX_CONCURRENCY`` (default 8). A slow query
    (a large source, an expensive aggregate) must not be free to exhaust the
    process's shared default thread pool and starve every other request;
    a request beyond this bound simply waits its turn, never refused.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_query_max_concurrency",
        env="CLIO_ARTIFACTS_TABLE_QUERY_MAX_CONCURRENCY",
        default=8,
        cast=conf.as_int,
    )


def table_query_processed_cache_entries() -> int:
    """How many PROCESSED (pre-page) query results the per-app cache keeps.

    Config: ``artifacts.table_query_processed_cache_entries`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_PROCESSED_CACHE_ENTRIES`` (default 8; 0
    disables it). Every page of the SAME query (same everything except
    ``offset``) reuses one entry, so paging avoids re-reading and
    re-processing the source file per page.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_query_processed_cache_entries",
        env="CLIO_ARTIFACTS_TABLE_QUERY_PROCESSED_CACHE_ENTRIES",
        default=8,
        cast=conf.as_int,
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


class ProcessedTableCache:
    """A small thread-safe LRU of PROCESSED (pre-page) query results.

    Keyed on ``(content sha256, format, canonical query WITHOUT offset)``:
    every page of the same query reuses the same entry (``ProcessedTable`` is
    read-only from here on, safe to share across concurrent requests), so
    paging through a large result costs one cheap ``.slice()`` per page
    instead of re-reading, re-filtering, re-aggregating, re-downsampling and
    re-sorting the source file each time.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str, str], "ProcessedTable"] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str, str]) -> "ProcessedTable | None":
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
            return found

    def put(self, key: tuple[str, str, str], value: "ProcessedTable", capacity: int) -> None:
        if capacity <= 0 or value.table.nbytes > _PROCESSED_CACHE_MAX_BYTES:
            return
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > capacity:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _processed_cache_for(app: FastAPI) -> ProcessedTableCache:
    cache = getattr(app.state, "table_query_processed_cache", None)
    if cache is None:
        cache = ProcessedTableCache()
        app.state.table_query_processed_cache = cache
    return cache


def _concurrency_semaphore_for(app: FastAPI) -> asyncio.Semaphore:
    """The app-wide semaphore bounding concurrent table-query executions.

    Rebuilt if the configured limit changes (a config-file edit takes effect
    on the next request, matching every other ``artifacts.table_query_*``
    knob here).
    """

    limit = table_query_max_concurrency()
    semaphore = getattr(app.state, "table_query_semaphore", None)
    if semaphore is None or getattr(app.state, "table_query_semaphore_limit", None) != limit:
        semaphore = asyncio.Semaphore(limit)
        app.state.table_query_semaphore = semaphore
        app.state.table_query_semaphore_limit = limit
    return semaphore


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
    cancellation: "QueryCancellation",
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
    source = table_source(app, record, version)
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

    # numpy/pyarrow load only when a query runs, not when the app registers routes.
    from clio_agent.gact.artifacts.table_query import (  # noqa: PLC0415
        compute_processed_table,
        page_processed_table,
    )

    processed_cache = _processed_cache_for(app)
    processed_key = (
        (version.sha256, fmt, request.canonical_json_for_processing(limit))
        if version.sha256
        else None
    )
    processed = processed_cache.get(processed_key) if processed_key is not None else None
    try:
        if processed is None:
            # Cache MISS: read, filter, aggregate, downsample and sort the
            # source file once; every page of this same query then reuses it
            # (owner ruling: paging must not re-read/re-process per page).
            processed = compute_processed_table(
                source, fmt, request, limit=limit, cancellation=cancellation
            )
            if processed_key is not None:
                processed_cache.put(processed_key, processed, table_query_processed_cache_entries())
        result = page_processed_table(processed, offset=request.offset or 0, limit=limit)
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

    # Eager, on the MAIN thread, at route-registration time (app startup) --
    # see `table_route_shared.import_table_engine_once` (#1551 review item
    # 6). The (now-cached, cheap) import inside `_table_query` below is
    # unchanged.
    import_table_engine_once()

    @app.post("/v1/artifacts/{artifact_id}/table-query")
    async def artifact_table_query(
        artifact_id: str, body: TableQueryRequest, request: Request
    ) -> dict[str, Any]:
        """Filter -> aggregate -> downsample -> sort -> offset/limit over a CSV/Parquet artifact."""

        from clio_agent.gact.artifacts.table_query import (  # noqa: PLC0415
            QueryCancellation,
            TableQueryTimeout,
        )

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

        # PRIMARY cancellation (owner ruling): a background task watches this
        # request's own HTTP connection and flips cancel_event the moment the
        # client disconnects; the engine checks it between every stage (and,
        # in the per-entity downsample loop, on every entity).
        cancel_event = threading.Event()
        cancellation = QueryCancellation(
            deadline=deadline, timeout_s=timeout_s, cancel_event=cancel_event
        )
        watcher = asyncio.ensure_future(watch_for_disconnect(request, cancel_event))
        watcher.add_done_callback(report_watcher_failure)
        semaphore = _concurrency_semaphore_for(app)
        try:
            async with semaphore:
                # The outer wait_for is the SECONDARY, hard backstop: pyarrow's
                # synchronous calls are not preemptible mid-call, so a single
                # oversized stage could otherwise still run past the deadline
                # before the next cancellation.check(); this bounds the HTTP
                # response itself regardless.
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        _table_query, app, record, version, body, limit, cancellation
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
        finally:
            # Cancel but deliberately do NOT await the watcher here. The
            # response is already computed by this point; blocking on the
            # watcher's own shutdown re-enters ``request.is_disconnected()``'s
            # raw ASGI ``receive()``, and under real scheduling delay (many
            # concurrent workers, a loaded CI runner) that in-flight receive
            # does not always unwind promptly once cancelled -- observed as
            # the whole request hanging past its 180s test timeout (#1534)
            # even though the query itself had already finished. Once a
            # result (or error) exists, the watcher has no further purpose;
            # letting it finish cancelling on its own time is safe, and
            # ``report_watcher_failure`` still surfaces a genuine bug in it
            # instead of a silently dropped exception.
            watcher.cancel()


__all__ = [
    "register_artifact_table_query_routes",
    "table_query_cache_entries",
    "table_query_max_concurrency",
    "table_query_max_rows",
    "table_query_max_source_bytes",
    "table_query_processed_cache_entries",
    "table_query_timeout_s",
]
