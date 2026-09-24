"""F1: the workspace file-change watcher (gact/workspace_watch.py).

Two layers, mirroring test_sse.py's own house convention (streaming-during-
request behaviour is fragile over TestClient/ASGITransport; drive the owner
object directly and reserve TestClient for the pieces that genuinely need a
real app -- the shutdown lifecycle and the HTTP wiring):

  1. WorkspaceWatchRegistry unit tests -- real filesystem changes through a
     real `watchfiles.awatch`, no mocking of watchfiles itself.
  2. HTTP wiring smoke -- delete-cleans-up and the files-listing response's
     `live_updates` field, both driven through a real TestClient app so the
     route code (routes/workspaces.py) is exercised, not just the registry.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.workspace_watch import (
    EVENT_TYPE,
    UNAVAILABLE_REASON,
    WorkspaceWatchRegistry,
)

_BATCH_WAIT_S = 5.0
_QUIET_WAIT_S = 1.0


def _fake_app(bus: EventBus) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(bus=bus))


def _start_broadcast_reader(
    bus: EventBus, session_id: str
) -> tuple["asyncio.Task[None]", "asyncio.Queue[Event]"]:
    """Subscribe like a real SSE client and forward every non-transient event.

    ``workspace.files.changed`` is published broadcast (session_id="", the same
    convention as lm.provider.changed/failed) so any live subscriber session
    receives it -- subscribing under an ordinary session id here mirrors a real
    client exactly (see events.py's fan-out: session_id="" delivers to every
    currently-subscribed session).
    """

    queue: "asyncio.Queue[Event]" = asyncio.Queue()

    async def _reader() -> None:
        async for ev in bus.subscribe(session_id):
            if ev.transient:
                continue
            await queue.put(ev)

    return asyncio.create_task(_reader()), queue


async def _wait_for_subscriber(bus: EventBus, session_id: str, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while bus.subscriber_count(session_id) == 0:
        if loop.time() > deadline:
            raise AssertionError(f"no subscriber ever registered for {session_id!r}")
        await asyncio.sleep(0.01)


async def _drain(registry: WorkspaceWatchRegistry, reader_task: "asyncio.Task[None]") -> None:
    await registry.shutdown()
    reader_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reader_task


async def test_watch_batches_real_changes_including_dot_clio_inputs(tmp_path: Path) -> None:
    """A create under root + a create under .clio/inputs land in ONE batched event.

    ``.clio`` MUST be reported (uploads materialize into ``.clio/inputs``) --
    unlike node_modules below, it is not in the listing's skip set.
    """

    root = tmp_path
    (root / ".clio" / "inputs").mkdir(parents=True)
    bus = EventBus()
    app = _fake_app(bus)
    registry = WorkspaceWatchRegistry()
    reader_task, queue = _start_broadcast_reader(bus, "s1")
    try:
        await _wait_for_subscriber(bus, "s1")
        await registry.acquire(app, "ws1", root)
        assert registry.status("ws1") == {"active": True}

        (root / "notes.txt").write_text("hello", encoding="utf-8")
        (root / ".clio" / "inputs" / "upload.pdf").write_bytes(b"pdf-bytes")

        first = await asyncio.wait_for(queue.get(), timeout=_BATCH_WAIT_S)
        assert first.type == EVENT_TYPE
        assert first.payload["workspace_id"] == "ws1"
        assert set(first.payload["paths"]) == {"notes.txt", ".clio/inputs/upload.pdf"}
        assert first.payload["truncated"] is False

        # Both changes batched into the ONE event above -- nothing else trickles in.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=_QUIET_WAIT_S)
    finally:
        await _drain(registry, reader_task)


async def test_watch_skips_node_modules_but_keeps_reporting_real_files(tmp_path: Path) -> None:
    """Changes confined to node_modules are never reported.

    Proven against a false negative (a dead watcher would also report nothing):
    a real sibling change made right after still arrives.
    """

    root = tmp_path
    (root / "node_modules" / "pkg").mkdir(parents=True)
    bus = EventBus()
    app = _fake_app(bus)
    registry = WorkspaceWatchRegistry()
    reader_task, queue = _start_broadcast_reader(bus, "s1")
    try:
        await _wait_for_subscriber(bus, "s1")
        await registry.acquire(app, "ws1", root)

        (root / "node_modules" / "pkg" / "index.js").write_text("ignored", encoding="utf-8")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=_QUIET_WAIT_S)

        (root / "kept.txt").write_text("still watching", encoding="utf-8")
        reported = await asyncio.wait_for(queue.get(), timeout=_BATCH_WAIT_S)
        assert reported.payload["paths"] == ["kept.txt"]
    finally:
        await _drain(registry, reader_task)


async def test_watch_start_failure_yields_typed_reason(tmp_path: Path) -> None:
    """A watcher that cannot start reports the typed reason, not a crash.

    A missing root is a real, portable way to make ``watchfiles`` itself raise
    (verified against the installed library: ``RustNotify`` construction fails
    synchronously with ``FileNotFoundError`` on the first iteration) without
    mocking watchfiles -- permission-denial is not reliably reproducible across
    CI's OSes, but a vanished/never-existed root is a real failure mode too
    (materialize_workspace_root races, a mount going away).
    """

    bus = EventBus()
    app = _fake_app(bus)
    registry = WorkspaceWatchRegistry()
    missing_root = tmp_path / "does-not-exist"

    await registry.acquire(app, "ws_missing", missing_root)
    try:
        status = registry.status("ws_missing")
        assert status["active"] is False
        assert status["reason"] == UNAVAILABLE_REASON
        assert status["detail"]
    finally:
        await registry.shutdown()


def test_status_for_an_idle_workspace_is_not_a_fabricated_failure() -> None:
    """No subscriber has ever acquired this workspace -- honestly idle, not "unavailable"."""

    registry = WorkspaceWatchRegistry()
    assert registry.status("never-touched") == {"active": False}


async def test_release_at_zero_refcount_stops_the_task_immediately(tmp_path: Path) -> None:
    root = tmp_path
    bus = EventBus()
    app = _fake_app(bus)
    registry = WorkspaceWatchRegistry()

    await registry.acquire(app, "ws1", root)
    handle = registry._handles["ws1"]
    task = handle.task
    assert task is not None
    assert not task.done()

    await registry.release(app, "ws1")
    assert task.done()
    assert registry.status("ws1") == {"active": False}


async def test_two_subscribers_share_one_watcher(tmp_path: Path) -> None:
    """One watcher per root, whatever the number of sessions (owner spec)."""

    root = tmp_path
    bus = EventBus()
    app = _fake_app(bus)
    registry = WorkspaceWatchRegistry()

    await registry.acquire(app, "ws1", root)
    task_after_first = registry._handles["ws1"].task
    await registry.acquire(app, "ws1", root)
    task_after_second = registry._handles["ws1"].task
    assert task_after_first is task_after_second

    # The first session's disconnect must not stop the watcher the second
    # session still depends on.
    await registry.release(app, "ws1")
    assert not task_after_first.done()
    assert registry.status("ws1") == {"active": True}

    await registry.release(app, "ws1")
    assert task_after_first.done()


def _http_client(tmp_path: Path):
    return build_app(sessions_path=tmp_path / "s.json")


def test_files_listing_reports_idle_live_updates_by_default(tmp_path: Path) -> None:
    app = _http_client(tmp_path)
    with TestClient(app) as client:
        body = client.get("/v1/workspaces/ws_default/files").json()
        assert body["live_updates"] == {"active": False}


def test_files_listing_surfaces_watch_unavailable_reason(tmp_path: Path) -> None:
    app = _http_client(tmp_path)
    with TestClient(app) as client:
        registry = app.state.workspace_watch
        # Same registry API production's SSE hook drives (routes/misc.py); the
        # failure trigger itself is exercised in isolation above.
        client.portal.call(registry.acquire, app, "ws_default", tmp_path / "missing-root")
        assert registry.status("ws_default")["reason"] == UNAVAILABLE_REASON

        body = client.get("/v1/workspaces/ws_default/files").json()
        assert body["live_updates"]["active"] is False
        assert body["live_updates"]["reason"] == UNAVAILABLE_REASON
        client.portal.call(registry.shutdown)


def test_delete_workspace_stops_its_watcher(tmp_path: Path) -> None:
    app = _http_client(tmp_path)
    workspace_root = tmp_path / "proj"
    workspace_root.mkdir()
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces", json={"name": "proj", "root_path": str(workspace_root)}
        ).json()
        wid = created["id"]
        registry = app.state.workspace_watch
        client.portal.call(registry.acquire, app, wid, workspace_root)
        assert registry.status(wid)["active"] is True

        response = client.delete(f"/v1/workspaces/{wid}")
        assert response.status_code == 204
        assert registry.status(wid) == {"active": False}


def test_watcher_task_does_not_leak_past_server_shutdown(tmp_path: Path) -> None:
    """The registry's own asyncio task must not survive the ASGI lifespan.

    Entering the TestClient as a context manager is load-bearing here (per the
    F1 spec): only then does `_lifespan`'s teardown -- and therefore
    `workspace_watch.shutdown()` -- actually run.
    """

    root = tmp_path / "root"
    root.mkdir()
    app = _http_client(tmp_path)
    with TestClient(app) as client:
        registry = app.state.workspace_watch
        client.portal.call(registry.acquire, app, "ws_test", root)
        handle = registry._handles["ws_test"]
        task = handle.task
        assert task is not None
        assert not task.done()

    # Outside the `with` block: ASGI shutdown has run.
    assert task.done()
    assert registry._handles == {}


def _fake_get_request(path: str) -> Request:
    """A minimal, read-only Starlette Request -- no ASGI transport involved.

    ``GET /v1/sessions/{sid}/events`` never reads a body, and
    ``project_for_request`` falls back to GACT_V2 when ``request.state`` has no
    ``protocol_version`` (unset here), so a bare scope is enough.
    """

    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def _route_endpoint(app: FastAPI, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route not registered: {path}")


async def test_sse_subscribe_acquires_and_disconnect_releases_the_watcher(tmp_path: Path) -> None:
    """The owner spec's core lifecycle rule, exercised through the real route.

    Deliberately NOT driven through TestClient/ASGITransport: this exact
    endpoint's own docstring precedent (test_sse.py) documents that TestClient
    deadlocks on this unbounded StreamingResponse under sync iteration. Calling
    the route function directly and stepping its async generator sidesteps
    that transport entirely while still exercising the real route code (the
    acquire before `bus.subscribe`, the release in `finally`).
    """

    root = tmp_path / "proj"
    root.mkdir()
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(root))
    session = app.state.sessions.create(workspace_id="ws_default")
    registry = app.state.workspace_watch

    endpoint = _route_endpoint(app, "/v1/sessions/{sid}/events")
    request = _fake_get_request(f"/v1/sessions/{session.id}/events")
    response = await endpoint(session.id, request)
    body_iterator = response.body_iterator

    await asyncio.wait_for(body_iterator.__anext__(), timeout=5.0)  # server.connected
    await asyncio.wait_for(body_iterator.__anext__(), timeout=5.0)  # session.snapshot

    # The generator resumes past the F1 acquire() call (which blocks on the
    # watcher's own ready signal) only once a THIRD frame is requested; the bus
    # subscription it makes right after has nothing published to it yet, so
    # this call parks on the live queue rather than returning -- poll the
    # registry from the outside while it is in flight.
    pending = asyncio.ensure_future(body_iterator.__anext__())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while registry.status("ws_default") != {"active": True}:
            if loop.time() > deadline:
                raise AssertionError(
                    "workspace watcher never became active for the live subscriber"
                )
            await asyncio.sleep(0.01)
    finally:
        # Cancelling the parked frame == the client disconnecting -- the same
        # `except asyncio.CancelledError: pass` path a real ASGI disconnect
        # takes, whose `finally` releases the watcher synchronously before
        # this awaited cancellation returns.
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending

    assert registry.status("ws_default") == {"active": False}
    await registry.shutdown()
