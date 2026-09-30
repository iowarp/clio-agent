"""The in-process SDK transport runs the app's lifespan, as uvicorn does.

Found in the suite (2026-09-30): the transport never ran the ASGI lifespan, so the
app's shutdown never drained its in-flight turns; closing the transport stopped the
loop under a still-running turn ("Task was destroyed but it is pending!"), which
surfaced later as an unraisable error in whichever test ran next.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tests.test_sdk.conftest import StreamingASGITransport


def test_closing_the_transport_drains_a_running_turn(app: Any) -> None:
    transport = StreamingASGITransport(app)
    runner: Any = app.state.turn_runner

    async def spawn() -> asyncio.Task[object]:
        return runner.spawn(asyncio.sleep(3600), sid="s1", turn_id="t1")

    task = asyncio.run_coroutine_threadsafe(spawn(), transport._loop).result(10)
    assert runner.busy("s1")

    transport.close()

    assert task.done(), "the turn was left pending when the app stopped"
    assert not runner.busy("s1")
