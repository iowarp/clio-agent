"""The in-process SDK transport runs the app's lifespan, as uvicorn does.

Found in the suite (2026-09-30): the transport never ran the ASGI lifespan, so the
app's shutdown never drained its in-flight turns; closing the transport stopped the
loop under a still-running turn ("Task was destroyed but it is pending!"), which
surfaced later as an unraisable error in whichever test ran next.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from tests.test_client.conftest import StreamingASGITransport


def test_closing_the_transport_drains_a_running_turn(app: Any) -> None:
    transport = StreamingASGITransport(app)
    runner: Any = app.state.turn_runner
    started = threading.Event()
    retired = threading.Event()
    tasks: list[asyncio.Task[Any]] = []

    async def hold(_segment: Any) -> Any:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()

    from clio_agent.gact.session_input import accept_session_input

    app.state.conversations._run = hold
    sid = app.state.sessions.create(workspace_id="ws_default", title="Drain").id
    try:
        accept_session_input(app, sid, "Hold this owned turn")
        assert started.wait(10)
        assert runner.busy(sid)
        assert len(tasks) == 1
    finally:
        transport.close()

    assert tasks[0].done(), "the turn was left pending when the app stopped"
    assert retired.is_set()
    assert not runner.busy(sid)
