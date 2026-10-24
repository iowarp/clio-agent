"""A reload waits for complete turns, including dependent child turns."""

from __future__ import annotations

import asyncio

import pytest

from tests.test_gact.runner_records import admit_segment, make_runner


@pytest.mark.asyncio
async def test_reload_drains_parent_and_child_before_new_turn() -> None:
    runner = make_runner()
    entered = asyncio.Event()
    finish = asyncio.Event()
    events: list[str] = []

    async def child() -> None:
        events.append("child")

    async def parent() -> None:
        entered.set()
        await finish.wait()
        await admit_segment(runner, child(), sid="child", turn_id="c")
        events.append("parent finished")

    async def reload() -> None:
        async with runner.revision_gate.change():
            events.append("applied")

    async def later() -> None:
        events.append("new turn")

    first = admit_segment(runner, parent(), sid="parent", turn_id="p")
    await entered.wait()
    change = asyncio.create_task(reload())
    await asyncio.sleep(0)
    next_turn = admit_segment(runner, later(), sid="other", turn_id="n")
    await asyncio.sleep(0)
    assert events == []
    finish.set()
    await asyncio.wait_for(asyncio.gather(first, change, next_turn), timeout=2)
    assert events == ["child", "parent finished", "applied", "new turn"]


@pytest.mark.asyncio
async def test_cancel_queued_turn_never_executes_and_does_not_block_reload() -> None:
    runner = make_runner()
    executed: list[bool] = []

    async def later() -> None:
        executed.append(True)

    async with runner.revision_gate.change():
        task = admit_segment(runner, later(), sid="queued", turn_id="q")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert executed == []
    await admit_segment(runner, later(), sid="next", turn_id="n")
    assert executed == [True]


@pytest.mark.asyncio
async def test_failed_reload_releases_waiting_turns() -> None:
    runner = make_runner()
    with pytest.raises(ValueError, match="invalid"):
        async with runner.revision_gate.change():
            raise ValueError("invalid staged revision")
    await asyncio.wait_for(
        admit_segment(runner, asyncio.sleep(0), sid="next", turn_id="n"), timeout=2
    )


@pytest.mark.asyncio
async def test_a_task_a_turn_started_does_not_keep_the_finished_turn_alive() -> None:
    """Live (#1846): a long-lived task created during a turn copied its context, whose
    strong reference to the turn's task kept every finished turn of the chain alive."""
    import gc
    import weakref

    from clio_agent.gact.turn_revision_gate import TurnRevisionGate

    gate = TurnRevisionGate()
    outlives = asyncio.Event()
    turn_ref: list[weakref.ref[asyncio.Task[None]]] = []
    started: list[asyncio.Task[None]] = []

    async def turn() -> None:
        current = asyncio.current_task()
        assert current is not None
        turn_ref.append(weakref.ref(current))
        started.append(asyncio.create_task(outlives.wait()))  # copies the turn's context

    await asyncio.create_task(gate.run(turn()))
    await asyncio.sleep(0)
    gc.collect()

    assert turn_ref[0]() is None
    assert not started[0].done()
    outlives.set()
    await started[0]
