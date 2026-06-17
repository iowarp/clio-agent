"""Isolated per-worker-queue delegation — the lease-free model (clio-core#559, per Luke).

Each request is routed to ONE worker's private queue, so that worker is the sole reader:
no claim, no lease, exactly-once BY CONSTRUCTION. Resilience is parent-side reassignment on
timeout; load is round-robin over a worker-presence list kept off the request hot path.
Offline (LocalFS); the cross-process real-ALCF proof lives in the live suite.
"""

from __future__ import annotations

import asyncio

from clio_agent.arc.storage import make_arc_store
from clio_agent.runtime.cee_transport import (
    IsolatedExpertInvoker,
    drop_presence,
    heartbeat_presence,
    live_workers,
    run_isolated_worker,
)
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult


async def _wait_for_workers(store, role, n, *, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(live_workers(store, role)) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"workers for {role!r} never reached {n}: {live_workers(store, role)}")


async def test_isolated_happy_path(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))

    async def handler(req: ExpertRequest) -> ExpertResult:
        return ExpertResult(expert_id=req.expert_id, answer=f"served:{req.question}")

    stop = asyncio.Event()
    worker = asyncio.ensure_future(
        run_isolated_worker(store, handler, role="data", worker_id="w1", stop=stop, poll=0.01)
    )
    try:
        await _wait_for_workers(store, "data", 1)
        res = await IsolatedExpertInvoker(store, role="data", timeout=5, poll=0.01).invoke(
            ExpertRequest("data", "ping")
        )
        assert res.answer == "served:ping"
    finally:
        stop.set()
        await asyncio.gather(worker, return_exceptions=True)


async def test_isolated_is_exactly_once_with_no_claim_blobs(tmp_path):
    """30 requests round-robined across 3 isolated workers: each runs EXACTLY ONCE (no
    double-execution — there is no shared queue to race), every parent gets ITS own answer,
    and the transport never writes a single .claim blob (the whole point — no lease)."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    execs: dict[str, int] = {}

    async def handler(req: ExpertRequest) -> ExpertResult:
        execs[req.question] = execs.get(req.question, 0) + 1
        await asyncio.sleep(0.01)
        return ExpertResult(expert_id=req.expert_id, answer=f"ok:{req.question}")

    stop = asyncio.Event()
    workers = [
        asyncio.ensure_future(
            run_isolated_worker(store, handler, role="data", worker_id=f"w{i}", stop=stop, poll=0.01)
        )
        for i in range(3)
    ]
    try:
        await _wait_for_workers(store, "data", 3)
        invoker = IsolatedExpertInvoker(store, role="data", timeout=10, poll=0.01)
        n = 30
        results = await asyncio.gather(
            *[invoker.invoke(ExpertRequest("data", f"j{i}")) for i in range(n)]
        )
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)

    for i, res in enumerate(results):
        assert res.answer == f"ok:j{i}"  # each parent got its own answer, no cross-talk
    assert len(execs) == n
    assert all(count == 1 for count in execs.values()), f"double-exec: {execs}"  # exactly-once
    # the lease-free guarantee: NO claim blobs were ever written
    assert not any(".claim" in name for name, _ in store.scan("context", "cee"))
    # and the per-worker queues drained clean
    assert not any(name.endswith(".req") or name.endswith(".res") for name, _ in store.scan("context", "cee"))


def test_presence_reflects_live_and_dead_workers(tmp_path):
    """Presence is a heartbeat-with-TTL: a worker that stops beating drops from the live set
    after ttl; an explicit drop removes it immediately. Deterministic via injected ``now``."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    heartbeat_presence(store, "data", "w1", now=1000.0)
    heartbeat_presence(store, "data", "w2", now=1000.0)
    assert live_workers(store, "data", ttl=6.0, now=1001.0) == ["w1", "w2"]

    heartbeat_presence(store, "data", "w2", now=1005.0)  # w2 renews; w1 goes stale
    assert live_workers(store, "data", ttl=6.0, now=1008.0) == ["w2"]  # w1: 8s>6s, w2: 3s<6s

    drop_presence(store, "data", "w2")
    assert live_workers(store, "data", ttl=6.0, now=1008.0) == []


async def test_reassigns_to_a_live_worker_on_timeout(tmp_path):
    """A worker that is PRESENT (heartbeating) but not draining must not strand a request:
    the parent times out on it and re-routes to a live worker. The phantom is named to sort
    LAST so the round-robin tries it first, exercising the reassignment path."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    stop = asyncio.Event()

    async def phantom_presence() -> None:  # heartbeats but never drains its queue
        while not stop.is_set():
            heartbeat_presence(store, "data", "zzz_phantom")
            await asyncio.sleep(0.1)

    async def handler(req: ExpertRequest) -> ExpertResult:
        return ExpertResult(expert_id=req.expert_id, answer=f"served:{req.question}")

    phantom = asyncio.ensure_future(phantom_presence())
    real = asyncio.ensure_future(
        run_isolated_worker(store, handler, role="data", worker_id="aaa_real", stop=stop, poll=0.01)
    )
    try:
        await _wait_for_workers(store, "data", 2)
        # short per-attempt timeout so the phantom attempt fails fast, then reassigns
        invoker = IsolatedExpertInvoker(store, role="data", timeout=0.5, poll=0.02, max_attempts=4)
        res = await invoker.invoke(ExpertRequest("data", "q"))
        assert res.answer == "served:q"  # the live worker served it after reassignment
    finally:
        stop.set()
        await asyncio.gather(phantom, real, return_exceptions=True)
