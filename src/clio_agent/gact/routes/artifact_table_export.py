"""``POST /v1/artifacts/{id}/table-export``: CSV/JSON/Parquet download for data views.

The download sibling of ``table-query`` (G0: "every component they make sense
for" gets a built-in download affordance, never something the agent writes
into its spec). Shares that route's custody rules (only registered artifacts,
re-hashed against the immutable version), its cancellation/size-ceiling shape,
and its concurrency budget (the SAME ``_concurrency_semaphore_for(app)``, so a
burst of exports cannot starve ordinary chart/table queries or each other) --
but never samples or pages, and never buffers a whole export in memory before
the first byte goes out, see :mod:`clio_agent.gact.artifacts.table_export` for
why.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.table_query_models import TableExportRequest, TableQueryError
from clio_agent.gact.routes.artifact_table_preview import _error
from clio_agent.gact.routes.artifact_table_query import (
    _concurrency_semaphore_for,
    table_query_max_source_bytes,
)
from clio_agent.gact.routes.content_disposition import content_disposition
from clio_agent.gact.routes.table_route_shared import (
    import_table_engine_once,
    report_watcher_failure,
    table_source,
    watch_for_disconnect,
)

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.table_export import ExportResult
    from clio_agent.gact.artifacts.table_query import QueryCancellation


def table_export_max_rows() -> int:
    """Ceiling on the PROCESSED row count a ``scope: "current"`` export may reach.

    Config: ``artifacts.table_export_max_rows`` /
    ``CLIO_ARTIFACTS_TABLE_EXPORT_MAX_ROWS`` (default 2000000) -- much higher
    than table-query's interactive ``table_query_max_rows``, since this is a
    hard refusal (``export_too_large``) past which the caller must narrow its
    filters, never a sampling trigger. Enforced on BOTH ``scope`` values and
    regardless of an explicit ``sort`` -- an export is never paged or sliced
    afterward, so nothing else would otherwise bound a sorted or "full"
    export's size.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_export_max_rows",
        env="CLIO_ARTIFACTS_TABLE_EXPORT_MAX_ROWS",
        default=2_000_000,
        cast=conf.as_int,
    )


def table_export_timeout_s() -> float:
    """Wall-clock budget, in seconds, for one export (see ``table_query_timeout_s``).

    Gates both the resolve step below AND, via the same shared
    ``QueryCancellation.deadline``, the streaming/serialization step: a
    slow client is bounded the same as a slow resolve.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.table_export_timeout_s",
        env="CLIO_ARTIFACTS_TABLE_EXPORT_TIMEOUT_S",
        default=30.0,
        cast=conf.as_float,
    )


def _resolve_export(
    app: FastAPI,
    record: ArtifactRecord,
    version: ArtifactVersion,
    body: TableExportRequest,
    cancellation: "QueryCancellation",
) -> "ExportResult":
    """Resolve + bound-check the table one export will serialize (worker thread).

    Only resolution runs here; the actual CSV/JSON/Parquet bytes are produced
    afterward by a streaming generator (see
    :func:`register_artifact_table_export_routes`), not buffered here.
    """

    from clio_agent.gact.artifacts.table_export import resolve_export_table  # noqa: PLC0415
    from clio_agent.gact.artifacts.table_query_models import table_format_for  # noqa: PLC0415

    fmt = table_format_for(record.name)
    if fmt is None:
        raise _error(
            415,
            "unsupported_media_type",
            "table export supports registered CSV and Parquet artifacts",
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
            "tabular artifact exceeds the bounded export size",
            artifact_id=version.artifact_id,
            size_bytes=source_size,
            max_bytes=max_source_bytes,
        )
    try:
        return resolve_export_table(
            source,
            fmt,
            body.as_query_request(),
            scope=body.scope,
            max_rows=table_export_max_rows(),
            cancellation=cancellation,
        )
    except TableQueryError as exc:
        raise _error(
            exc.status_code, exc.code, exc.message, artifact_id=version.artifact_id, **exc.details
        ) from exc


def register_artifact_table_export_routes(app: FastAPI) -> None:
    """Register the CSV/JSON/Parquet export endpoint for data-backed views."""

    # Eager, on the MAIN thread, at route-registration time (app startup) --
    # see `table_route_shared.import_table_engine_once` (#1551 review item 6).
    import_table_engine_once(export=True)

    @app.post("/v1/artifacts/{artifact_id}/table-export")
    async def artifact_table_export(
        artifact_id: str, body: TableExportRequest, request: Request
    ) -> StreamingResponse:
        """Stream a CSV/Parquet artifact's current view or full dataset for download."""

        from clio_agent.gact.artifacts.table_export import (  # noqa: PLC0415
            EXPORT_MEDIA_TYPES,
            export_filename,
            stream_export,
        )
        from clio_agent.gact.artifacts.table_query import QueryCancellation  # noqa: PLC0415

        timeout_s = table_export_timeout_s()
        deadline = time.monotonic() + timeout_s
        registry = await asyncio.to_thread(get_registry, app)
        found = registry.get_by_artifact_id(artifact_id)
        if found is None:
            raise _error(
                404, "not_found", f"artifact not found: {artifact_id}", artifact_id=artifact_id
            )
        record, version = found

        # One cancel token for the WHOLE request -- resolution AND streaming.
        # PRIMARY cancellation (owner ruling): the watcher keeps polling the
        # client's own connection for the entire response, including while
        # bytes are still being streamed, so an abandoned download stops
        # promptly instead of finishing unread work.
        cancel_event = threading.Event()
        cancellation = QueryCancellation(
            deadline=deadline, timeout_s=timeout_s, cancel_event=cancel_event
        )
        watcher = asyncio.ensure_future(watch_for_disconnect(request, cancel_event))
        watcher.add_done_callback(report_watcher_failure)

        # Bounds concurrent exports sharing table-query's own worker-thread
        # budget (#1551 review item 3): acquired here, released only once the
        # WHOLE streamed response has finished sending (the `BackgroundTask`
        # below) -- releasing when this handler merely RETURNS would be wrong,
        # since returning a `StreamingResponse` happens before any byte of a
        # streamed body is actually sent.
        semaphore = _concurrency_semaphore_for(app)
        await semaphore.acquire()
        released = False

        async def release_resources() -> None:
            nonlocal released
            if not released:
                released = True
                semaphore.release()
            watcher.cancel()

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_resolve_export, app, record, version, body, cancellation),
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except TimeoutError as exc:
            await release_resources()
            raise _error(
                504,
                "table_export_timeout",
                "table export exceeded its configured wall-clock backstop",
                artifact_id=artifact_id,
                timeout_s=timeout_s,
            ) from exc
        except BaseException:
            await release_resources()
            raise

        filename = export_filename(record.name, body.scope, body.format)
        return StreamingResponse(
            stream_export(result, body.format, cancellation=cancellation),
            media_type=EXPORT_MEDIA_TYPES[body.format],
            headers={
                "Content-Disposition": content_disposition(filename),
                "X-Clio-Export-Total-Rows": str(result.total_rows),
                "X-Clio-Export-Matched-Rows": str(result.matched_rows),
                "X-Clio-Export-Returned-Rows": str(result.table.num_rows),
            },
            background=BackgroundTask(release_resources),
        )


__all__ = [
    "register_artifact_table_export_routes",
    "table_export_max_rows",
    "table_export_timeout_s",
]
