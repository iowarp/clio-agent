"""Live progress and log of one infrastructure operation (SSE and polling).

``GET /v1/infrastructure/operations/{id}/events`` streams, as SSE frames like
``GET /v1/sessions/{sid}/events`` (``event:``/``id:``/``data:``):

* ``operation.snapshot`` (no id): the whole operation record, first;
* ``operation.progress``: steps, current step, per-step progress, reuse,
  elapsed time (the record's progress fields);
* ``operation.log``: ``{"line", "stream", "step"}`` -- one redacted output line,
  in order;
* ``operation.reuse``: one verified, skipped install step;
* ``operation.completed``: the final record; the stream then ends;
* ``server.heartbeat`` (no id) every 15 s of silence, ``stream.gap`` (no id)
  when the cursor is older than the retained window.

``Last-Event-ID`` (or ``?after=``) is the highest event id the client holds;
replay is exclusive. ``GET .../log?after=&limit=`` returns the same log lines
as JSON for clients that poll.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from clio_agent.gact.infrastructure.operation_events import (
    LOG_EVENT,
    TERMINAL_EVENT,
    OperationEvent,
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _cursor(request: Request, after: int | None) -> int:
    if after is not None:
        return max(0, after)
    try:
        return max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        return 0


def register_infrastructure_operation_routes(app: FastAPI) -> None:
    """Register the operation event stream and log polling routes."""

    @app.get("/v1/infrastructure/operations/{operation_id}/events")
    async def operation_events(
        operation_id: str, request: Request, after: int | None = Query(default=None, ge=0)
    ) -> StreamingResponse:
        store = app.state.infrastructure_store
        events = app.state.infrastructure_runtime.events
        row = store.operation(operation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Infrastructure operation not found")
        cursor = _cursor(request, after)

        async def stream() -> AsyncIterator[bytes]:
            yield OperationEvent(
                0, "operation.snapshot", operation_id, row.model_dump(mode="json")
            ).frame()
            if not events.known(operation_id):
                # Not run by this process (it restarted): the record is all there is.
                if row.state not in {"queued", "running"}:
                    yield OperationEvent(
                        0, TERMINAL_EVENT, operation_id, row.model_dump(mode="json")
                    ).frame()
                    return
            # A client that goes away cancels this generator (StreamingResponse).
            async for event in events.follow(operation_id, cursor):
                yield event.frame()

        return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    @app.get("/v1/infrastructure/operations/{operation_id}/log")
    async def operation_log(
        operation_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, object]:
        """Live log lines newer than ``after`` (an event id), oldest first."""

        row = app.state.infrastructure_store.operation(operation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Infrastructure operation not found")
        events = app.state.infrastructure_runtime.events
        rows, truncated = events.after(operation_id, after, kinds=frozenset({LOG_EVENT}))
        rows = rows[:limit]
        return {
            "operation_id": operation_id,
            "state": row.state,
            "lines": [{"id": event.id, "at": event.at, **event.payload} for event in rows],
            "next_cursor": rows[-1].id if rows else max(after, events.latest_id(operation_id)),
            "truncated": truncated,
            "complete": row.state not in {"queued", "running"},
        }


__all__ = ["register_infrastructure_operation_routes"]
