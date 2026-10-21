"""Session ownership and physical retirement through the production turn runner."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from threading import Thread
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.turn_runner import TurnRunner, session_busy_error_payload


@pytest.mark.asyncio
async def test_running_sync_turn_does_not_poll_worker_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_alive = threading.Thread.is_alive
    probes = 0
    requested = True

    def alive(thread: threading.Thread) -> bool:
        nonlocal probes
        if thread.name == "clio-session-primary":
            probes += 1
        return original_alive(thread)

    monkeypatch.setattr(threading.Thread, "is_alive", alive)

    def work() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "Test worker release was not delivered"

    async def segment() -> None:
        await runner.run_sync("primary", work)

    async def admit(sid: str) -> tuple[str, Coroutine[Any, Any, Any]] | None:
        nonlocal requested
        assert sid == "primary"
        if not requested:
            return None
        requested = False
        return "turn", segment()

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(loop)
    try:
        runner.signal("primary")
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.04)
        assert probes == 0
        assert runner.busy("primary")
        handle = runner.handle("primary")
        assert handle is not None and handle.turn_id == "turn"
        assert session_busy_error_payload(runner, "primary") is not None
    finally:
        release.set()
        await asyncio.wait_for(runner.drain(), 2)
    assert not runner.busy("primary")
    assert session_busy_error_payload(runner, "primary") is None


@pytest.mark.asyncio
async def test_stop_holds_ownership_through_actual_thread_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    teardown_entered, teardown_release = asyncio.Event(), threading.Event()
    requested = True
    workers: list[threading.Thread] = []
    retirements: list[bool] = []

    class HeldTeardownThread(Thread):
        """Exercise a real thread whose function has returned but teardown has not."""

        def run(self) -> None:
            try:
                super().run()
            finally:
                loop.call_soon_threadsafe(teardown_entered.set)
                assert teardown_release.wait(5), "Test teardown release was not delivered"

    monkeypatch.setattr("clio_agent.gact.turn_runner.threading.Thread", HeldTeardownThread)

    def work() -> str:
        workers.append(threading.current_thread())
        return "Completed function"

    async def segment() -> None:
        await runner.run_sync("primary", work)

    async def admit(_sid: str) -> tuple[str, Coroutine[Any, Any, Any]] | None:
        nonlocal requested
        if not requested:
            return None
        requested = False

        async def before_release(stopped: bool) -> None:
            assert runner.busy("primary")
            assert all(not worker.is_alive() for worker in workers)
            retirements.append(stopped)

        runner.bind_retirement("primary", "turn", before_release)
        return "turn", segment()

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(loop)
    stop: asyncio.Task[None] | None = None
    try:
        runner.signal("primary")
        await asyncio.wait_for(teardown_entered.wait(), 2)
        stop = asyncio.create_task(runner.stop("primary"))
        await asyncio.sleep(0.02)
        runner.signal("primary")
        assert not stop.done()
        assert runner.busy("primary")
        assert len(workers) == 1 and workers[0].is_alive()
        assert not retirements
        teardown_release.set()
        await asyncio.wait_for(stop, 2)
        assert not workers[0].is_alive()
        assert not runner.busy("primary")
        assert retirements == [True]
    finally:
        teardown_release.set()
        if stop is not None:
            await asyncio.wait_for(stop, 2)
        await asyncio.wait_for(runner.drain(), 2)


@pytest.mark.asyncio
async def test_stop_requested_from_another_event_loop_stops_on_the_runners_loop() -> None:
    """A subtree cancel runs on a tool call's loop; the segment's task lives on the
    runner's. Stop must cross over instead of awaiting a future of another loop."""
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    requested = True

    def work() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait(5)

    async def segment() -> None:
        await runner.run_sync("primary", work)

    async def admit(_sid: str) -> tuple[str, Coroutine[Any, Any, Any]] | None:
        nonlocal requested
        if not requested:
            return None
        requested = False
        return "turn", segment()

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(loop)
    errors: list[BaseException] = []

    def stop_from_another_loop() -> None:
        try:
            asyncio.run(runner.stop("primary"))
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    try:
        runner.signal("primary")
        await asyncio.wait_for(entered.wait(), 2)
        other = Thread(target=stop_from_another_loop)
        other.start()
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(asyncio.to_thread(other.join, 5), 6)
        assert errors == []
        assert not runner.busy("primary")
    finally:
        release.set()
        await asyncio.wait_for(runner.drain(), 2)
