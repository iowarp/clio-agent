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
        for j, r in zip(batch, results):
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
