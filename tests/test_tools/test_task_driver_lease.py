"""A driver cannot continue polling after losing its exclusive owner identity."""

import asyncio
import time
from dataclasses import replace
from typing import Any

import pytest

from clio_agent.errors import ToolError
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskLease,
    TaskRecord,
)
from clio_agent.tools.mcp_tasks import drive_task_to_terminal, resume_task
from clio_agent.tools.task_driver_lease import drive_with_lease
from tests.test_tools.test_mcp_tasks import ScriptedSession, _task_payload


@pytest.mark.asyncio
async def test_custody_loss_stops_pending_operation_and_preserves_replacement_owner() -> None:
    """A renewal refusal cancels the pending RPC before any later poll can run."""
    key = TaskKey("backend", "conversation", "accepted")
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, status="working"))
    lease = TaskLease(store, key, owner="original", ttl_seconds=0.06)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def pending() -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    with lease:
        driver = asyncio.create_task(drive_with_lease(pending(), lease))
        await asyncio.wait_for(started.wait(), 5)
        row = store.get(key)
        assert row is not None
        store.put(replace(row, lease_owner="replacement", lease_expires_at=time.time() + 60))
        with pytest.raises(ToolError) as refused:
            await asyncio.wait_for(driver, 5)
        assert refused.value.details["reason"] == "mcp_task_lease_lost"
        assert stopped.is_set()
    retained = store.get(key)
    assert retained is not None and retained.lease_owner == "replacement"
    assert retained.status == "working"


@pytest.mark.asyncio
async def test_long_rpc_renews_driver_lease_and_refuses_concurrent_resume() -> None:
    """A pending RPC cannot expose an expired lease to a second poll/answer owner."""
    import time

    store = InMemoryTaskRecordStore()
    key = TaskKey("backend", "conversation", "long-accepted-work")
    store.put(TaskRecord(key=key, status="working"))
    started, release = asyncio.Event(), asyncio.Event()

    class HeldSession(ScriptedSession):
        async def send_request(
            self, request: Any, result_type: Any, request_read_timeout_seconds: float | None = None
        ) -> Any:
            started.set()
            await release.wait()
            return await super().send_request(request, result_type, request_read_timeout_seconds)

    lease = TaskLease(store, key, ttl_seconds=0.15, owner="first")
    lease.acquire()
    initial = store.get(key)
    assert initial is not None and initial.lease_expires_at is not None
    initial_expiry = initial.lease_expires_at
    first: Any = HeldSession([_task_payload(key.task_id, "completed", result={})])
    driver = asyncio.create_task(drive_task_to_terminal(first, key, store=store, lease=lease))
    try:
        await asyncio.wait_for(started.wait(), 5)
        while time.time() <= initial_expiry + 0.1:
            await asyncio.sleep(0.01)
        second: Any = ScriptedSession([_task_payload(key.task_id, "completed", result={})])
        with pytest.raises(ToolError) as refused:
            await resume_task(second, key, store=store)
        assert refused.value.details["reason"] == "mcp_task_lease_held"
        assert not second.requests
        current = store.get(key)
        assert current is not None and current.lease_owner == "first"
        release.set()
        assert (await asyncio.wait_for(driver, 5)).status == "completed"
    finally:
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        lease.release()
    current = store.get(key)
    assert current is not None and current.lease_owner is None
