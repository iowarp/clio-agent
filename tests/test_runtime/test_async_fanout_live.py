"""LIVE async fan-out (epic #667, #670/#671): a parent fans out several REAL ALCF
child delegations concurrently and monitors them through the background handle.

Composes the semantics that a cluster run leans on, end to end with real inference and
NO daemon (LocalFS mailbox, in-process worker pool — so it is not blocked by the
clio-core cross-process wedge):
  * (b) async expert execution — ``spawn_invocation`` wraps each delegation in a handle,
  * (c) monitor / wait_for — the parent polls/awaits the handles while they run,
  * (e) the ``ExpertInvoker`` seam — each child crosses the clio-core mailbox transport.

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


async def test_async_fanout_real_alcf_children(tmp_path):
    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")
    lm = create_lm(cfg)

    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store)

    async def handler(req: ExpertRequest) -> ExpertResult:
        # a REAL ALCF completion answers the child's question
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            pred = await asyncio.to_thread(
                lambda: dspy.Predict("question -> answer")(question=req.question)
            )
        return ExpertResult(expert_id=req.expert_id, answer=str(getattr(pred, "answer", "") or ""))

    # a worker pool drains the mailbox concurrently — three children run in parallel
    stop = asyncio.Event()
    workers = [
        asyncio.ensure_future(run_worker(mailbox, handler, stop=stop, worker_id=f"w{i}", poll=0.05))
        for i in range(3)
    ]
    invoker = CEEExpertInvoker(mailbox, timeout=300, poll=0.05)
    tasks = BackgroundTasks()

    cases = [
        ("What is 2 + 2? Answer with only the number.", "4"),
        ("What is the capital of France? Answer with one word.", "paris"),
        ("What is 10 * 5? Answer with only the number.", "50"),
    ]

    try:
        # (b) fan out: each delegation becomes an independent monitored handle, all in flight
        handles = [
            spawn_invocation(tasks, invoker, ExpertRequest("data", q), label=f"q{i}")
            for i, (q, _) in enumerate(cases)
        ]

        # (c) monitor while in flight: at least one handle reaches RUNNING before any finish
        running_seen = False
        for _ in range(200):
            statuses = [tasks.status(h) for h in handles]
            if any(s is TaskStatus.RUNNING for s in statuses):
                running_seen = True
            if all(tasks.get(h).done.is_set() for h in handles):
                break
            await asyncio.sleep(0.05)
        assert running_seen, "never observed a child in RUNNING — fan-out did not go async"

        # wait for every child and fold the answers back
        results = [await tasks.wait(h, timeout=300) for h in handles]
    finally:
        stop.set()
        for w in workers:
            w.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*workers, return_exceptions=True)

    for rec, (q, needle) in zip(results, cases, strict=True):
        assert rec.status is TaskStatus.COMPLETED, f"{q!r} -> {rec.status} {rec.error}"
        answer = str(getattr(rec.result, "answer", "") or "")
        assert needle in answer.lower(), f"{q!r} -> {answer!r} (wanted {needle!r})"

    # the mailbox drained clean — every req/res/claim consumed
    assert [n for n, _ in store.scan("context", "cee_")] == []
