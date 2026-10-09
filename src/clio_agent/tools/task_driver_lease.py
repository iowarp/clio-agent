"""Keep existing task-driver custody live across unbounded owner execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from clio_agent.tools.mcp_task_records import TaskLease

Result = TypeVar("Result")


async def _renew(lease: TaskLease) -> None:
    """Renew the same claim until its driver ends or custody is lost."""
    while True:
        # Custody uses its own loop timer. Poll cadence may be injected or removed
        # independently; it must never turn this worker into a non-yielding loop.
        wake = asyncio.Event()
        timer = asyncio.get_running_loop().call_later(lease.renewal_interval, wake.set)
        try:
            await wake.wait()
        finally:
            timer.cancel()
        lease.renew()


async def drive_with_lease(operation: Awaitable[Result], lease: TaskLease) -> Result:
    """Stop driving on custody loss and retire the renewal worker on every exit."""
    driver = asyncio.ensure_future(operation)
    renewal = asyncio.create_task(_renew(lease))
    try:
        done, _ = await asyncio.wait({driver, renewal}, return_when=asyncio.FIRST_COMPLETED)
        if renewal in done:
            await renewal
        return await driver
    finally:
        for worker in (driver, renewal):
            worker.cancel()
        await asyncio.gather(driver, renewal, return_exceptions=True)
