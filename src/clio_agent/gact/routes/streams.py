"""``/v1/streams``: a view's reads of a stream, and the lease that makes it watched.

- ``GET /v1/streams/{id}``: the stream (state, rows held, writes, times, its view URI).
- ``POST /v1/streams/{id}/table-query``: the rows in memory now, through the same
  filter, aggregate, sort and page pipeline as an artifact (``schema``, ``columns``,
  ``totalRows`` ...), plus ``stream`` (the state, so a view can say it stopped).
- ``POST /v1/streams/{id}/watch``: ``{session_id, release?}`` holds or releases that
  session's lease (:mod:`clio_agent.gact.streams.push`); a view renews it while shown.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from clio_agent.gact.artifacts.table_query_models import TableQueryError, TableQueryRequest
from clio_agent.gact.routes.artifact_table_preview import _error


class StreamWatchRequest(BaseModel):
    """A view's lease on a stream, for the session it is shown in."""

    session_id: str
    release: bool = False


def _stream(app: Any, stream_id: str) -> Any:
    from clio_agent.gact.streams.store import stream_store  # noqa: PLC0415

    stream = stream_store(app).get(stream_id)
    if stream is None:
        raise _error(404, "stream_not_found", f"No stream {stream_id!r} on this server.")
    return stream


def stream_table_query(
    app: Any, stream_id: str, request: TableQueryRequest, limit: int
) -> dict[str, Any]:
    """Run the table-query pipeline over the stream's rows now (off the loop)."""

    import pyarrow.parquet as pq  # noqa: PLC0415

    from clio_agent.gact.artifacts.table_query import (  # noqa: PLC0415
        QueryCancellation,
        compute_processed_table,
        page_processed_table,
    )
    from clio_agent.gact.streams.persist import rows_with_columns  # noqa: PLC0415

    stream = _stream(app, stream_id)
    with stream.lock:
        rows, columns, described = list(stream.rows), list(stream.columns), stream.describe()
    table = rows_with_columns(rows, columns)
    if table.num_columns == 0:
        return {
            "schema": [],
            "columns": {},
            "totalRows": 0,
            "matchedRows": 0,
            "returnedRows": 0,
            "truncated": False,
            "offset": request.offset or 0,
            "downsample": {"mode": "none"},
            "empty": "stream_empty",
            "cached": False,
            "stream": described,
        }
    with tempfile.TemporaryDirectory(prefix="clio-stream-") as folder:
        path = Path(folder) / "stream.parquet"
        pq.write_table(table, path)
        try:
            processed = compute_processed_table(
                path,
                "parquet",
                request,
                limit=limit,
                cancellation=QueryCancellation(cancel_event=threading.Event()),
            )
            result = page_processed_table(processed, offset=request.offset or 0, limit=limit)
        except TableQueryError as exc:
            raise _error(
                exc.status_code, exc.code, exc.message, stream=stream_id, **exc.details
            ) from exc
    return {**result, "cached": False, "stream": described}


def register_stream_routes(app: FastAPI) -> None:
    """Register the stream reads and the watch lease."""

    from clio_agent.gact.routes.artifact_table_query import _effective_limit  # noqa: PLC0415

    @app.get("/v1/streams/{stream_id}")
    async def get_stream(stream_id: str) -> dict[str, Any]:
        return dict(_stream(app, stream_id).describe())

    @app.post("/v1/streams/{stream_id}/table-query")
    async def query_stream(stream_id: str, body: TableQueryRequest) -> dict[str, Any]:
        limit = _effective_limit(body.limit)
        return await asyncio.to_thread(stream_table_query, app, stream_id, body, limit)

    @app.post("/v1/streams/{stream_id}/watch")
    async def watch_stream(stream_id: str, body: StreamWatchRequest) -> dict[str, Any]:
        from clio_agent.gact.streams.push import LEASE_S, watch  # noqa: PLC0415

        _stream(app, stream_id)
        watchers = watch(stream_id, body.session_id, release=body.release)
        return {"stream_id": stream_id, "watchers": watchers, "lease_s": LEASE_S}


__all__ = ["StreamWatchRequest", "register_stream_routes", "stream_table_query"]
