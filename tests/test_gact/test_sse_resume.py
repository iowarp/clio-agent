"""Route-level SSE resume integrity for /v1/sessions/{sid}/events (#773).

The endpoint's ``Last-Event-ID`` resume path (routes/misc.py) is the
seam a reconnecting TUI relies on to catch up without gaps or dupes.
``test_sse.py`` covers the EventBus replay primitive in isolation and
the 404 smoke; this module drives the *route* end to end so the header
parse, the pinned-id preamble, and the ``bus.subscribe(last_event_id=)``
wiring are exercised together.

TestClient deadlocks on the unbounded SSE StreamingResponse (see the
note at the bottom of test_sse.py), and httpx's ASGITransport buffers
SSE chunks. So we drive the ASGI app directly: construct the http scope
ourselves, collect each ``http.response.body`` message (Starlette emits
one per ``yield`` in the stream generator), and cancel the request task
once we have the frames we need. Every read is bounded by
``asyncio.wait_for`` so a regression that drops a frame fails fast
instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event

# ---- ASGI driving helpers -------------------------------------------------

_READ_TIMEOUT = 3.0


def _parse_frame(frame: bytes) -> dict[str, Any]:
    """Decode one ``event:/id:/data:`` SSE frame into its parts."""

    text = frame.decode("utf-8")
    ev_type: str | None = None
    ev_id: int | None = None
    data: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("event: "):
            ev_type = line.removeprefix("event: ")
        elif line.startswith("id: "):
            ev_id = int(line.removeprefix("id: "))
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
    assert ev_type is not None and ev_id is not None and data is not None, text
    return {"type": ev_type, "id": ev_id, "data": data}


class _SSEConnection:
    """A live ASGI request against the SSE route.

    Runs the app coroutine as a task and exposes each streamed frame via
    a queue. Cancels the task (which cancels the route's heartbeat task
    in its ``finally``) on exit.
    """

    def __init__(
        self,
        app: Any,
        sid: str,
        last_event_id: str | None,
        *,
        gact_version: str = "",
        a2ui_version: str = "",
    ) -> None:
        self._app = app
        self._sid = sid
        self._last_event_id = last_event_id
        self._gact_version = gact_version
        self._a2ui_version = a2ui_version
        self._frames: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.status: int | None = None

    async def __aenter__(self) -> "_SSEConnection":
        headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
        if self._last_event_id is not None:
            headers.append((b"last-event-id", self._last_event_id.encode("utf-8")))
        if self._gact_version:
            headers.append((b"x-gact-version", self._gact_version.encode("utf-8")))
        if self._a2ui_version:
            headers.append((b"x-a2ui-version", self._a2ui_version.encode("utf-8")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/v1/sessions/{self._sid}/events",
            "raw_path": f"/v1/sessions/{self._sid}/events".encode("utf-8"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": ("testserver", 80),
            "client": ("testclient", 12345),
        }

        async def receive() -> dict[str, Any]:
            # No request body; keep the connection "open" so the stream
            # doesn't see a disconnect until we cancel the task.
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    self._frames.put_nowait(body)

        self._task = asyncio.create_task(self._app(scope, receive, send))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def read_frame(self, timeout: float = _READ_TIMEOUT) -> dict[str, Any]:
        raw = await asyncio.wait_for(self._frames.get(), timeout)
        return _parse_frame(raw)

    async def read_frames(self, n: int, timeout: float = _READ_TIMEOUT) -> list[dict[str, Any]]:
        return [await self.read_frame(timeout) for _ in range(n)]

    async def assert_no_frame(self, timeout: float = 0.3) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(self._frames.get(), timeout)


async def _wait_subscribed(app: Any, sid: str, timeout: float = _READ_TIMEOUT) -> None:
    """Block until the route has registered its bus subscriber queue."""

    deadline = asyncio.get_running_loop().time() + timeout
    while app.state.bus.subscriber_count(sid) < 1:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("route never subscribed to the bus")
        await asyncio.sleep(0.01)


def _make_app_with_session(tmp_path: Any) -> tuple[Any, str]:
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t")
    return app, sess.id


def _seed_history(app: Any, sid: str, n: int) -> list[Event]:
    """Publish ``n`` real timeline events into the bus replay history.

    No SSE subscriber exists yet, so ``publish`` only appends to the
    per-session replay log (events.py `_deliver`). Returns them so tests
    can key on their monotonic ids.
    """

    events: list[Event] = []
    for i in range(n):
        ev = Event(type="message.created", session_id=sid, payload={"n": i})
        app.state.bus.publish(ev)
        events.append(ev)
    return events


# ---- Preamble helpers -----------------------------------------------------

_PREAMBLE_TYPES = ("server.connected", "session.snapshot")


async def _read_preamble(conn: _SSEConnection) -> list[dict[str, Any]]:
    """Read + assert the two connection-meta frames (both pinned id 0)."""

    preamble = await conn.read_frames(2)
    assert [f["type"] for f in preamble] == list(_PREAMBLE_TYPES)
    # Pinned to id 0 so the served wire stays monotonic on reconnect
    # (routes/misc.py preamble comment) and never marked replay.
    assert all(f["id"] == 0 for f in preamble)
    assert all("replay" not in f["data"] for f in preamble)
    return preamble


# ---- (a) exact resume -----------------------------------------------------


async def test_resume_replays_exactly_after_last_event_id(tmp_path: Any) -> None:
    """Last-Event-ID k → replay exactly k+1..N as monotonic replay events."""

    app, sid = _make_app_with_session(tmp_path)
    events = _seed_history(app, sid, 5)
    cutoff = events[1].id  # client already has up through the 2nd event
    expected_ids = [e.id for e in events[2:]]

    async with _SSEConnection(app, sid, last_event_id=str(cutoff)) as conn:
        await _read_preamble(conn)
        replayed = await conn.read_frames(len(expected_ids))

    assert [f["id"] for f in replayed] == expected_ids  # exactly k+1..N
    assert [f["id"] for f in replayed] == sorted({f["id"] for f in replayed})  # monotonic, no dupes
    assert all(f["type"] == "message.created" for f in replayed)
    assert all(f["data"]["replay"] is True for f in replayed)
    # Nothing at or below the cutoff is re-sent.
    assert all(f["id"] > cutoff for f in replayed)


# ---- (b) malformed header pins current full-replay behavior ---------------


@pytest.mark.parametrize("bad_header", ["not-an-int", "", "12abc", "3.5", "  "])
async def test_malformed_last_event_id_falls_back_to_full_replay(
    tmp_path: Any, bad_header: str
) -> None:
    """Unparseable Last-Event-ID → full replay from 0 (pins routes/misc.py).

    This documents/locks the CURRENT behavior: the header parse swallows
    TypeError/ValueError to ``last_event_id = 0``, so the whole history
    replays. This is a characterization test — do NOT change route
    behavior to make it pass; if the route changes, update this pin
    deliberately.
    """

    app, sid = _make_app_with_session(tmp_path)
    events = _seed_history(app, sid, 4)
    all_ids = [e.id for e in events]

    async with _SSEConnection(app, sid, last_event_id=bad_header) as conn:
        await _read_preamble(conn)
        replayed = await conn.read_frames(len(all_ids))

    assert [f["id"] for f in replayed] == all_ids  # everything, from the start
    assert all(f["data"]["replay"] is True for f in replayed)


async def test_absent_last_event_id_full_replay(tmp_path: Any) -> None:
    """No header at all → same full replay from 0 (default branch)."""

    app, sid = _make_app_with_session(tmp_path)
    events = _seed_history(app, sid, 3)
    all_ids = [e.id for e in events]

    async with _SSEConnection(app, sid, last_event_id=None) as conn:
        await _read_preamble(conn)
        replayed = await conn.read_frames(len(all_ids))

    assert [f["id"] for f in replayed] == all_ids
    assert all(f["data"]["replay"] is True for f in replayed)


# ---- (c) beyond-head ------------------------------------------------------


async def test_beyond_head_replays_nothing(tmp_path: Any) -> None:
    """Last-Event-ID far past the head → no stale replay at all.

    A client claiming an id beyond everything in this session's history
    must not have old events re-sent to it (no dupes). We assert the
    stream settles at the preamble with nothing behind it.
    """

    app, sid = _make_app_with_session(tmp_path)
    events = _seed_history(app, sid, 3)
    beyond = events[-1].id + 10_000

    async with _SSEConnection(app, sid, last_event_id=str(beyond)) as conn:
        await _read_preamble(conn)
        # No history clears the beyond-head high-water mark.
        await conn.assert_no_frame()


async def test_head_resume_replays_nothing_but_keeps_live(tmp_path: Any) -> None:
    """Last-Event-ID == head → no stale replay, fresh live event delivered.

    A well-behaved client's Last-Event-ID is the highest id it has seen,
    the session head. Resume must re-send nothing (no dupes) yet keep
    streaming events published after the reconnect. Because ids are a
    monotonic global counter, the head is the maximal 'client holds the
    whole timeline' mark for which the *next* published event still
    clears the route's ``id <= last_event_id`` live filter.
    """

    app, sid = _make_app_with_session(tmp_path)
    events = _seed_history(app, sid, 3)
    head = events[-1].id

    async with _SSEConnection(app, sid, last_event_id=str(head)) as conn:
        await _read_preamble(conn)
        # Nothing in history is strictly newer than the head.
        await conn.assert_no_frame()

        await _wait_subscribed(app, sid)
        live = Event(type="message.completed", session_id=sid, payload={"k": "v"})
        app.state.bus.publish(live)

        frame = await conn.read_frame()

    assert frame["id"] == live.id
    assert frame["id"] > head
    assert frame["type"] == "message.completed"
    # Live delivery is not a replay.
    assert "replay" not in frame["data"]


async def test_v3_route_emits_scoped_revisioned_envelopes(tmp_path: Any) -> None:
    """Negotiation reaches the real unbounded SSE route, not only pure projection."""

    app, sid = _make_app_with_session(tmp_path)
    async with _SSEConnection(
        app,
        sid,
        last_event_id=None,
        gact_version="0.3",
        a2ui_version="0.9.1",
    ) as conn:
        connected, snapshot = await conn.read_frames(2)

    assert connected["type"] == "stream.live"
    assert connected["data"]["protocol_version"] == "0.3"
    assert connected["data"]["scope"]["session_id"] == sid
    assert snapshot["type"] == "session.upserted"
    assert snapshot["data"]["entity_revision"] == 0
    assert snapshot["data"]["payload"]["id"] == sid
