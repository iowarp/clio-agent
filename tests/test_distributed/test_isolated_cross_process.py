"""The isolated detached delegation model over a REAL clio_run daemon, cross-process (#561).

This is the test that was missing — and whose absence let a CONFIG bug masquerade as a
clio-core "wedge." Every earlier exactly-once / soak proof ran over LocalFS, which never
touches clio-core. Here the parent and N worker PROCESSES share ONE properly-configured
``clio_run`` daemon (bounded DRAM + a file tier — see clio_test_daemon.yaml) and delegate over
the CTE transport. Asserts the real distributable guarantees on the real backend:
  * cross-process — every answer is produced by a DIFFERENT OS process,
  * exactly-once — no request runs twice, and ZERO claim/lease blobs (the lease-free model),
  * no wedge — many concurrent delegations across the pool all complete, daemon stays healthy.

Gated by ``CLIO_RUN_CROSS_PROCESS=1`` (needs the clio_daemon fixture). LM-free (echo handler),
so it runs without ALCF.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ISOLATED_WORKER = Path(__file__).resolve().parent / "_isolated_cte_worker.py"
# the FULL gact worker entry (build_app + ClioAgent + registers 'calc'): the real deployment
# worker, used for the fleet-orchestrated end-to-end over CTE with a real child.
_GACT_WORKER = Path(__file__).resolve().parent.parent / "test_runtime" / "_clio_core_worker_entry.py"


@pytest.mark.cross_process
def test_isolated_delegation_cross_process_over_cte(clio_daemon):
    from clio_agent.arc.storage import make_arc_store
    from clio_agent.runtime.clio_core_transport import IsolatedExpertInvoker, live_workers
    from clio_agent.runtime.expert_invoker import ExpertRequest

    role = "calc"
    n_workers = 3
    n_requests = 45
    # unique namespace so this test can't collide with others on the session-shared daemon
    prefix = f"isoxp_{os.getpid()}_"
    stop_key = f"{prefix}STOP"

    base_env = {
        **clio_daemon["env"],  # LD_LIBRARY_PATH + CLIO_SERVER_CONF the daemon was started with
        "CLIO_CTE_WITH_RUNTIME": "0",
        "CLIO_ARC_STORE": "cte",
        "CLIO_CORE_ROLE": role,
        "CLIO_CORE_PREFIX": prefix,
        "CLIO_CORE_STOP_KEY": stop_key,
    }

    procs: list[subprocess.Popen] = []
    try:
        for i in range(n_workers):
            env = {**base_env, "CLIO_CORE_WORKER_ID": f"w{i}"}
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(_ISOLATED_WORKER)],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )

        async def drive() -> dict:
            os.environ["CLIO_CTE_WITH_RUNTIME"] = "0"
            os.environ["CLIO_ARC_STORE"] = "cte"
            store = make_arc_store(backend="cte")

            # wait for all worker PROCESSES to announce presence over CTE
            for _ in range(600):
                if len(live_workers(store, role, prefix=prefix)) >= n_workers:
                    break
                if any(p.poll() is not None for p in procs):
                    raise RuntimeError("a worker subprocess exited before registering presence")
                await asyncio.sleep(0.1)
            live = live_workers(store, role, prefix=prefix)
            assert len(live) >= n_workers, f"workers never registered over CTE: {live}"

            invoker = IsolatedExpertInvoker(
                store, role=role, prefix=prefix, timeout=120, poll=0.05, ready_timeout=30
            )
            results = await asyncio.gather(
                *[invoker.invoke(ExpertRequest(role, f"q{i}")) for i in range(n_requests)],
                return_exceptions=True,
            )
            store.put("context", stop_key, b"1")  # tell the workers to stop
            claim_blobs = [n for n, _ in store.scan("context", prefix) if ".claim" in n]
            return {"results": results, "claim_blobs": claim_blobs, "store": store}

        out = asyncio.run(drive())
        results = out["results"]

        # exactly-once + cross-process: every request answered, by a real other process, once
        answers = []
        worker_pids = set()
        parent_pid = str(os.getpid())
        for r in results:
            assert not isinstance(r, BaseException), f"delegation failed over CTE: {r!r}"
            assert r.status == "completed", f"{r.status} {r.error}"
            assert r.answer.startswith("WORKER"), r.answer
            pid = r.answer.split(":")[0].removeprefix("WORKER")
            assert pid != parent_pid, "a child ran in the PARENT process, not cross-process"
            worker_pids.add(pid)
            answers.append(r.answer.split(":", 1)[1])

        # every distinct request got its own answer (no loss / no cross-talk)
        assert sorted(answers) == sorted(f"q{i}" for i in range(n_requests))
        # the work actually spread across the worker POOL (not all to one)
        assert len(worker_pids) >= 2, f"only {len(worker_pids)} worker process(es) served"
        # the lease-free model never writes a claim/lease blob — exactly-once by construction
        assert out["claim_blobs"] == [], f"unexpected claim blobs over CTE: {out['claim_blobs']}"
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


@pytest.mark.cross_process
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_CTE_SOAK") != "1",
    reason="sustained CTE soak (~2min): set CLIO_RUN_CTE_SOAK=1",
)
def test_isolated_delegation_sustained_soak_over_cte(clio_daemon):
    """SUSTAINED delegation soak over the real daemon — the test that retires clio-core#561.

    The old soak misread a config bug (80%-RAM DRAM tier) + a transport bug (every poll did a
    ~30ms GetContainedBlobs tag-scan + a GetBlob that raced the parent's discard) as a clio-core
    "daemon wedge after ~700 CTE ops." With a bounded+disk-backed daemon and the transport fixes
    (cached presence, a per-queue doorbell so idle workers don't scan, GetBlob tolerant of a
    concurrent delete), the SAME path runs 1000+ delegations (~10k CTE ops, 14x the old "wedge")
    with zero failures and NON-degrading throughput. Gated by CLIO_RUN_CTE_SOAK=1 (~2 min)."""
    from clio_agent.arc.storage import make_arc_store
    from clio_agent.runtime.clio_core_transport import IsolatedExpertInvoker, live_workers
    from clio_agent.runtime.expert_invoker import ExpertRequest

    role = "calc"
    n_workers = 3
    total = 600  # ~6000 CTE ops — far past the old ~700-op "wedge"
    batch = 20
    prefix = f"isosoak_{os.getpid()}_"
    stop_key = f"{prefix}STOP"
    base_env = {
        **clio_daemon["env"],
        "CLIO_CTE_WITH_RUNTIME": "0",
        "CLIO_ARC_STORE": "cte",
        "CLIO_CORE_ROLE": role,
        "CLIO_CORE_PREFIX": prefix,
        "CLIO_CORE_STOP_KEY": stop_key,
    }
    procs: list[subprocess.Popen] = []
    try:
        for i in range(n_workers):
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(_ISOLATED_WORKER)],
                    env={**base_env, "CLIO_CORE_WORKER_ID": f"w{i}"},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )

        async def drive() -> dict:
            os.environ["CLIO_CTE_WITH_RUNTIME"] = "0"
            os.environ["CLIO_ARC_STORE"] = "cte"
            store = make_arc_store(backend="cte")
            for _ in range(600):
                if len(live_workers(store, role, prefix=prefix)) >= n_workers:
                    break
                await asyncio.sleep(0.1)
            inv = IsolatedExpertInvoker(
                store, role=role, prefix=prefix, timeout=120, poll=0.05, ready_timeout=30
            )
            done = failed = 0
            for b in range(total // batch):
                res = await asyncio.gather(
                    *[inv.invoke(ExpertRequest(role, f"b{b}_{i}")) for i in range(batch)],
                    return_exceptions=True,
                )
                for r in res:
                    if not isinstance(r, BaseException) and getattr(r, "answer", "").startswith(
                        "WORKER"
                    ):
                        done += 1
                    else:
                        failed += 1
            store.put("context", stop_key, b"1")
            claim_blobs = [n for n in store.iter_names("context", prefix) if ".claim" in n]
            return {"done": done, "failed": failed, "claim_blobs": claim_blobs}

        out = asyncio.run(drive())
        # the whole point: it does NOT wedge — every delegation completes, none fail, no claims
        assert out["failed"] == 0, f"{out['failed']} failed delegations (a wedge would fail many)"
        assert out["done"] == total, f"only {out['done']}/{total} completed"
        assert out["claim_blobs"] == [], f"unexpected claim blobs: {out['claim_blobs']}"
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


@pytest.mark.cross_process
@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="full-stack cluster proof: set CLIO_RUN_LIVE=1 + CLIO_RUN_CROSS_PROCESS=1 (+ ALCF auth)",
)
def test_fleet_orchestrated_isolated_delegation_over_cte_with_real_child(clio_daemon, tmp_path):
    """THE cluster-readiness proof, on one box, over the REAL transport.

    Composes the entire distributable stack against a real ``clio_run`` daemon:
      * orchestration — ``WorkerFleet`` spawns + supervises real gact worker PROCESSES
        (``build_app`` + ``ClioAgent``) attached to the daemon over CTE (``cte_worker_env``),
      * live default — the production hinge ``run_child_via_boundary(mode="clio_core_isolated")``
        (exactly what ``_invoke_child_expert`` calls) routes a delegation to that pool,
      * real inference — a worker runs a real ALCF child and the answer folds back,
      * exactly-once — lease-free, zero claim blobs.
    The parent's ``run_child`` is rigged to fail if touched, so a pass proves out-of-process
    execution over CTE. The ONLY thing a GPU cluster adds is cross-node networking (clio-core's,
    identical code). Gated by CLIO_RUN_LIVE=1 + CLIO_RUN_CROSS_PROCESS=1.
    """
    import time
    from types import SimpleNamespace

    from clio_agent.config import load_config_from_env
    from clio_agent.gact.delegation_invoker import run_child_via_boundary
    from clio_agent.runtime.clio_core_transport import live_workers
    from clio_agent.runtime.worker_fleet import (
        LocalSubprocessSpawner,
        WorkerFleet,
        WorkerSpec,
        cte_worker_env,
    )

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")

    daemon_env = clio_daemon["env"]
    # the parent (this process) attaches to the SAME daemon over CTE
    for k in ("LD_LIBRARY_PATH", "CLIO_SERVER_CONF"):
        if k in daemon_env:
            os.environ[k] = daemon_env[k]
    os.environ["CLIO_CTE_WITH_RUNTIME"] = "0"
    os.environ["CLIO_ARC_STORE"] = "cte"

    from clio_agent.arc.storage import make_arc_store

    store = make_arc_store(backend="cte")

    role = "calc"
    prefix = f"fleetcte_{os.getpid()}_"
    # the parent's isolated invoker reads these from the env (via _invoke_via_isolated)
    os.environ["CLIO_CORE_PREFIX"] = prefix
    os.environ["CLIO_CORE_READY_TIMEOUT"] = "180"
    os.environ["CLIO_CORE_TIMEOUT"] = "240"
    os.environ.pop("CLIO_CORE_ROLE", None)  # role defaults to the expert id

    # worker env: attach to the daemon over CTE + ALCF creds (NOT HOME — the ~/.globus token
    # lives under the real HOME; overriding it breaks worker auth).
    worker_env = {
        **{k: daemon_env[k] for k in ("LD_LIBRARY_PATH", "CLIO_SERVER_CONF") if k in daemon_env},
        **cte_worker_env(),
        "CLIO_RUN_LIVE": "1",
        "CLIO_LM_PROVIDER": os.environ.get("CLIO_LM_PROVIDER", "argonne"),
        "CLIO_LM_API_BASE": os.environ.get(
            "CLIO_LM_API_BASE",
            "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        ),
        "CLIO_LM_MODEL": os.environ.get("CLIO_LM_MODEL", "openai/gpt-oss-120b"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "CLIO_ALLOWED_ROOTS": f"{tmp_path}:{os.getcwd()}",
    }

    fleet = WorkerFleet(
        store,
        [WorkerSpec(role, replicas=2)],
        spawner=LocalSubprocessSpawner(
            command=[sys.executable, str(_GACT_WORKER)], log_dir=str(tmp_path / "wlogs")
        ),
        worker_env=worker_env,
        prefix=prefix,
    )

    async def parent_run_child(agent_def, prompt):
        raise AssertionError("parent must NOT run the child — the detached CTE pool does")

    try:
        fleet.start(wait_ready=False)
        # real gact workers build_app + ClioAgent before announcing presence (slow); wait
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if live_workers(store, role, prefix=prefix):
                break
            time.sleep(0.2)
        assert live_workers(store, role, prefix=prefix), "no gact worker registered over CTE"

        out = asyncio.run(
            run_child_via_boundary(
                SimpleNamespace(id=role),
                "What is 2 + 2? Answer with only the number.",
                run_child=parent_run_child,
                session_id="fleetcte",
                mode="clio_core_isolated",
                store=store,
                role=role,
            )
        )
        assert "4" in str(getattr(out, "answer", "")), f"answer from CTE worker: {out.answer!r}"
        claim_blobs = [n for n in store.iter_names("context", prefix) if ".claim" in n]
        assert claim_blobs == [], f"unexpected claim blobs: {claim_blobs}"
    finally:
        fleet.stop()
        for k in ("CLIO_CORE_PREFIX", "CLIO_CORE_READY_TIMEOUT", "CLIO_CORE_TIMEOUT"):
            os.environ.pop(k, None)
