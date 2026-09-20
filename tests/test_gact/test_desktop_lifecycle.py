"""Tests for the desktop-facing lifecycle seams (gact/desktop_lifecycle.py).

Split out of ``gact/app.py`` (file-size ratchet, #775/#774): ``serve_foreground``
(``run_server``'s non-reload uvicorn branch) and ``release_runtime_after_drain``
(the lifespan desktop-release step) moved to their own owner module. These
tests pin the two behaviors app.py used to cover inline.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import threading
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from clio_agent.gact import desktop_lifecycle


def _free_port() -> int:
    """Bind to ``:0`` to grab a currently-free high port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_serve_foreground_stamps_uvicorn_server_on_app_state(monkeypatch) -> None:
    """``serve_foreground`` must stamp ``app.state.uvicorn_server`` BEFORE it
    blocks in ``server.run()`` -- the authenticated desktop lifecycle route
    reaches it there to set ``should_exit`` for a cross-platform graceful stop."""

    stamped_at_run: list[object] = []

    def _fake_run(self: uvicorn.Server) -> None:
        # Assert the stamp already happened by the time run() is entered, not
        # merely after serve_foreground returns.
        stamped_at_run.append(app.state.uvicorn_server)

    monkeypatch.setattr(uvicorn.Server, "run", _fake_run)

    app = FastAPI()
    desktop_lifecycle.serve_foreground(app, host="127.0.0.1", port=8123)

    server = app.state.uvicorn_server
    assert isinstance(server, uvicorn.Server)
    assert stamped_at_run == [server]
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8123
    # The connection-grace bound that keeps an always-open desktop SSE stream
    # from blocking the lifespan shutdown forever (see the real-server test
    # below).
    assert server.config.timeout_graceful_shutdown == desktop_lifecycle._DESKTOP_GRACEFUL_TIMEOUT_S


def test_serve_foreground_lifespan_shutdown_runs_with_an_open_sse_stream() -> None:
    """Regression: uvicorn's ``Server.shutdown()`` waits for in-flight HTTP
    connections to close BEFORE running the ASGI lifespan shutdown. The
    desktop WebView always holds an open per-session SSE stream
    (``gact/routes/misc.py``) that never closes on its own, so left at
    uvicorn's default (wait forever) that wait never ends: the lifespan
    teardown (turn drain + ``release_runtime_after_drain`` + atexit) never
    runs, and the desktop supervisor force-kills the process at 30s, leaking
    the shared clio-core daemon. ``serve_foreground``'s
    ``timeout_graceful_shutdown`` must bound that wait so the lifespan
    shutdown runs anyway.

    Runs a REAL ``uvicorn.Server`` (built through ``serve_foreground``'s own
    config path) on a free port, in a thread, with one open streaming
    response -- the same shape as the always-open desktop SSE endpoint.
    """

    shutdown_ran = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        shutdown_ran.set()

    app = FastAPI(lifespan=lifespan)

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen():
            while True:
                yield "event: server.heartbeat\ndata: {}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    port = _free_port()
    server_thread = threading.Thread(
        target=desktop_lifecycle.serve_foreground,
        args=(app,),
        kwargs={"host": "127.0.0.1", "port": port},
        daemon=True,
    )
    server_thread.start()

    server: uvicorn.Server | None = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        server = getattr(app.state, "uvicorn_server", None)
        if server is not None and server.started:
            break
        time.sleep(0.05)
    assert server is not None and server.started, "server never started within 5s"

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/stream")
        resp = conn.getresponse()
        resp.read(1)  # block until the stream is actually flowing

        server.should_exit = True

        assert shutdown_ran.wait(timeout=5.0), (
            "lifespan shutdown never ran within 5s with an open SSE stream -- "
            "the desktop supervisor would force-kill and leak clio-core"
        )

        server_thread.join(timeout=5.0)
        assert not server_thread.is_alive()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_release_runtime_after_drain_releases_owned_runtime_when_flag_unset(
    monkeypatch,
) -> None:
    """A normal CLI/server shutdown releases ARC after the same safe turn drain."""

    app = FastAPI()
    monkeypatch.setattr(desktop_lifecycle, "_app_owns_runtime_client", lambda _app: True)
    released: list[bool] = []
    monkeypatch.setattr(
        "clio_agent.arc.storage.release_runtime_client",
        lambda: released.append(True),
    )

    result = await desktop_lifecycle.release_runtime_after_drain(app)

    assert result == "released"
    assert released == [True]


@pytest.mark.asyncio
async def test_release_runtime_after_drain_preserves_unowned_runtime(monkeypatch) -> None:
    """An app without a CTE-backed ARC cannot release another in-process owner."""

    app = FastAPI()
    released: list[bool] = []
    monkeypatch.setattr(
        "clio_agent.arc.storage.release_runtime_client",
        lambda: released.append(True),
    )

    result = await desktop_lifecycle.release_runtime_after_drain(app)

    assert result == "not_owned"
    assert released == []


def test_app_owns_runtime_client_only_for_cte_backed_arc() -> None:
    """Ownership follows the app's actual store, not a process-global attachment."""

    from types import SimpleNamespace

    from clio_agent.arc.storage import ClioCoreStore

    app = FastAPI()
    app.state.arc = SimpleNamespace(_store=object())
    assert desktop_lifecycle._app_owns_runtime_client(app) is False

    app.state.arc = SimpleNamespace(_store=object.__new__(ClioCoreStore))
    assert desktop_lifecycle._app_owns_runtime_client(app) is True


def test_terminate_process_after_cleanup_is_desktop_only(monkeypatch) -> None:
    app = FastAPI()
    exits: list[int] = []
    monkeypatch.setattr(desktop_lifecycle.os, "_exit", exits.append)

    assert desktop_lifecycle.terminate_process_after_cleanup(app) == "not_desktop"
    assert exits == []

    app.state.desktop_shutdown_requested = True
    desktop_lifecycle.terminate_process_after_cleanup(app)
    assert exits == [0]
