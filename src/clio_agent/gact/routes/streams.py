"""``/v1/streams``: a view's reads of a stream, and the lease that makes it watched.

A stream's content lives in clio-core (:mod:`clio_agent.gact.streams.store`); these
routes read it as it is, with each entry's type. Any reshaping a chart wants it does in
the browser, so there is no query pipeline here.

- ``GET /v1/streams/{id}``: the stream (state, entries held, writes, times, view URI).
- ``GET /v1/streams/{id}/content?limit=``: the stream and its newest entries, oldest
  first: JSON entries as ``value``, text as ``text``, other types by ``size_bytes`` and
  their ``entry_path``.
- ``GET /v1/streams/{id}/entries/{seq}``: one entry's bytes, with its media type.
- ``POST /v1/streams/{id}/watch``: ``{session_id, release?, lease_id?}`` holds or
  releases a view's lease (:mod:`clio_agent.gact.streams.push`); a view renews it while
  shown. The session must exist (404 ``session_not_found``) and be in the stream's
  workspace (403 ``stream_not_in_session_workspace``).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Response
from pydantic import BaseModel

from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.routes.artifact_table_preview import _error


class StreamWatchRequest(BaseModel):
    """A view's lease on a stream, for the session it is shown in (``lease_id``: the view's
    own, when one session shows the stream in more than one view)."""

    session_id: str
    release: bool = False
    lease_id: str = ""


def _stream(app: Any, stream_id: str) -> Any:
    from clio_agent.gact.streams.store import StreamError, stream_store  # noqa: PLC0415

    try:
        stream = stream_store(app).get(stream_id)
    except StreamError as exc:
        raise _error(503, exc.code, str(exc)) from exc
    if stream is None:
        raise _error(404, "stream_not_found", f"No stream {stream_id!r}.")
    return stream


def stream_content(app: Any, stream_id: str, limit: int) -> dict[str, Any]:
    """The stream and its newest ``limit`` entries, read from clio-core."""
    from clio_agent.gact.streams.store import WINDOW_MAX, stream_store  # noqa: PLC0415

    stream = _stream(app, stream_id)
    entries = stream_store(app).read(stream, max(1, min(int(limit), WINDOW_MAX)))
    for entry in entries:
        if "value" not in entry and "text" not in entry:
            entry["entry_path"] = f"/v1/streams/{stream_id}/entries/{entry['seq']}"
    fresh = stream_store(app).get(stream_id, stream.workspace_id) or stream
    return {"stream": fresh.describe(), "entries": entries}


def register_stream_routes(app: FastAPI) -> None:
    """Register the stream reads and the watch lease."""

    @app.get("/v1/streams/{stream_id}")
    async def get_stream(stream_id: str) -> dict[str, Any]:
        return dict((await run_off_loop(lambda: _stream(app, stream_id))).describe())

    @app.get("/v1/streams/{stream_id}/content")
    async def get_stream_content(stream_id: str, limit: int = 1000) -> dict[str, Any]:
        return await run_off_loop(lambda: stream_content(app, stream_id, limit))

    @app.get("/v1/streams/{stream_id}/entries/{seq}")
    async def get_stream_entry(stream_id: str, seq: int) -> Response:
        from clio_agent.gact.streams.store import stream_store  # noqa: PLC0415

        def read() -> tuple[str, bytes] | None:
            return stream_store(app).entry(_stream(app, stream_id), seq)

        found = await run_off_loop(read)
        if found is None:
            raise _error(404, "stream_entry_not_held", "The window no longer holds that entry.")
        media_type, body = found
        return Response(content=body, media_type=media_type or "application/octet-stream")

    @app.post("/v1/streams/{stream_id}/watch")
    async def watch_stream(stream_id: str, body: StreamWatchRequest) -> dict[str, Any]:
        from clio_agent.gact.streams.push import LEASE_S, watch  # noqa: PLC0415

        stream = await run_off_loop(lambda: _stream(app, stream_id))
        session = app.state.sessions.get(body.session_id) if body.session_id else None
        if session is None:
            raise _error(404, "session_not_found", f"No session {body.session_id!r}.")
        if str(getattr(session, "workspace_id", "") or "") != stream.workspace_id:
            raise _error(
                403,
                "stream_not_in_session_workspace",
                "The stream belongs to another workspace than this session's.",
            )
        watchers = watch(stream_id, body.session_id, release=body.release, lease_id=body.lease_id)
        return {"stream_id": stream_id, "watchers": watchers, "lease_s": LEASE_S}


__all__ = ["StreamWatchRequest", "register_stream_routes", "stream_content"]
