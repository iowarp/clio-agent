"""``POST /v1/studies/{study}/table-query``: a view's live read of a study source.

The body is the artifact table-query request plus ``source`` (``measurements``,
``entries`` or ``records/<view>``). The source is built from the study's current
record on every call, written to a temporary Parquet file, and run through the same
filter, aggregate, downsample, sort and page pipeline as an artifact, so a view's chart
reads a study exactly the way it reads a file. Nothing is cached: the next call sees
the next entry.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from clio_agent.gact.artifacts.table_query_models import TableQueryError, TableQueryRequest
from clio_agent.gact.routes.artifact_table_preview import _error


class StudyTableQueryRequest(TableQueryRequest):
    """A table-query request naming the study source it reads."""

    source: str


def study_table_query(
    app: Any, study: str, body: StudyTableQueryRequest, limit: int
) -> dict[str, Any]:
    """Build the source and run the table-query pipeline over it (off the loop)."""

    import pyarrow.parquet as pq  # noqa: PLC0415

    from clio_agent.gact.artifacts.table_query import (  # noqa: PLC0415
        QueryCancellation,
        compute_processed_table,
        page_processed_table,
    )
    from clio_agent.gact.science_log.entries import EntryRejected  # noqa: PLC0415
    from clio_agent.gact.science_log.sources import (  # noqa: PLC0415
        parse_study_uri,
        rows_table,
        source_rows,
    )

    try:
        _, source, view = parse_study_uri(f"study://{study}/{body.source}")
        rows = source_rows(app, study, source, view)
    except EntryRejected as exc:
        raise _error(404 if exc.reason == "study_not_found" else 400, exc.reason, str(exc)) from exc
    request = TableQueryRequest.model_validate(
        body.model_dump(exclude={"source"}, exclude_none=True)
    )
    if not rows:
        # A study with nothing recorded yet has no columns to query; the view shows an
        # empty result, which fills once entries arrive.
        names = list(request.columns or [])
        return {
            "study": study,
            "source": body.source,
            "schema": [{"name": name, "type": "null"} for name in names],
            "columns": {name: [] for name in names},
            "totalRows": 0,
            "matchedRows": 0,
            "returnedRows": 0,
            "truncated": False,
            "offset": request.offset or 0,
            "downsample": None,
            "empty": "study_source_empty",
            "cached": False,
        }
    with tempfile.TemporaryDirectory(prefix="clio-study-source-") as folder:
        path = Path(folder) / "source.parquet"
        pq.write_table(rows_table(rows), path)
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
                exc.status_code, exc.code, exc.message, study=study, **exc.details
            ) from exc
    return {"study": study, "source": body.source, **result, "cached": False}


def register_study_source_routes(app: FastAPI) -> None:
    """Register the live study-source query the Science log's views read."""

    from clio_agent.gact.routes.artifact_table_query import _effective_limit  # noqa: PLC0415

    @app.post("/v1/studies/{study}/table-query")
    async def study_source_query(study: str, body: StudyTableQueryRequest) -> dict[str, Any]:
        limit = _effective_limit(body.limit)
        return await asyncio.to_thread(study_table_query, app, study, body, limit)


__all__ = ["StudyTableQueryRequest", "register_study_source_routes", "study_table_query"]
