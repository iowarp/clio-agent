"""Real cross-PROCESS delegation tests over a clio_run daemon (epic #667, #659).

Separate OS processes share clio-core context. This is where process death, orphan
reclaim, and claim races surface — bugs that single-event-loop tests structurally
cannot catch. Gated by CLIO_RUN_CROSS_PROCESS=1 (spawns the daemon + worker procs).
No local GPU; the echo/crash workers need no model.
"""
from __future__ import annotations

import os

import pytest

from clio_agent.runtime.cee_transport import CEEExpertInvoker, CEEMailbox
from clio_agent.runtime.expert_invoker import ExpertRequest

pytestmark = pytest.mark.cross_process


async def test_happy_path_cross_process(cross_arc, spawn_worker):
    """A parent delegates to a worker in a DIFFERENT OS process; the answer crosses
    back through clio-core. The baseline the whole suite was missing."""
    prefix = "xp_happy_"
    spawn_worker(prefix, mode="echo", n=1)
    mb = CEEMailbox(cross_arc, prefix=prefix)
    res = await CEEExpertInvoker(mb, timeout=30).invoke(ExpertRequest("data", "ping"))
    assert res.answer == "echo:ping"
    wp = res.workflow_state.get("worker_pid")
    assert wp and wp != os.getpid()  # answered by a genuinely different process
    cross_arc.put("context", f"{prefix}STOP", b"1")


async def test_worker_crash_single_worker_parent_times_out(cross_arc, spawn_worker):
    """One worker that DIES mid-delegation (os._exit after claiming) -> the parent
    times out gracefully (no hang) and the orphaned request is discarded. With a
    single worker there is no reclaim."""
    prefix = "xp_crash1_"
    spawn_worker(prefix, mode="hardcrash", n=1)
    mb = CEEMailbox(cross_arc, prefix=prefix)
    with pytest.raises(TimeoutError):
        await CEEExpertInvoker(mb, timeout=6).invoke(ExpertRequest("data", "will-orphan"))
    # the invoker cleaned up the orphaned request blob on timeout
    assert not any(nm.endswith(".req") for nm, _ in cross_arc.scan("context", prefix))
    cross_arc.put("context", f"{prefix}STOP", b"1")


async def test_worker_crash_other_worker_reclaims(cross_arc, spawn_worker):
    """A dead worker's claimed-but-undelivered request is reclaimed by a LIVE worker,
    so the parent still gets a result (resilience). NB: reclaim works precisely
    because the claim is NOT a lease — the same property double-executes a merely
    SLOW worker. Exactly-once needs a real lease (tracked for #659)."""
    prefix = "xp_reclaim_"
    spawn_worker(prefix, mode="hardcrash", n=1)
    spawn_worker(prefix, mode="echo", n=1)
    mb = CEEMailbox(cross_arc, prefix=prefix)
    res = await CEEExpertInvoker(mb, timeout=30).invoke(ExpertRequest("data", "reclaim-me"))
    assert res.answer == "echo:reclaim-me"  # the live worker served it
    cross_arc.put("context", f"{prefix}STOP", b"1")
