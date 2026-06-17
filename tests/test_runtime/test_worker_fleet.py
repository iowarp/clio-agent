"""Worker-fleet orchestration (epic #667): bring up N isolated workers per role, supervise
(respawn on death), route exactly-once across them, tear down clean.

The fleet's process lifecycle is exercised here with an in-process spawner — each "worker" is
an asyncio task running the REAL ``run_isolated_worker`` over a shared LocalFS store, so the
transport/presence/exactly-once paths are real; only the OS-process boundary is stubbed (its
own live proof is the subprocess fleet in ``test_clio_core_worker``). This keeps the
orchestration logic — desired-vs-live reconciliation, respawn, spec parsing — fast and
deterministic.
"""

from __future__ import annotations

import asyncio

import pytest

from clio_agent.arc.storage import make_arc_store
from clio_agent.runtime.clio_core_transport import IsolatedExpertInvoker, run_isolated_worker
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult
from clio_agent.runtime.worker_fleet import (
    WorkerFleet,
    WorkerSpec,
    parse_fleet_spec,
)

# --- spec parsing / validation (no I/O) --------------------------------------------------


def test_parse_fleet_spec_counts_bare_and_whitespace():
    specs = parse_fleet_spec("data:2, analysis:1 , viz ,, ")
    assert specs == [
        WorkerSpec("data", 2),
        WorkerSpec("analysis", 1),
        WorkerSpec("viz", 1),  # bare role => one replica
    ]


def test_parse_fleet_spec_rejects_bad_count():
    with pytest.raises(ValueError, match="replica count"):
        parse_fleet_spec("data:notanumber")


def test_worker_spec_validates():
    with pytest.raises(ValueError):
        WorkerSpec("", 1)
    with pytest.raises(ValueError):
        WorkerSpec("data", 0)


# --- in-process spawner: a worker == an asyncio task running the real isolated worker -----


class _InProcessSpawner:
    """Test :class:`Spawner` whose 'process' is an asyncio task running the REAL
    ``run_isolated_worker`` with a shared echo handler. Honours CLIO_CORE_PREFIX from env
    (as a real worker would) and counts spawns so respawn is observable."""

    def __init__(self, store, handler):
        self._store = store
        self._handler = handler
        self.spawn_count = 0

    def spawn(self, *, role, worker_id, env):
        stop = asyncio.Event()
        task = asyncio.ensure_future(
            run_isolated_worker(
                self._store,
                self._handler,
                role=role,
                worker_id=worker_id,
                prefix=env.get("CLIO_CORE_PREFIX", "clio_core_"),
                stop=stop,
                poll=0.02,
                presence_ttl=2.0,
            )
        )
        self.spawn_count += 1
        return [task, stop]

    def is_alive(self, handle):
        task, _ = handle
        return not task.done()

    def terminate(self, handle, *, timeout=10.0):
        task, stop = handle
        stop.set()
        task.cancel()


def _echo_handler(execs):
    async def handler(req: ExpertRequest) -> ExpertResult:
        execs[req.question] = execs.get(req.question, 0) + 1
        await asyncio.sleep(0.02)
        return ExpertResult(expert_id=req.expert_id, answer=f"echo:{req.question}")

    return handler


def _fleet(store, specs, handler, ttl=2.0):
    return WorkerFleet(
        store,
        specs,
        spawner=_InProcessSpawner(store, handler),
        worker_env={},  # the in-proc spawner uses the injected store, not store-attach env
        presence_ttl=ttl,
    )


async def test_fleet_starts_desired_replicas_per_role(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    fleet = _fleet(store, [WorkerSpec("data", 2), WorkerSpec("analysis", 1)], _echo_handler({}))
    fleet.start(wait_ready=False)  # async test: don't block the loop with the sync wait
    try:
        await fleet.wait_ready_async(timeout=10)
        assert fleet.desired_counts() == {"data": 2, "analysis": 1}
        assert fleet.live_counts() == {"data": 2, "analysis": 1}
        assert fleet._spawner.spawn_count == 3  # exactly the desired number of workers
    finally:
        fleet.stop()
    await asyncio.sleep(0.1)  # let cancellations drop presence
    assert fleet.live_counts() == {"data": 0, "analysis": 0}


async def test_supervise_respawns_a_dead_worker(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    fleet = _fleet(store, [WorkerSpec("data", 2)], _echo_handler({}))
    fleet.start(wait_ready=False)
    try:
        await fleet.wait_ready_async(timeout=10)
        assert fleet._spawner.spawn_count == 2

        # simulate a crash: kill one slot's task WITHOUT going through the fleet
        task, _stop = fleet._handles["data-0"]
        task.cancel()
        await asyncio.sleep(0.1)  # let it die + drop presence
        assert fleet.live_counts()["data"] == 1  # one worker is gone

        respawned = fleet.supervise_once()
        assert respawned == 1
        assert fleet._spawner.spawn_count == 3  # the dead slot was replaced (id reused)
        await fleet.wait_ready_async(timeout=10)
        assert fleet.live_counts()["data"] == 2  # back to full strength
    finally:
        fleet.stop()
    await asyncio.sleep(0.1)


async def test_fleet_routes_exactly_once_across_workers(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    execs: dict[str, int] = {}
    fleet = _fleet(store, [WorkerSpec("calc", 3)], _echo_handler(execs))
    fleet.start(wait_ready=False)
    try:
        await fleet.wait_ready_async(timeout=10)
        invoker = IsolatedExpertInvoker(store, role="calc", timeout=15, poll=0.02, presence_ttl=2.0)

        n = 30
        results = await asyncio.gather(
            *[invoker.invoke(ExpertRequest("calc", f"q{i}")) for i in range(n)]
        )
        # every request got ITS own answer back
        for i, res in enumerate(results):
            assert res.status == "completed"
            assert res.answer == f"echo:q{i}"
        # exactly-once by construction: each question executed exactly one time
        assert all(c == 1 for c in execs.values()), {k: v for k, v in execs.items() if v != 1}
        assert len(execs) == n
        # the lease-free model NEVER writes a claim blob
        assert not any(".claim" in name for name, _ in store.scan("context", "clio_core_"))
    finally:
        fleet.stop()
    await asyncio.sleep(0.1)


async def test_stop_is_idempotent(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    fleet = _fleet(store, [WorkerSpec("data", 1)], _echo_handler({}))
    fleet.start(wait_ready=False)
    await fleet.wait_ready_async(timeout=10)
    fleet.stop()
    fleet.stop()  # second stop is a no-op, not an error
    await asyncio.sleep(0.1)
    assert fleet.live_counts() == {"data": 0}
