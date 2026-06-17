"""High-volume in-process soak over LocalFS — stresses the clio-agent delegation
semantics (mailbox, TTL lease, claim race, crash-reclaim, throughput, clean drain) at
thousands of delegations with chaos, with NO daemon limit. Finds clio-agent bugs at
scale. Gated by CLIO_STRESS_SECS>0 (skipped otherwise)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import time

import pytest

from clio_agent.arc.storage import make_arc_store
from clio_agent.runtime.cee_transport import CEEExpertInvoker, CEEMailbox, run_worker
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult

_SECS = float(os.environ.get("CLIO_STRESS_SECS", "0"))
_WORKERS = int(os.environ.get("CLIO_SOAK_WORKERS", "4"))
_BATCH = int(os.environ.get("CLIO_SOAK_BATCH", "20"))
_SLEEP = float(os.environ.get("CLIO_SOAK_SLEEP", "0.005"))
_PAYLOAD = int(os.environ.get("CLIO_SOAK_PAYLOAD", "0"))
_CRASH_EVERY = int(os.environ.get("CLIO_SOAK_CRASH_EVERY", "200"))
_LEASE = float(os.environ.get("CLIO_SOAK_LEASE", "1.0"))


@pytest.mark.skipif(_SECS <= 0, reason="set CLIO_STRESS_SECS>0 to run the soak")
async def test_localfs_highvolume_soak(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mb = CEEMailbox(store, prefix="soak_")
    inv = CEEExpertInvoker(mb, timeout=30, poll=0.02)
    execs: dict[str, int] = {}  # job -> times executed (detect double-execution)
    pad = "x" * _PAYLOAD

    async def handler(req: ExpertRequest) -> ExpertResult:
        execs[req.question] = execs.get(req.question, 0) + 1
        await asyncio.sleep(_SLEEP)
        return ExpertResult(expert_id=req.expert_id, answer=f"ok:{req.question}", workflow_state={"pad": pad})

    stop = asyncio.Event()
    workers = [
        asyncio.ensure_future(run_worker(mb, handler, stop=stop, worker_id=f"w{i}", lease_ttl=_LEASE))
        for i in range(_WORKERS)
    ]
    stats = {"done": 0, "errors": 0, "crashes": 0, "double": 0, "inflight": 0}
    failures: list[str] = []
    start = time.monotonic()
    jid = 0

    while time.monotonic() - start < _SECS:
        batch = [f"j{jid + i}" for i in range(_BATCH)]
        jid += _BATCH
        results = await asyncio.gather(
            *[inv.invoke(ExpertRequest("data", j)) for j in batch], return_exceptions=True
        )
        for j, r in zip(batch, results, strict=True):
            if isinstance(r, BaseException) or getattr(r, "answer", None) != f"ok:{j}":
                stats["errors"] += 1
                failures.append(f"{j} -> {r!r}")
            else:
                stats["done"] += 1

        # chaos: crash (cancel) + restart a worker every ~200 jobs
        if jid % _CRASH_EVERY == 0 and workers:
            victim = workers.pop(random.randrange(len(workers)))
            victim.cancel()
            with contextlib.suppress(BaseException):
                await victim
            workers.append(
                asyncio.ensure_future(run_worker(mb, handler, stop=stop, worker_id=f"w{jid}", lease_ttl=_LEASE))
            )
            stats["crashes"] += 1

        # metrics: in-flight blob count (informational — slow concurrent handlers always
        # have some in flight) + cumulative double-exec count.
        if jid % _CRASH_EVERY == 0:
            stats["inflight"] = len([n for n, _ in store.scan("context", "soak_")])
            stats["double"] = sum(1 for c in execs.values() if c > 1)
            print(
                f"[soak t={int(time.monotonic() - start)}s] done={stats['done']} "
                f"crashes={stats['crashes']} double={stats['double']} "
                f"inflight={stats['inflight']} err={stats['errors']}",
                flush=True,
            )

    stop.set()
    for w in workers:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(w, timeout=5)
    await asyncio.sleep(max(_LEASE * 2, 1.0))  # let any last reclaim settle
    residual = [n for n, _ in store.scan("context", "soak_")]
    stats["double"] = sum(1 for c in execs.values() if c > 1)
    double_rate = stats["double"] / max(stats["done"], 1)
    print(f"[soak DONE] total_jobs={jid} {stats} double_rate={double_rate:.4f} residual={len(residual)}", flush=True)

    # correctness: every job completed with its own answer (no loss / no cross-talk).
    assert stats["errors"] == 0, f"{len(failures)} violations: {failures[:10]}"
    assert stats["done"] >= _BATCH
    # no PERMANENT leak: after full drain + settle, the mailbox is empty (the late-publish
    # orphan-.res fix). double-exec itself is at-least-once: the non-atomic claim can let two
    # workers serve one job under concurrency (needs a clio-core CAS for true exactly-once),
    # so we report the rate but don't fail on it — the RESULT is always correct (errors==0).
    assert residual == [], f"permanent leak: {len(residual)} blobs left: {residual[:5]}"


_BT_SECS = float(os.environ.get("CLIO_BT_SOAK_SECS", "0"))
_BT_BATCH = int(os.environ.get("CLIO_BT_SOAK_BATCH", "200"))


@pytest.mark.skipif(_BT_SECS <= 0, reason="set CLIO_BT_SOAK_SECS>0 to run the BackgroundTasks soak")
async def test_background_tasks_soak():
    """Sustained high-concurrency soak of the monitor/wait_for engine — stresses the
    cancel/finalize paths (incl. cancel-before-first-step, the bug fixed this session) at
    scale. Each batch mixes: instant-complete, slow-complete, fail, cancel-BEFORE-start,
    cancel-DURING-run, and a shell command. Hard invariants checked every batch:
      * every task reaches a TERMINAL state (none stuck QUEUED/RUNNING) within its wait,
      * on_complete fires EXACTLY once per task (no missed / no double),
      * prune() evicts every terminal record and the registry returns to empty.
    Gated by CLIO_BT_SOAK_SECS>0."""
    from clio_agent.runtime.background_tasks import BackgroundTasks, TaskStatus, spawn_command

    bt = BackgroundTasks()
    fired: dict[str, int] = {}  # tid -> on_complete count (must end at exactly 1 each)
    stats = {
        "spawned": 0, "completed": 0, "failed": 0, "cancelled": 0,
        "stuck": 0, "bad_fire": 0, "leak": 0, "batches": 0,
    }
    failures: list[str] = []
    start = time.monotonic()

    async def _work(sink, kind: int, x: int):
        if kind == 0:
            sink.emit(f"l{x}")
            return x
        if kind == 1:
            await asyncio.sleep(0.02)
            return x
        if kind == 2:
            raise RuntimeError(f"boom{x}")
        # kinds 3 & 4 sleep long enough to be cancelled (before-start / during-run)
        await asyncio.sleep(3)
        return x

    while time.monotonic() - start < _BT_SECS:
        handles: list[tuple[int, str]] = []
        for i in range(_BT_BATCH):
            kind = i % 5
            tid = bt.spawn(lambda sink, k=kind, x=i: _work(sink, k, x), label=f"k{kind}")
            bt.on_complete(tid, lambda rec: fired.__setitem__(rec.id, fired.get(rec.id, 0) + 1))
            handles.append((kind, tid))
            stats["spawned"] += 1
        # a shell command rides the same handle every batch
        cmd_tid = spawn_command(bt, "true")
        bt.on_complete(cmd_tid, lambda rec: fired.__setitem__(rec.id, fired.get(rec.id, 0) + 1))

        # cancel kind-3 BEFORE the loop runs them (the fixed path)
        for kind, tid in handles:
            if kind == 3:
                bt.cancel(tid)
        # let kind-4 start, then cancel DURING run
        await asyncio.sleep(0.01)
        for kind, tid in handles:
            if kind == 4:
                bt.cancel(tid)

        for kind, tid in [*handles, (-1, cmd_tid)]:
            rec = await bt.wait(tid, timeout=10)
            if not rec.status.terminal:
                stats["stuck"] += 1
                failures.append(f"stuck k{kind} {tid} -> {rec.status.value}")
                continue
            if rec.status is TaskStatus.COMPLETED:
                stats["completed"] += 1
            elif rec.status is TaskStatus.FAILED:
                stats["failed"] += 1
            elif rec.status is TaskStatus.CANCELLED:
                stats["cancelled"] += 1

        # on_complete fired exactly once for every handle this batch
        for _, tid in [*handles, (-1, cmd_tid)]:
            if fired.get(tid, 0) != 1:
                stats["bad_fire"] += 1
                failures.append(f"fire={fired.get(tid, 0)} for {tid}")
            fired.pop(tid, None)  # bound the dict

        evicted = bt.prune()
        if bt.list():
            stats["leak"] += 1
            failures.append(f"registry not empty after prune: {len(bt.list())} left (evicted {evicted})")
        stats["batches"] += 1
        if stats["batches"] % 50 == 0:
            print(f"[bt-soak t={int(time.monotonic() - start)}s] {stats}", flush=True)

    print(f"[bt-soak DONE] {stats} residual_fired={len(fired)}", flush=True)
    # the engine never stranded a task, never mis-fired a notifier, never leaked a record
    assert not failures, f"{len(failures)} invariant violations: {failures[:10]}"
    assert stats["stuck"] == 0
    assert stats["bad_fire"] == 0
    assert stats["leak"] == 0
    assert stats["cancelled"] >= _BT_BATCH  # the cancel paths actually exercised
    assert bt.list() == [] and not fired  # fully drained at the end
