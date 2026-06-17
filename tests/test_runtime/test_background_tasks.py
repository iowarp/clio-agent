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


async def test_background_tasks_high_concurrency_stress():
    """500 concurrent tasks with mixed outcomes (complete / slow / fail / cancel),
    on_complete on every one, then prune — asserts correct terminal states, every
    on_complete fires exactly once, and the registry fully evicts (no leak)."""
    bt = BackgroundTasks()
    n = 500
    fired = {"n": 0}
    handles: list[tuple[int, str]] = []
    for i in range(n):
        kind = i % 4

        async def work(sink, x=i, k=kind):
            if k == 0:
                sink.emit(f"line{x}")
                return x
            if k == 1:
                await asyncio.sleep(0.05)
                return x
            if k == 2:
                raise RuntimeError(f"boom{x}")
            await asyncio.sleep(3)  # long -> will be cancelled
            return x

        h = bt.spawn(work, label=f"t{i}")
        bt.on_complete(h, lambda rec: fired.__setitem__("n", fired["n"] + 1))
        handles.append((kind, h))

    for kind, h in handles:
        if kind == 3:
            bt.cancel(h)

    counts = {TaskStatus.COMPLETED: 0, TaskStatus.FAILED: 0, TaskStatus.CANCELLED: 0}
    for _, h in handles:
        rec = await bt.wait(h, timeout=15)
        counts[rec.status] = counts.get(rec.status, 0) + 1

    assert counts[TaskStatus.COMPLETED] == n // 2  # kinds 0 + 1
    assert counts[TaskStatus.FAILED] == n // 4      # kind 2
    assert counts[TaskStatus.CANCELLED] == n // 4   # kind 3
    assert fired["n"] == n                          # every on_complete fired exactly once
    assert bt.prune() == n                          # all terminal -> fully evicted
    assert bt.list() == []                          # registry empty (no leak)


async def test_spawn_command_runs_streams_output_and_exit_code():
    from clio_agent.runtime.background_tasks import spawn_command

    bt = BackgroundTasks()
    tid = spawn_command(bt, "echo hello; echo world")
    rec = await bt.wait(tid, timeout=10)
    assert rec.status is TaskStatus.COMPLETED
    assert rec.result["exit_code"] == 0
    assert bt.poll_output(tid) == ["hello", "world"]  # streamed to the handle


async def test_spawn_command_nonzero_exit_is_captured():
    from clio_agent.runtime.background_tasks import spawn_command

    bt = BackgroundTasks()
    tid = spawn_command(bt, "exit 3")
    rec = await bt.wait(tid, timeout=10)
    assert rec.status is TaskStatus.COMPLETED  # the task completed; the command failed
    assert rec.result["exit_code"] == 3


async def test_unknown_handle_raises():
    bt = BackgroundTasks()
    with pytest.raises(KeyError):
        bt.status("nope")
    with pytest.raises(KeyError):
        await bt.wait("nope", timeout=0.1)


async def test_prune_evicts_terminal_records_keeps_running():
    bt = BackgroundTasks()

    async def quick(sink):
        return 1

    async def slow(sink):
        await asyncio.sleep(5)

    t1, t2 = bt.spawn(quick), bt.spawn(quick)
    await bt.wait(t1, timeout=1)
    await bt.wait(t2, timeout=1)
    t3 = bt.spawn(slow)
    await asyncio.sleep(0.01)

    assert bt.prune() == 2  # only the two completed are evicted
    assert bt.get(t1) is None and bt.get(t2) is None
    assert bt.get(t3) is not None  # running task kept

    bt.cancel(t3)
    await bt.wait(t3, timeout=1)


async def test_remove_refuses_running_task():
    bt = BackgroundTasks()

    async def slow(sink):
        await asyncio.sleep(5)

    tid = bt.spawn(slow)
    await asyncio.sleep(0.01)
    with pytest.raises(ValueError):
        bt.remove(tid)
    bt.cancel(tid)
    await bt.wait(tid, timeout=1)
    assert bt.remove(tid) is True
    assert bt.get(tid) is None


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
