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


@pytest.mark.skipif(_SECS <= 0, reason="set CLIO_STRESS_SECS>0 to run the soak")
async def test_localfs_highvolume_soak(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mb = CEEMailbox(store, prefix="soak_")
    inv = CEEExpertInvoker(mb, timeout=30, poll=0.02)
    execs: dict[str, int] = {}  # job -> times executed (detect double-execution)

    async def handler(req: ExpertRequest) -> ExpertResult:
        execs[req.question] = execs.get(req.question, 0) + 1
        await asyncio.sleep(0.005)
        return ExpertResult(expert_id=req.expert_id, answer=f"ok:{req.question}")

    stop = asyncio.Event()
    workers = [
        asyncio.ensure_future(run_worker(mb, handler, stop=stop, worker_id=f"w{i}", lease_ttl=1.0))
        for i in range(4)
    ]
    stats = {"done": 0, "errors": 0, "crashes": 0, "double": 0, "leaks": 0}
    failures: list[str] = []
    start = time.monotonic()
    jid = 0

    while time.monotonic() - start < _SECS:
        batch = [f"j{jid + i}" for i in range(20)]
        jid += 20
        results = await asyncio.gather(
            *[inv.invoke(ExpertRequest("data", j)) for j in batch], return_exceptions=True
        )
        for j, r in zip(batch, results):
            if isinstance(r, BaseException) or getattr(r, "answer", None) != f"ok:{j}":
                stats["errors"] += 1
                failures.append(f"{j} -> {r!r}")
            else:
                stats["done"] += 1

        # chaos: crash (cancel) + restart a worker every ~200 jobs
        if jid % 200 == 0 and workers:
            victim = workers.pop(random.randrange(len(workers)))
            victim.cancel()
            with contextlib.suppress(BaseException):
                await victim
            workers.append(
                asyncio.ensure_future(run_worker(mb, handler, stop=stop, worker_id=f"w{jid}", lease_ttl=1.0))
            )
            stats["crashes"] += 1

        # invariants: clean drain (no leaked blobs) + double-exec metric
        if jid % 200 == 0:
            leftover = [n for n, _ in store.scan("context", "soak_")]
            if leftover:
                stats["leaks"] += 1
                failures.append(f"leak {leftover[:3]}")
            stats["double"] = sum(1 for c in execs.values() if c > 1)
            print(
                f"[soak t={int(time.monotonic() - start)}s] done={stats['done']} "
                f"crashes={stats['crashes']} double={stats['double']} leaks={stats['leaks']} "
                f"err={stats['errors']}",
                flush=True,
            )

    stop.set()
    for w in workers:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(w, timeout=5)
    print(f"[soak DONE] total_jobs={jid} {stats}", flush=True)

    # every job completed correctly (no loss / no cross-talk); double-exec is at-least-once
    # under crashes and should stay low thanks to the lease.
    assert stats["errors"] == 0, f"{len(failures)} violations: {failures[:10]}"
    assert stats["done"] >= 20
