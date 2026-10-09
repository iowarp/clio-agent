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
        await asyncio.sleep(lease.renewal_interval)
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
