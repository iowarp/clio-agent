"""A reload waits for complete turns, including dependent child turns."""

from __future__ import annotations

import asyncio

import pytest

from clio_agent.gact.turn_runner import TurnRunner


@pytest.mark.asyncio
async def test_reload_drains_parent_and_child_before_new_turn() -> None:
    runner = TurnRunner({})
    entered = asyncio.Event()
    finish = asyncio.Event()
    events: list[str] = []

    async def child() -> None:
        events.append("child")

    async def parent() -> None:
        entered.set()
        await finish.wait()
        await runner.spawn(child(), sid="child", turn_id="c")
        events.append("parent finished")

    async def reload() -> None:
        async with runner.revision_gate.change():
            events.append("applied")

    async def later() -> None:
        events.append("new turn")

    first = runner.spawn(parent(), sid="parent", turn_id="p")
    await entered.wait()
    change = asyncio.create_task(reload())
    await asyncio.sleep(0)
    next_turn = runner.spawn(later(), sid="other", turn_id="n")
    await asyncio.sleep(0)
    assert events == []
    finish.set()
    await asyncio.wait_for(asyncio.gather(first, change, next_turn), timeout=2)
    assert events == ["child", "parent finished", "applied", "new turn"]


@pytest.mark.asyncio
async def test_cancel_queued_turn_never_executes_and_does_not_block_reload() -> None:
    runner = TurnRunner({})
    executed: list[bool] = []

    async def later() -> None:
        executed.append(True)

    async with runner.revision_gate.change():
        task = runner.spawn(later(), sid="queued", turn_id="q")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert executed == []
    await runner.spawn(later(), sid="next", turn_id="n")
    assert executed == [True]


@pytest.mark.asyncio
async def test_failed_reload_releases_waiting_turns() -> None:
    runner = TurnRunner({})
    with pytest.raises(ValueError, match="invalid"):
        async with runner.revision_gate.change():
            raise ValueError("invalid staged revision")
    await asyncio.wait_for(runner.spawn(asyncio.sleep(0), sid="next", turn_id="n"), timeout=2)
