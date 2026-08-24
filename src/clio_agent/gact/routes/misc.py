"""Misc session-adjacent routes for the GACT server (#714).

This concern collects the small, self-contained session-adjacent surfaces that do
not belong to a larger concern module -- each reads/writes only its own slice of
``app.state`` and the live event bus:

* ``GET/POST /v1/sessions/{sid}/tasks`` + ``PATCH/DELETE /v1/tasks/{tid}`` (#18)
  -- the per-session todo ledger. Delete is permission-gated via
  ``deps.guard_direct_destructive_action``.
* ``GET /v1/sessions/{sid}/memory/events`` + ``.../{event_id}`` -- the
  read-only per-session memory-event ledger (compaction / summary provenance).
* ``POST /v1/sessions/{sid}/share`` + ``GET /v1/shared/{token}`` (#22) -- mint a
  TTL-bounded share token and read the shared session snapshot back.
* ``GET /v1/sessions/{sid}/events`` (BBB13) -- the per-session SSE feed: a
  ``server.connected`` + ``session.snapshot`` preamble, then every published
  event, with a 15-second ``server.heartbeat`` so idle proxies do not drop it.

The module imports only leaf packages (events, runtime, types, stdlib) and never
loads :mod:`clio_agent.gact.app`. The shared task-id generator and task-lookup
helpers are concern-private and live here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from clio_agent.gact.events import Event, heartbeat_event
from clio_agent.gact.protocol_v3 import format_sse_v3, requests_gact_v3
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.runtime.constants import GACT_BACKEND_VERSION
from clio_agent.gact.runtime.globals import _format_sse
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Session
from clio_agent.runtime.stream_audit import stream_audit

# Connection-preamble events (server.connected / session.snapshot) carry this
# fixed id instead of a monotonic timeline id: it sorts before every real event
# (which start at 1) and is re-sent on every (re)connect, so the served wire never
# inverts the preamble against a replayed lower-id event.
_PREAMBLE_EVENT_ID = 0


def _sse_wire_tap(sid: str, frame: bytes, event: Event | None = None) -> None:
    """Append the EXACT bytes written to one SSE connection to a debug file.

    Opt-in via ``CLIO_SSE_WIRE_TAP`` (a file path). This records the literal
    wire — byte-for-byte what every subscribed client (the TUI included)
    receives, since :func:`_format_sse` is deterministic and the bus fans the
    same Event objects to all subscribers. Used to get a 1-1 replica of the UI
    stream for ordering/quality debugging. Best-effort: never breaks the feed.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    path = conf.resolve("debug.sse_wire_tap", env="CLIO_SSE_WIRE_TAP", default="", cast=conf.as_str)
    if path:
        try:
            with open(path, "ab") as fh:
                fh.write(frame)
        except OSError:
            pass
    if event is None:
        return
    written_at = datetime.now(timezone.utc).isoformat()
    row = {
        "sse_written_at": written_at,
        "session_id": sid,
        "event_id": event.id,
        "event_type": event.type,
        "event_occurred_at": event.occurred_at,
        "replay": bool(event.replay),
        "frame_bytes": len(frame),
        "payload_keys": sorted(event.payload.keys()),
    }
    event_log = conf.resolve(
        "debug.sse_event_log", env="CLIO_SSE_EVENT_LOG", default="", cast=conf.as_str
    ).strip()
    if event_log:
        try:
            log_path = Path(event_log).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True))
                fh.write("\n")
        except OSError:
            pass
    stream_audit("sse.write", **row)


