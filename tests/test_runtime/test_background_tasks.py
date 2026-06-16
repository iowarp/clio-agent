"""Unit tests for the background task monitor/wait_for primitives (epic #667, #670).

Pure asyncio — no LM, no network. pytest-asyncio is in auto mode.
"""

from __future__ import annotations

import asyncio

import pytest

from clio_agent.runtime.background_tasks import BackgroundTasks, TaskStatus


async def test_spawn_runs_completes_and_buffers_output():
    bt = BackgroundTasks()

    async def work(sink):
        sink.emit("step1")
        sink.emit("step2")
        return 42

    tid = bt.spawn(work, label="w")
    rec = await bt.wait(tid, timeout=1)
    assert rec.status is TaskStatus.COMPLETED
    assert rec.result == 42
    assert bt.poll_output(tid) == ["step1", "step2"]


async def test_failure_is_recorded_not_raised():
    bt = BackgroundTasks()

    async def work(sink):
        raise ValueError("boom")

    tid = bt.spawn(work)
    rec = await bt.wait(tid, timeout=1)
    assert rec.status is TaskStatus.FAILED
    assert "boom" in (rec.error or "")
    assert rec.result is None


async def test_wait_timeout_leaves_task_running():
    bt = BackgroundTasks()

    async def work(sink):
        await asyncio.sleep(5)
        return 1

    tid = bt.spawn(work)
    rec = await bt.wait(tid, timeout=0.05)
    assert rec.status is TaskStatus.RUNNING  # didn't finish within the timeout
    bt.cancel(tid)
    await bt.wait(tid, timeout=1)


async def test_wait_until_predicate_returns_before_completion():
    bt = BackgroundTasks()

    async def work(sink):
        for i in range(6):
            sink.emit(f"line{i}")
            await asyncio.sleep(0.02)
        return "done"

    tid = bt.spawn(work)
    rec = await bt.wait(tid, until=lambda r: len(r.output) >= 3, timeout=2)
    assert len(rec.output) >= 3
    assert rec.status is TaskStatus.RUNNING  # condition hit before the task ended
    final = await bt.wait(tid, timeout=2)
    assert final.status is TaskStatus.COMPLETED


async def test_poll_output_is_incremental():
    bt = BackgroundTasks()

    async def work(sink):
        for i in range(3):
            sink.emit(str(i))
            await asyncio.sleep(0.02)
        return None

    tid = bt.spawn(work)
    await bt.wait(tid, until=lambda r: len(r.output) >= 1, timeout=2)
    first = bt.poll_output(tid)
    await bt.wait(tid, timeout=2)
    rest = bt.poll_output(tid, since=len(first))
    assert first + rest == ["0", "1", "2"]
    assert "0" not in rest  # since= advanced past what we already saw


async def test_cancel_settles_to_cancelled():
    bt = BackgroundTasks()

    async def work(sink):
        await asyncio.sleep(5)

    tid = bt.spawn(work)
    await asyncio.sleep(0.01)  # let it start
    assert bt.cancel(tid) is True
    rec = await bt.wait(tid, timeout=1)
    assert rec.status is TaskStatus.CANCELLED


async def test_on_complete_notifies():
    bt = BackgroundTasks()
    seen: list[TaskStatus] = []

    async def work(sink):
        return "ok"

    tid = bt.spawn(work)
    bt.on_complete(tid, lambda r: seen.append(r.status))
    await bt.wait(tid, timeout=1)
    assert seen == [TaskStatus.COMPLETED]


async def test_on_complete_fires_immediately_when_already_done():
    bt = BackgroundTasks()

    async def work(sink):
        return 1

    tid = bt.spawn(work)
    await bt.wait(tid, timeout=1)
    seen: list[str] = []
    bt.on_complete(tid, lambda r: seen.append(r.id))
    assert seen == [tid]


async def test_unknown_handle_raises():
    bt = BackgroundTasks()
    with pytest.raises(KeyError):
        bt.status("nope")
    with pytest.raises(KeyError):
        await bt.wait("nope", timeout=0.1)


async def test_list_snapshots_redact_result():
    bt = BackgroundTasks()

    async def work(sink):
        sink.emit("x")
        return {"secret": "value"}

    tid = bt.spawn(work)
    await bt.wait(tid, timeout=1)
    rows = bt.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == tid
    assert row["status"] == "completed"
    assert row["output_lines"] == 1
    assert "secret" not in str(row)  # snapshot never carries the raw result
