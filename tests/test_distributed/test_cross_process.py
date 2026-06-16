"""Real cross-PROCESS delegation tests over a clio_run daemon (epic #667, #659).

Separate OS processes share clio-core context. This is where process death, orphan
reclaim, and claim races surface — bugs that single-event-loop tests structurally
cannot catch. Gated by CLIO_RUN_CROSS_PROCESS=1 (spawns the daemon + worker procs).
No local GPU; the echo/crash workers need no model.
"""
from __future__ import annotations

import asyncio
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


async def test_concurrent_delegations_across_worker_processes(cross_arc, spawn_worker):
    """N concurrent delegations distributed across M real worker PROCESSES: each
    parent gets ITS job back (no cross-talk), and the mailbox drains clean (the
    blob-leak fix — successful delegations no longer accumulate req/res/claim)."""
    prefix = "xp_thru_"
    spawn_worker(prefix, mode="echo", n=3)
    mb = CEEMailbox(cross_arc, prefix=prefix)
    inv = CEEExpertInvoker(mb, timeout=40)
    n = 12

    async def one(job: str):
        return job, await inv.invoke(ExpertRequest("data", job))

    results = await asyncio.gather(*[one(f"job{i}") for i in range(n)])
    for job, res in results:
        assert res.answer == f"echo:{job}"  # ITS own job, no cross-talk
    # mailbox fully drained — no leaked req/res/claim blobs after success
    leftovers = [nm for nm, _ in cross_arc.scan("context", prefix) if "READY" not in nm]
    assert leftovers == [], f"mailbox leaked: {leftovers}"
    cross_arc.put("context", f"{prefix}STOP", b"1")


_ALCF = {
    "CLIO_LM_PROVIDER": "argonne",
    "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
    "CLIO_LM_MODEL": "openai/gpt-oss-120b",
    "CLIO_RUN_LIVE": "1",
}


@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="real ALCF trace: set CLIO_RUN_LIVE=1 (+ Argonne auth)",
)
async def test_real_alcf_expert_cross_process(cross_arc, spawn_worker):
    """A REAL ALCF expert runs on a worker process; the parent delegates to it across
    the process boundary and gets the tool-grounded answer back through clio-core."""
    prefix = "xp_alcf_"
    spawn_worker(prefix, mode="alcf", n=1, extra_env=_ALCF)
    mb = CEEMailbox(cross_arc, prefix=prefix)
    res = await CEEExpertInvoker(mb, timeout=150).invoke(
        ExpertRequest("data", "Use the lookup tool to get the 'latency' metric and report its exact value.")
    )
    assert res.workflow_state.get("worker_pid") != os.getpid()  # ran in the worker process
    assert "42.7" in res.answer  # the tool-derived value crossed back through clio-core
    cross_arc.put("context", f"{prefix}STOP", b"1")


_METIS = {
    "CLIO_LM_PROVIDER": "argonne",
    "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1",
    "CLIO_LM_MODEL": "gpt-oss-120b",
    "CLIO_RUN_LIVE": "1",
}


@pytest.mark.skipif(os.environ.get("CLIO_RUN_LIVE") != "1", reason="real ALCF trace")
async def test_heterogeneous_alcf_models_cross_process(cross_arc, spawn_worker):
    """Two workers on DIFFERENT ALCF endpoints/models (Sophia + Metis), each its own
    process — per-expert model holds cross-process. Metis is best-effort (maintenance)."""
    spawn_worker("xp_s_", mode="alcf", n=1, extra_env=_ALCF)
    s = await CEEExpertInvoker(CEEMailbox(cross_arc, prefix="xp_s_"), timeout=150).invoke(
        ExpertRequest("data", "Use the lookup tool to get the 'throughput' metric and report its value.")
    )
    assert s.workflow_state["api_base"].endswith("/sophia/vllm/v1")  # used ITS endpoint
    assert "42.7" in s.answer
    cross_arc.put("context", "xp_s_STOP", b"1")

    spawn_worker("xp_m_", mode="alcf", n=1, extra_env=_METIS)
    try:
        m = await CEEExpertInvoker(CEEMailbox(cross_arc, prefix="xp_m_"), timeout=150).invoke(
            ExpertRequest("data", "Use the lookup tool to get the 'latency' metric and report its value.")
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Metis leg unavailable (maintenance): {exc}")
    finally:
        cross_arc.put("context", "xp_m_STOP", b"1")
    assert m.workflow_state["api_base"].endswith("/metis/api/v1")  # its OWN, different endpoint
    assert m.workflow_state["worker_pid"] != s.workflow_state["worker_pid"]


@pytest.mark.skipif(os.environ.get("CLIO_RUN_LIVE") != "1", reason="real ALCF trace")
async def test_ndp_pipeline_multihop_cross_process(cross_arc, spawn_worker):
    """Modeled on the EarthScope/NDP marketplace pipeline: a geospatial expert then a
    data-discovery expert, each a real ALCF expert in a SEPARATE worker process. The
    parent orchestrates; context flows between hops through clio-core. A needle in the
    data hop's context proves it crossed the process boundary and reached the expert."""
    geo_role = (
        "You are a geospatial expert. Given a place name, reply with its approximate "
        "center latitude, longitude, and a sensible search radius in km."
    )
    data_role = (
        "You are an EarthScope GNSS data-discovery expert. Using the geospatial context, "
        "briefly plan which station data to retrieve for the region. If the context "
        "contains a REFERENCE CODE, you MUST include it verbatim in your answer."
    )
    spawn_worker("xp_geo_", mode="role", n=1, extra_env={**_ALCF, "CLIO_WORKER_ROLE": geo_role})
    spawn_worker("xp_dd_", mode="role", n=1, extra_env={**_ALCF, "CLIO_WORKER_ROLE": data_role})

    # hop 1 — geospatial expert (worker process A)
    geo = await CEEExpertInvoker(CEEMailbox(cross_arc, prefix="xp_geo_"), timeout=150).invoke(
        ExpertRequest("geospatial", "San Diego, California", context={})
    )
    assert geo.answer and geo.workflow_state["worker_pid"] != os.getpid()

    # hop 2 — data-discovery expert (worker process B), fed hop-1 context + a needle
    needle = f"REFERENCE CODE: NDP-{os.getpid()}-7Q4Z"
    dd = await CEEExpertInvoker(CEEMailbox(cross_arc, prefix="xp_dd_"), timeout=150).invoke(
        ExpertRequest(
            "data",
            "Plan the EarthScope GNSS data retrieval for the region in the context.",
            context={"geospatial": geo.answer, "note": needle},
        )
    )
    # a genuinely different process handled hop 2...
    assert dd.workflow_state["worker_pid"] not in (os.getpid(), geo.workflow_state["worker_pid"])
    # ...and the context crossed the process boundary intact (the needle came back)
    assert "NDP-" in dd.answer and "7Q4Z" in dd.answer
    cross_arc.put("context", "xp_geo_STOP", b"1")
    cross_arc.put("context", "xp_dd_STOP", b"1")
