"""Bounded server-side query / downsample over registered tabular artifacts.

``POST /v1/artifacts/{artifact_id}/table-query`` lets a chart pull a projected,
filtered, aggregated and downsampled slice of a CSV or Parquet artifact instead
of the whole file (e.g. 360 entities x 100 time points). The route is the
sibling of ``GET .../table-preview`` and shares its custody rules: only
artifacts in the registry are addressable (no filesystem paths are accepted),
bytes come from the provider-owned store / CAS blob / contained workspace path,
and every read is re-hashed against the immutable version hash.

The query engine lives in :mod:`clio_agent.gact.artifacts.table_query`; this
module owns limits, cancellation (the client disconnecting), the
progress-based wait (the query runs on its own thread and is waited for while
that thread keeps consuming CPU, :mod:`clio_agent.runtime.progress`),
a concurrency guard over those query threads, two small result caches (the exact final response, and the
processed-but-unpaged result so paging through a large query does not
re-read/re-process the source file per page), and the error envelope.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.artifacts.cas import sha256_file
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.table_query_models import (
    DEFAULT_LIMIT,
    TableQueryError,
    TableQueryRequest,
    table_format_for,
)
from clio_agent.gact.routes.artifact_table_preview import (
    _artifact_source,
    _error,
    _workspace_root,
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
# How often the disconnect watcher polls the client connection.
_DISCONNECT_POLL_S = 0.25


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


def table_query_no_progress_s() -> float:
    """Seconds a table query may go without an answer AND without CPU work.

    Config: ``artifacts.table_query_no_progress_s`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_NO_PROGRESS_S`` (default 30). The query runs
    on its own thread; while that thread keeps consuming CPU the client keeps
    waiting (a large query on a slow machine is never cut off by a clock). A
    whole window with neither is a typed 504 ``table_query_stalled``
    (``reason: no_progress``).
    """

    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "artifacts.table_query_no_progress_s",
        env="CLIO_ARTIFACTS_TABLE_QUERY_NO_PROGRESS_S",
        default=30.0,
        cast=conf.as_float,
    )
    return value if value > 0 else 30.0


def table_query_max_wait_s() -> float:
    """Ceiling, in seconds, on waiting for a table query that keeps working.

    Config: ``artifacts.table_query_max_wait_s`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_MAX_WAIT_S`` (default 180). A query still
    consuming CPU at the ceiling is a typed 504 ``table_query_stalled``
    (``reason: ceiling``).
    """

    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "artifacts.table_query_max_wait_s",
        env="CLIO_ARTIFACTS_TABLE_QUERY_MAX_WAIT_S",
        default=180.0,
        cast=conf.as_float,
    )
    return value if value > 0 else 180.0


def table_query_max_concurrency() -> int:
    """Max table-query executions running at once, each on its own thread.

    Config: ``artifacts.table_query_max_concurrency`` /
    ``CLIO_ARTIFACTS_TABLE_QUERY_MAX_CONCURRENCY`` (default 8). Slow queries
    (a large source, an expensive aggregate) must not be free to pile up
    threads and starve every other request of CPU; a request beyond this
    bound simply waits its turn, never refused.
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


async def _watch_for_disconnect(request: Request, cancel_event: threading.Event) -> None:
    """Set ``cancel_event`` as soon as ``request``'s own HTTP client disconnects.

    The PRIMARY cancellation path (owner ruling): polled rather than a single
    ``await``, since Starlette's ``is_disconnected`` only reports a truthful
    answer when asked repeatedly. Cancelled by the route once the query
    finishes (success or error) either way -- this task never outlives one
    request.
    """

    while True:
        if await request.is_disconnected():
            cancel_event.set()
            return
        await asyncio.sleep(_DISCONNECT_POLL_S)


def _report_watcher_failure(task: "asyncio.Task[None]") -> None:
    """Log an unexpected ``_watch_for_disconnect`` failure; never re-raise it.

    The route cancels the watcher fire-and-forget (see the route's own
    ``finally``), so this done-callback is the only place a bug in the
    watcher itself would otherwise surface -- without it, a non-cancellation
    exception here would just vanish into "Task exception was never
    retrieved" at GC time instead of a readable log line (no silent
    fallback). Cancellation is the expected, silent outcome.
    """

    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("table-query disconnect watcher failed: %r", exc)


def _owned_error(
    *, status_code: int, error: str, message: str, details: dict[str, Any] | None = None
) -> HTTPException:
    """Adapt the storage-owner error seam onto this route's envelope."""

    return _error(status_code, error, message, **(details or {}))


