"""Sustained stress SOAK — continuous, mixed-semantic load over real clio (+ ALCF) with
chaos, asserting invariants the whole time. The harness for a multi-hour campaign.

Runs for ``CLIO_STRESS_SECS`` (default 60; set to e.g. 21600 for 6h). Every batch mixes:
  - high-volume echo delegations (throughput + no cross-talk),
  - real ALCF delegations (if CLIO_RUN_LIVE=1),
  - background bash commands (the unified handle),
  - chaos: kill + respawn a worker (pool resilience under churn).
Invariants checked continuously: every job returns ITS own answer, the mailbox drains
clean (no leaked blobs), no errors. Metrics logged each interval. Gated cross_process.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from clio_agent.runtime.background_tasks import BackgroundTasks, TaskStatus, spawn_command
from clio_agent.runtime.cee_transport import CEEExpertInvoker, CEEMailbox
from clio_agent.runtime.expert_invoker import ExpertRequest

pytestmark = pytest.mark.cross_process

_DURATION = float(os.environ.get("CLIO_STRESS_SECS", "60"))
_BATCH = int(os.environ.get("CLIO_STRESS_BATCH", "8"))
_LIVE = os.environ.get("CLIO_RUN_LIVE") == "1"
_ALCF = {
    "CLIO_LM_PROVIDER": "argonne",
    "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
    "CLIO_LM_MODEL": "openai/gpt-oss-120b",
    "CLIO_RUN_LIVE": "1",
}


async def test_load_isolation(cross_arc, spawn_worker):
    """Isolate the wedge: pure SEQUENTIAL delegations, no chaos/concurrency/commands.
    How many before the daemon stops serving? Characterizes the limit (report-only)."""
    prefix = "st_iso_"
    spawn_worker(prefix, mode="echo", n=2)
    inv = CEEExpertInvoker(CEEMailbox(cross_arc, prefix=prefix), timeout=8)
    ok = 0
    for i in range(2000):
        try:
            r = await inv.invoke(ExpertRequest("data", f"j{i}"))
            assert r.answer == f"echo:j{i}"
            ok += 1
            if i % 50 == 0:
                print(f"[iso] {ok} ok", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[iso] WEDGED after {ok} delegations: {type(exc).__name__}: {exc}", flush=True)
            break
    print(f"[iso] FINAL ok={ok}", flush=True)
    cross_arc.put("context", prefix + "STOP", b"1")
    assert ok > 0


async def test_stress_soak(cross_arc, spawn_worker):
    echo_prefix, alcf_prefix = "st_echo_", "st_alcf_"
    echo_pool = list(spawn_worker(echo_prefix, mode="echo", n=4))
    if _LIVE:
        spawn_worker(alcf_prefix, mode="alcf", n=2, extra_env=_ALCF)

    echo_inv = CEEExpertInvoker(CEEMailbox(cross_arc, prefix=echo_prefix), timeout=60)
    alcf_inv = CEEExpertInvoker(CEEMailbox(cross_arc, prefix=alcf_prefix), timeout=180)
    cmd_tasks = BackgroundTasks()

    stats = {"echo": 0, "alcf": 0, "cmd": 0, "crashes": 0, "leaks": 0, "errors": 0}
    failures: list[str] = []
    start = time.monotonic()
    it = 0

    while time.monotonic() - start < _DURATION:
        it += 1

        # 1) high-volume echo delegations — invariant: each gets ITS own answer
        batch = [f"e{it}_{i}" for i in range(_BATCH)]
        res = await asyncio.gather(
            *[echo_inv.invoke(ExpertRequest("data", j)) for j in batch], return_exceptions=True
        )
        for j, r in zip(batch, res):
            if isinstance(r, BaseException) or getattr(r, "answer", None) != f"echo:{j}":
                stats["errors"] += 1
                failures.append(f"echo {j} -> {r!r}")
            else:
                stats["echo"] += 1

        # 2) a background command (unified handle) every 3rd iter
        if it % 3 == 0:
            tid = spawn_command(cmd_tasks, "echo soak-cmd; true")
            rec = await cmd_tasks.wait(tid, timeout=15)
            if rec.status is TaskStatus.COMPLETED and rec.result.get("exit_code") == 0:
                stats["cmd"] += 1
                cmd_tasks.remove(tid)
            else:
                stats["errors"] += 1
                failures.append(f"cmd -> {rec.status} {rec.result}")

        # 3) a real ALCF delegation every 5th iter (if live)
        if _LIVE and it % 5 == 0:
            try:
                r = await alcf_inv.invoke(
                    ExpertRequest("data", "Use the lookup tool to get the 'x' metric and report its value.")
                )
                if "42.7" in r.answer:
                    stats["alcf"] += 1
                else:
                    stats["errors"] += 1
                    failures.append(f"alcf -> {r.answer!r}")
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                failures.append(f"alcf err {exc}")

        # 4) chaos: kill + respawn an echo worker every 10th iter
        if it % 10 == 0 and echo_pool:
            victim = echo_pool.pop(0)
            victim.kill()
            victim.wait()
            stats["crashes"] += 1
            echo_pool += list(spawn_worker(echo_prefix, mode="echo", n=1))

        # 5) invariant: the echo mailbox drains clean (no leaked req/res/claim) + metrics
        if it % 10 == 0:
            leftover = [
                nm for nm, _ in cross_arc.scan("context", echo_prefix)
                if "READY" not in nm and not nm.endswith("STOP")
            ]
            if leftover:
                stats["leaks"] += 1
                failures.append(f"leak {leftover[:3]}")
            print(f"[soak t={int(time.monotonic() - start)}s it={it}] {stats}", flush=True)

    print(f"[soak DONE] dur={int(time.monotonic() - start)}s {stats}", flush=True)
    cross_arc.put("context", echo_prefix + "STOP", b"1")
    if _LIVE:
        cross_arc.put("context", alcf_prefix + "STOP", b"1")

    assert not failures, f"stress found {len(failures)} invariant violations: {failures[:10]}"
    assert stats["echo"] > 0
