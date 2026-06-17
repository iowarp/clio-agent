"""LIVE NDP-driven pipeline (epic #667; GOAL: NDP marketplace as the use-case driver).

Models the EarthScope/NDP GNSS marketplace flow as a multi-hop, partly-parallel pipeline
of REAL ALCF experts, composed entirely from the distributable semantics — and with NO
daemon (LocalFS mailbox + in-process worker pool), so the scenario runs green every time
instead of being blocked by the clio-core cross-process wedge:

  hop 1  geospatial expert            place -> lat/lon/radius
  hop 2  data-discovery (x2, async)   plan station retrieval for the region (fanned out)
  hop 3  analysis expert              synthesize the combined retrieval plan

A reference code planted in hop 1's downstream context must reappear in every later hop's
answer — proving context crosses each delegation (and into BOTH concurrent children)
through the clio-core mailbox transport. One worker pool serves all three heterogeneous
role-experts; the role rides in each request's context.

Gated by ``CLIO_RUN_LIVE=1``. ALCF only (no local GPU).
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import dspy
import pytest

from clio_agent.arc.storage import make_arc_store
from clio_agent.config import create_lm, load_config_from_env
from clio_agent.runtime.background_tasks import BackgroundTasks, TaskStatus
from clio_agent.runtime.cee_transport import CEEExpertInvoker, CEEMailbox, run_worker
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult, spawn_invocation

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CLIO_RUN_LIVE") != "1",
        reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
    ),
]

GEO_ROLE = (
    "You are a geospatial expert. Given a place name, reply with its approximate center "
    "latitude, longitude, and a sensible search radius in km. If the context contains a "
    "REFERENCE CODE, include it verbatim in your answer."
)
DATA_ROLE = (
    "You are an EarthScope GNSS data-discovery expert. Using the geospatial context, "
    "briefly plan which station data to retrieve for the region. If the context contains "
    "a REFERENCE CODE, you MUST include it verbatim in your answer."
)
ANALYSIS_ROLE = (
    "You are a geophysical analysis expert. Summarize the combined GNSS retrieval plan in "
    "one or two sentences. If the context contains a REFERENCE CODE, include it verbatim."
)


def _question_with_context(base: str, context: dict) -> str:
    parts = [base]
    for key, value in context.items():
        if key == "role":
            continue
        parts.append(f"\n[{key}]: {value}")
    return "".join(parts)


async def test_ndp_pipeline_real_alcf(tmp_path):
    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")
    lm = create_lm(cfg)

    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store)

    async def handler(req: ExpertRequest) -> ExpertResult:
        role = req.context.get("role", "You are a helpful scientific expert.")
        question = _question_with_context(req.question, req.context)

        def _run():
            with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
                sig = dspy.Signature("question -> answer", role)
                return dspy.Predict(sig)(question=question)

        pred = await asyncio.to_thread(_run)
        return ExpertResult(expert_id=req.expert_id, answer=str(getattr(pred, "answer", "") or ""))

    stop = asyncio.Event()
    workers = [
        asyncio.ensure_future(run_worker(mailbox, handler, stop=stop, worker_id=f"w{i}", poll=0.05))
        for i in range(3)
    ]
    invoker = CEEExpertInvoker(mailbox, timeout=300, poll=0.05)
    tasks = BackgroundTasks()
    needle = "NDP-7Q4Z"  # the reference code that must survive every hop

    try:
        # hop 1 — geospatial expert (sequential; later hops depend on it)
        geo = await invoker.invoke(
            ExpertRequest(
                "geospatial",
                "San Diego, California",
                context={"role": GEO_ROLE, "REFERENCE CODE": needle},
            )
        )
        assert geo.answer.strip(), "geo hop returned no answer"

        # hop 2 — data-discovery FANNED OUT: two independent children run concurrently,
        # each fed hop-1 context + the reference code.
        dd_ctx = {"role": DATA_ROLE, "geospatial": geo.answer, "REFERENCE CODE": needle}
        h_coastal = spawn_invocation(
            tasks, invoker,
            ExpertRequest("data", "Plan retrieval for COASTAL stations in the region.", context=dd_ctx),
            label="dd_coastal",
        )
        h_inland = spawn_invocation(
            tasks, invoker,
            ExpertRequest("data", "Plan retrieval for INLAND stations in the region.", context=dd_ctx),
            label="dd_inland",
        )

        # monitor: both children should be in flight (RUNNING) before either finishes
        running_seen = False
        for _ in range(300):
            if any(tasks.status(h) is TaskStatus.RUNNING for h in (h_coastal, h_inland)):
                running_seen = True
            if all(tasks.get(h).done.is_set() for h in (h_coastal, h_inland)):
                break
            await asyncio.sleep(0.05)
        assert running_seen, "data-discovery fan-out never went async"

        r_coastal = await tasks.wait(h_coastal, timeout=300)
        r_inland = await tasks.wait(h_inland, timeout=300)
        assert r_coastal.status is TaskStatus.COMPLETED, f"coastal -> {r_coastal.error}"
        assert r_inland.status is TaskStatus.COMPLETED, f"inland -> {r_inland.error}"

        # hop 3 — analysis expert consumes BOTH discovery plans
        an = await invoker.invoke(
            ExpertRequest(
                "analysis",
                "Summarize the combined EarthScope GNSS retrieval plan.",
                context={
                    "role": ANALYSIS_ROLE,
                    "coastal_plan": r_coastal.result.answer,
                    "inland_plan": r_inland.result.answer,
                    "REFERENCE CODE": needle,
                },
            )
        )
    finally:
        stop.set()
        for w in workers:
            w.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*workers, return_exceptions=True)

    # the reference code crossed every hop — into BOTH concurrent children and through to
    # the final synthesis — proving context integrity across the whole detached pipeline
    assert needle in r_coastal.result.answer, f"coastal lost the code: {r_coastal.result.answer!r}"
    assert needle in r_inland.result.answer, f"inland lost the code: {r_inland.result.answer!r}"
    assert needle in an.answer, f"analysis lost the code: {an.answer!r}"
    # the mailbox drained clean — no leaked req/res/claim across the multi-hop pipeline
    assert [n for n, _ in store.scan("context", "cee_")] == []