def _table_source(
    app: FastAPI,
    record: ArtifactRecord,
    version: ArtifactVersion,
    *,
    verify: bool = True,
) -> Path:
    """Resolve bytes exactly as ``/bytes`` does: owned store first, then CAS/path.

    ``verify`` (default ``True``) re-hashes the resolved bytes against
    ``version.sha256``, exactly like every table-query/table-preview serving
    path. ``verify=False`` trusts the artifact record's own recorded metadata
    instead of re-reading and re-hashing the whole file — used by producer-
    side, non-serving checks (e.g. ``_data_reference.py``'s dataQuery/*Field
    shape validation, #1533 S4 adversarial review item 6) that run on every
    ``create_a2ui_surface``/``update_a2ui_components`` call and must not pay
    a full artifact re-hash each time just to check column names.
    """

    from clio_agent.gact.artifacts.storage import (  # noqa: PLC0415
        resolve_owned_artifact_or_raise,
    )

    root = _workspace_root(app, record.workspace_id)
    owned = resolve_owned_artifact_or_raise(app, version, workspace_root=root, error=_owned_error)
    if owned is None:
        return _artifact_source(app, record, version, verify=verify)
    if verify and version.sha256:
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

    @app.post("/v1/artifacts/{artifact_id}/table-query")
    async def artifact_table_query(
        artifact_id: str, body: TableQueryRequest, request: Request
    ) -> dict[str, Any]:
        """Filter -> aggregate -> downsample -> sort -> offset/limit over a CSV/Parquet artifact."""

        from clio_agent.gact.artifacts.table_query import QueryCancellation  # noqa: PLC0415
        from clio_agent.gact.artifacts.table_query_models import (  # noqa: PLC0415
            TableQueryCancelled,
            TableQueryStalled,
        )
        from clio_agent.runtime.progress import (  # noqa: PLC0415
            ThreadStalled,
            ThreadWorkUnresolved,
            run_while_thread_works,
        )

        limit = _effective_limit(body.limit)
        no_progress_s = table_query_no_progress_s()
        max_wait_s = table_query_max_wait_s()
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

        # A background task watches this request's own HTTP connection and
        # flips cancel_event the moment the client disconnects (the primary
        # cancellation, owner ruling); the engine checks it between every stage
        # (and, in the per-entity downsample loop, on every entity), and the
        # wait below ends at once, even while the query thread is blocked.
        cancel_event = threading.Event()
        cancellation = QueryCancellation(cancel_event=cancel_event)
        watcher = asyncio.ensure_future(_watch_for_disconnect(request, cancel_event))
        watcher.add_done_callback(_report_watcher_failure)
        semaphore = _concurrency_semaphore_for(app)
        try:
            async with semaphore:
                query = asyncio.ensure_future(
                    run_while_thread_works(
                        lambda: _table_query(app, record, version, body, limit, cancellation),
                        op=f"table-query {artifact_id}",
                        no_progress_s=no_progress_s,
                        ceiling_s=max_wait_s,
                        thread_name="clio-table-query",
                    )
                )
                done, _pending = await asyncio.wait(
                    {query, watcher}, return_when=asyncio.FIRST_COMPLETED
                )
                if query not in done:
                    query.cancel()
                    cancelled = TableQueryCancelled()
                    raise _error(
                        cancelled.status_code,
                        cancelled.code,
                        cancelled.message,
                        artifact_id=artifact_id,
                    )
                return query.result()
        except ThreadStalled as exc:
            # Tell the query thread to stop at its next check should it resume.
            cancel_event.set()
            stalled = TableQueryStalled(exc.reason, exc.waited_s, no_progress_s)
            raise _error(
                stalled.status_code,
                stalled.code,
                stalled.message,
                artifact_id=artifact_id,
                **stalled.details,
            ) from exc
        except ThreadWorkUnresolved as exc:
            cancel_event.set()
            raise _error(
                500,
                "table_query_progress_unmeasurable",
                "the table query's progress (its thread's CPU time) cannot be read here",
                artifact_id=artifact_id,
                detail=str(exc),
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
            # ``_report_watcher_failure`` still surfaces a genuine bug in it
            # instead of a silently dropped exception.
            watcher.cancel()


__all__ = [
    "register_artifact_table_query_routes",
    "table_query_cache_entries",
    "table_query_max_concurrency",
    "table_query_max_rows",
    "table_query_max_source_bytes",
    "table_query_max_wait_s",
    "table_query_no_progress_s",
    "table_query_processed_cache_entries",
]
