"""``settle_turn_slot`` returns only after the finished turn's terminal publish ran.

Regression for the CI-only ``test_cancel_during_turn_marks_turn_as_cancelled``
flake: ``TurnRunner.busy()`` is ``not task.done()``, which turns False the instant
the turn coroutine returns, while the task's done-callback (slot release plus the
``run_when_released`` terminal ``session.status_changed``) is merely queued on the
loop. The helper used to return on ``busy()`` alone, so a loaded runner let the test
read the bus before that publish. Here the loop is held between the two, exactly
where a slow runner can stall it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from clio_agent.gact.turn_runner import TurnRunner

from .conftest import settle_turn_slot

SID = "sess_barrier"
BACKSTOP_S = 30.0


def test_settle_turn_slot_waits_for_the_release_publish() -> None:
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, name="barrier-loop", daemon=True)
    loop_thread.start()
    runner = TurnRunner({})
    runner.bind_loop(loop)
    published: list[str] = []
    loop_held = threading.Event()
    release_loop = threading.Event()

    def hold_the_loop() -> None:
        loop_held.set()
        assert release_loop.wait(BACKSTOP_S), "the test never released the loop"

    async def turn() -> None:
        runner.run_when_released(SID, lambda: published.append("idle"))
        # Queued BEFORE the task's done-callback (which is queued when this returns),
        # so the loop stalls with the task done and its release publish still pending.
        asyncio.get_running_loop().call_soon(hold_the_loop)

    client = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(turn_runner=runner)))
    try:
        loop.call_soon_threadsafe(lambda: runner.spawn(turn(), sid=SID, turn_id="turn_1"))
        assert loop_held.wait(BACKSTOP_S), "the turn never ran"
        assert not runner.busy(SID), "busy() already reads free here: the task is done"
        assert published == [], "the terminal publish has not run yet"

        settled = threading.Event()
        waiter = threading.Thread(
            target=lambda: (settle_turn_slot(client, SID), settled.set()),  # type: ignore[arg-type]
            daemon=True,
        )
        waiter.start()
        # busy() is False, yet the helper must keep waiting for the queued publish.
        time.sleep(0.3)
        assert not settled.is_set(), "settle_turn_slot returned before the terminal publish"

        release_loop.set()
        assert settled.wait(BACKSTOP_S), "settle_turn_slot never returned"
        assert published == ["idle"]
    finally:
        release_loop.set()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(BACKSTOP_S)
        loop.close()
