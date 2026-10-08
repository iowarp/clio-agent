"""Real Streamable HTTP proof of task wait responsiveness and cancellation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clio_agent.tools.mcp_task_records import TaskRecord, task_record_store
from tests.test_tools import test_mcp_tasks_conformance as conformance

# Reuse the real server and isolated task store fixtures, not a scripted session.
backend = conformance.backend
_isolated_store = conformance._isolated_store


async def _working_record() -> TaskRecord:
    """Wait for the client to durably record the live task before testing it."""
    async with asyncio.timeout(5):
        while True:
            record = next(
                (row for row in task_record_store().list() if row.status == "working"), None
            )
            if record is not None:
                return record
            await asyncio.sleep(0.01)


async def test_task_wait_keeps_the_client_responsive(backend: Any) -> None:
    """Waiting for a background task must not monopolize the client or event loop."""
    async with conformance._client(backend.url) as client:
        await client.list_tools()
        waiting = asyncio.create_task(client.call_tool("slow", {"seconds": 1.5}))
        try:
            record = await _working_record()
            assert not waiting.done()
            tools = await asyncio.wait_for(client.list_tools(), timeout=1)
            assert any(tool.name == "crunch" for tool in tools)
            assert not waiting.done(), "an unrelated request waited for the long task"
            result = await asyncio.wait_for(waiting, timeout=5)
            assert result.content[0].text == "slow-done"
            settled = task_record_store().get(record.key)
            assert settled is not None and settled.status == "completed"
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_cancelling_foreground_wait_cancels_the_real_backend_task(backend: Any) -> None:
    """Stop a transparent await without leaving phantom working activity on the server."""
    async with conformance._client(backend.url) as client:
        waiting = asyncio.create_task(client.call_tool("slow", {"seconds": 5}))
        try:
            record = await _working_record()
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, timeout=5)
            settled = task_record_store().get(record.key)
            assert settled is not None and settled.status == "cancelled"
            cancels = [
                (method, name)
                for method, name in backend.capture.task_rpcs()
                if method == "tasks/cancel"
            ]
            assert cancels == [("tasks/cancel", record.task_id)]
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