if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_misc_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the misc session-adjacent routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach sessions/tasks/memory-events/share-tokens/the event bus through
    ``app.state``. The task-delete route reaches the shared direct-destructive-
    action guard through ``deps`` rather than importing back into ``gact.app``.
    """

    # ---- /v1/sessions/{sid}/tasks + /v1/tasks/{tid} (#18) ------------

    def _task_id() -> str:
        return f"task_{uuid.uuid4().hex[:12]}"

    @app.get("/v1/sessions/{sid}/tasks")
    async def list_session_tasks(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = list(app.state.session_tasks.get(sid, {}).values())
        return {"tasks": rows}

    @app.post("/v1/sessions/{sid}/tasks")
    async def create_session_task(sid: str, request: Request) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = await json_body(request, route="POST /v1/sessions/{sid}/tasks")
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: title",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        status = body.get("status") or "pending"
        if status not in {"pending", "running", "completed", "failed"}:
            status = "pending"
        tid = _task_id()
        row = {
            "id": tid,
            "session_id": sid,
            "title": title,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        app.state.session_tasks.setdefault(sid, {})[tid] = row
        return row

    def _find_task(tid: str) -> Optional[tuple[str, dict[str, Any]]]:
        for sid_key, rows in app.state.session_tasks.items():
            if tid in rows:
                return sid_key, rows[tid]
        return None

    @app.patch("/v1/tasks/{tid}")
    async def patch_task(tid: str, request: Request) -> dict[str, Any]:
        found = _find_task(tid)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"task not found: {tid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _, row = found
        body = await json_body(request, route="PATCH /v1/tasks/{tid}")
        if "title" in body and body["title"]:
            row["title"] = str(body["title"])
        if "status" in body and body["status"] in {"pending", "running", "completed", "failed"}:
            row["status"] = body["status"]
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        return row

    @app.delete("/v1/tasks/{tid}")
    async def delete_task(tid: str) -> Response:
        found = _find_task(tid)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"task not found: {tid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sid_key, _row = found
        sess = app.state.sessions.get(sid_key)
        deps.guard_direct_destructive_action(
            app,
            session_id=sid_key,
            workspace_id=getattr(sess, "workspace_id", ""),
            tool_name="gact.task.delete",
            args={"task_id": tid, "session_id": sid_key},
            summary=f"delete task {tid}",
            reason="user_requested_task_delete",
        )
        app.state.session_tasks[sid_key].pop(tid, None)
        return Response(status_code=204)

    # ---- /v1/sessions/{sid}/memory/events ----------------------------

    @app.get("/v1/sessions/{sid}/memory/events")
    async def list_session_memory_events(sid: str, limit: int = 50) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if limit <= 0:
            limit = 50
        limit = min(limit, 200)
        events = list(app.state.memory_events.get(sid, []))
        return {"events": events[-limit:]}

    @app.get("/v1/sessions/{sid}/memory/events/{event_id}")
    async def get_session_memory_event(sid: str, event_id: str) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        event = next(
            (row for row in app.state.memory_events.get(sid, []) if row.get("id") == event_id),
            None,
        )
        if event is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"memory event not found: {event_id}",
                        details={"session_id": sid, "event_id": event_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"event": event}

    # ---- /v1/sessions/{sid}/share + /v1/shared/{token} (#22) ---------

    @app.post("/v1/sessions/{sid}/share")
    async def share_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = await json_body(request, route="POST /v1/sessions/{sid}/share")
        ttl_s = int(body.get("ttl_s") or 0)
        token = "shr_" + uuid.uuid4().hex[:24]
        expires_at: str | float = ""
        if ttl_s > 0:
            expires_at = datetime.now(timezone.utc).timestamp() + ttl_s
        app.state.shared_tokens[token] = {
            "session_id": sid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
        }
        enforce_dict_bound(app, app.state.shared_tokens, "shared_tokens", session_id=sid)
        return {
            "token": token,
            "session_id": sid,
            "url": f"/v1/shared/{token}",
            "expires_at": expires_at,
        }

    @app.get("/v1/shared/{token}")
    async def get_shared(token: str) -> dict[str, Any]:
        row = app.state.shared_tokens.get(token)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"share token not found: {token}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Expiry check.
        expires_at = row.get("expires_at") or 0
        if expires_at and (datetime.now(timezone.utc).timestamp() > float(expires_at)):
            app.state.shared_tokens.pop(token, None)
            raise HTTPException(
                status_code=410,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="share token expired",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sid = row["session_id"]
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=(f"underlying session {sid} no longer exists"),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        msgs = app.state.messages.get(sid, [])
        return {
            "session": Session(**sess.to_wire()).model_dump(exclude_none=True),
            "messages": [m.model_dump(exclude_none=True) for m in msgs],
            "shared_at": row.get("created_at"),
        }

    # ---- /v1/sessions/{sid}/events SSE (BBB13) -----------------------

    @app.get("/v1/sessions/{sid}/events")
    async def session_events(sid: str, request: Request) -> StreamingResponse:
        """SSE feed for one session. Emits the events POST /messages
        publishes (status_changed, message.created, message.part.*,
        message.completed) plus periodic 15-s heartbeats so HTTP
        proxies don't drop the idle connection.

        Per SPEC §7.1: streams forever until the client disconnects.
        Emits ``server.connected`` immediately so clients can confirm
        the wire is healthy before any real event arrives.
        """

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        use_v3 = requests_gact_v3(request)

        def _format_event(event: Event, session: Any) -> bytes:
            if use_v3:
                return format_sse_v3(
                    event,
                    session=session,
                    workspace_id=str(getattr(session, "workspace_id", "") or ""),
                )
            return _format_sse(event)

        async def event_stream() -> AsyncIterator[bytes]:
            sess_snapshot = app.state.sessions.get(sid)
            # Initial server.connected event so clients can flip
            # their UI from "connecting" to "live" immediately.
            #
            # The preamble (server.connected + session.snapshot) is CONNECTION meta,
            # not part of the session's event timeline. Event() assigns a monotonic id
            # at construction (events.py `_event_id_counter`), so constructing them here
            # would grab the NEXT ids — landing AFTER any already-buffered event (e.g.
            # an `lm.provider.changed` from bind) that the replay below then re-sends
            # with a LOWER id. Delivered out of id order (2, 3, 1 …), a sort-by-id TUI
            # hoists the replayed event above the preamble ("backwards messages"). Pin
            # the preamble to id 0 (< every real id, always re-sent on reconnect) so the
            # served wire stays monotonic: 0, 0, <timeline ids ascending>.
            connected = Event(
                type="server.connected",
                session_id=sid,
                payload={"server_version": GACT_BACKEND_VERSION},
            )
            connected.id = _PREAMBLE_EVENT_ID
            _frame = _format_event(connected, sess_snapshot)
            _sse_wire_tap(sid, _frame, connected)
            yield _frame
            if sess_snapshot is not None:
                snapshot = Event(
                    type="session.snapshot",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "status": sess_snapshot.status,
                        "updated_at": sess_snapshot.updated_at,
                        "authoritative": True,
                    },
                )
                snapshot.id = _PREAMBLE_EVENT_ID
                _frame = _format_event(snapshot, sess_snapshot)
                _sse_wire_tap(sid, _frame, snapshot)
                yield _frame

            try:
                last_event_id = int(request.headers.get("last-event-id", "0"))
            except (TypeError, ValueError):
                last_event_id = 0
            if use_v3 and last_event_id > app.state.bus.highest_event_id:
                # A process restart resets the in-memory timeline. Waiting for
                # the new process to count past an old Last-Event-ID leaves the
                # client live-looking but permanently stale. GACT 0.3 makes the
                # epoch mismatch explicit so the client can reconcile from REST
                # and then resume this new timeline from its beginning. Keep the
                # connection-local marker at id 0 like the other preamble frames;
                # it is a state transition, not a durable session event.
                gap = Event(
                    type="stream.gap",
                    session_id=sid,
                    payload={
                        "reason": "cursor_epoch_reset",
                        "requested_cursor": str(last_event_id),
                        "new_timeline_head": str(app.state.bus.highest_event_id),
                    },
                )
                gap.id = _PREAMBLE_EVENT_ID
                _frame = _format_event(gap, app.state.sessions.get(sid))
                _sse_wire_tap(sid, _frame, gap)
                yield _frame
                last_event_id = 0
            sub = app.state.bus.subscribe(sid, last_event_id=last_event_id)
            heartbeat_task: Optional[asyncio.Task] = None
            try:
                # Heartbeat task — pumps a server.heartbeat event
                # into the queue every 15s. SPEC §7.1.
                # Transient (live-delivery only): heartbeats must not
                # enter the replay history or count as watchdog
                # progress — see events.heartbeat_event (#761).
                async def _heartbeat() -> None:
                    while True:
                        await asyncio.sleep(15)
                        app.state.bus.publish(heartbeat_event(sid))

                heartbeat_task = asyncio.create_task(_heartbeat())

                async for event in sub:
                    _frame = _format_event(event, app.state.sessions.get(sid))
                    _sse_wire_tap(sid, _frame, event)
                    yield _frame
            except asyncio.CancelledError:
                # Client disconnected. Cleanup happens in `finally`.
                pass
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
            },
        )
