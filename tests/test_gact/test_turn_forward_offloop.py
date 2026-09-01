"""Cold blueprint/tool preparation must not freeze the GACT event loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from clio_agent.gact.turn_forward import _run_turn_setup_off_loop


def test_blocking_turn_setup_runs_off_the_event_loop() -> None:
    started = threading.Event()
    release = threading.Event()
    state = SimpleNamespace(
        sid="sess_parent",
        app=SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(get=lambda _sid: None))),
    )

    def blocking_setup() -> str:
        started.set()
        assert release.wait(timeout=2.0)
        return "ready"

    async def exercise() -> None:
        task = asyncio.create_task(_run_turn_setup_off_loop(state, blocking_setup))
        while not started.is_set():
            await asyncio.sleep(0)

        # If setup ran inline, this coroutine could not resume to make either
        # assertion until the blocking operation had already returned.
        assert not task.done()
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        assert await task == "ready"

    asyncio.run(exercise())
