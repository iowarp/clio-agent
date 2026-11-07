"""Session-exclusive admission and physical worker retirement contracts."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.turn_runner import TurnAdmissionError, TurnRunner, session_busy_error_payload


async def wait_until(predicate: Any) -> None:
    """Bound only this test's diagnostic wait; the product has no drain cutoff."""
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_admission_reserves_session_before_awaiting_preflight() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def admit(sid: str) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return None

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(asyncio.get_running_loop())
    runner.signal("session")
    await entered.wait()
    assert runner.busy("session")
    for _ in range(100):
        runner.signal("session")
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    await wait_until(lambda: not runner.busy("session"))
    await runner.drain()
    assert not runner.busy("session")


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(5))
async def test_closing_arrival_is_admitted_without_another_external_signal(iteration: int) -> None:
    """Busy-to-idle handoff retains a producer's arrival and serializes the next segment."""
    pending = ["first"]
    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[str] = []
    simultaneous = 0

    async def segment(value: str) -> None:
        nonlocal simultaneous
        simultaneous += 1
        assert simultaneous == 1
        try:
            seen.append(value)
            entered.set()
            if value == "first":
                await release.wait()
        finally:
            simultaneous -= 1

    async def admit(sid: str) -> tuple[str, Coroutine[Any, Any, Any]] | None:
        if not pending:
            return None
        value = pending.pop(0)
        return value, segment(value)

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(asyncio.get_running_loop())
    runner.signal("session")
    await entered.wait()
    pending.append("closing")
    runner.signal("session")
    release.set()
    await wait_until(lambda: seen == ["first", "closing"] and not runner.busy("session"))
    await runner.drain()


@pytest.mark.asyncio
async def test_policy_veto_retains_work_without_busy_spinning() -> None:
    calls, allowed = 0, False
    pending = ["retained"]
    seen: list[str] = []

    async def segment() -> None:
        seen.append(pending.pop())

    async def admit(sid: str) -> tuple[str, Coroutine[Any, Any, Any]] | None:
        nonlocal calls
        calls += 1
        return ("turn", segment()) if allowed and pending else None

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(asyncio.get_running_loop())
    runner.signal("session")
    await wait_until(lambda: calls == 1 and not runner.busy("session"))
    await asyncio.sleep(0.02)
    assert calls == 1 and pending == ["retained"]
    allowed = True
    runner.signal("session")
    await wait_until(lambda: seen == ["retained"] and not runner.busy("session"))
    await runner.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True], ids=["return", "raise"])
async def test_stop_and_shutdown_wait_for_actual_worker_exit(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    provider_releases: list[str] = []
    monkeypatch.setattr(
        "clio_agent.providers.session_lifecycle.release_session_resources",
        lambda sid: provider_releases.append(sid),
    )
    offered = False
    runner: TurnRunner

    def work() -> str:
        entered.set()
        try:
            assert release.wait(5), "Test controller did not release its worker"
            if failure:
                raise RuntimeError("owned model failure")
            return "settled"
        finally:
            exited.set()

    async def segment() -> None:
        await runner.run_sync("session", work)

    async def admit(sid: str) -> tuple[str, Coroutine[Any, Any, Any]] | None:
        nonlocal offered
        if offered:
            return None
        offered = True
        return "turn", segment()

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(asyncio.get_running_loop())
    runner.signal("session")
    await wait_until(entered.is_set)
    callbacks: list[bool] = []
    runner.run_when_released("session", lambda: callbacks.append(exited.is_set()))
    stop = asyncio.create_task(runner.stop("session"))
    try:
        await wait_until(lambda: runner.stop_requested("session"))
        assert runner.busy("session") and not stop.done()
        assert provider_releases == [] and callbacks == []
        assert session_busy_error_payload(runner, "session") is not None
    finally:
        release.set()
        await stop
        await runner.drain()
    assert exited.is_set() and not runner.busy("session")
    assert provider_releases == ["session"] and callbacks == [True]


@pytest.mark.asyncio
async def test_shutdown_closes_admission_before_pending_signal_runs() -> None:
    admitted: list[str] = []

    async def admit(sid: str) -> None:
        admitted.append(sid)

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(asyncio.get_running_loop())
    runner.close_admission()
    runner.signal("session")
    await runner.drain()
    assert admitted == [] and not runner.busy("session")


@pytest.mark.asyncio
async def test_unreserved_sync_execution_is_refused() -> None:
    async def admit(sid: str) -> None:
        return None

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(asyncio.get_running_loop())
    with pytest.raises(TurnAdmissionError):
        await runner.run_sync("unreserved", lambda: "must not run")
    await runner.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "released"), [(False, []), (True, ["session"])])
async def test_a_clean_turn_keeps_the_provider_connection(
    monkeypatch: pytest.MonkeyPatch, failure: bool, released: list[str]
) -> None:
    """A clean turn end keeps the pooled connection (the next turn sends only new
    messages); a failed segment releases it. While the turn runs it is marked active.

    SABOTAGE: release unconditionally again and the clean case records a release.
    """
    from clio_agent.providers import turn_activity

    provider_releases: list[str] = []
    monkeypatch.setattr(
        "clio_agent.providers.session_lifecycle.release_session_resources",
        lambda sid: provider_releases.append(sid),
    )
    seen_active: list[bool] = []
    offered = False
    runner: TurnRunner

    def work() -> str:
        seen_active.append(turn_activity.active("session"))
        if failure:
            raise RuntimeError("owned model failure")
        return "settled"

    async def segment() -> None:
        await runner.run_sync("session", work)

    async def admit(sid: str) -> tuple[str, Coroutine[Any, Any, Any]] | None:
        nonlocal offered
        if offered:
            return None
        offered = True
        return "turn", segment()

    runner = TurnRunner(SimpleNamespace(state=SimpleNamespace()), admit)
    runner.bind_loop(asyncio.get_running_loop())
    runner.signal("session")
    await wait_until(lambda: seen_active and not runner.busy("session"))
    await runner.drain()
    assert seen_active == [True] and not turn_activity.active("session")
    assert provider_releases == released
