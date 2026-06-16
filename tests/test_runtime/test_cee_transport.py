"""Capability (d): clio-to-clio delegation over a clio-core context store
(epic #667, #659).

Two parties on the SAME machine exchange a delegation through clio-core — sharing
ONLY the store, no in-memory objects. The offline case rides LocalFS (fast); the
integration case rides real clio-core CTE (boots the in-process runtime). On a
cluster the same store spans nodes and the identical code is cross-machine.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from clio_agent.arc.storage import make_arc_store
from clio_agent.runtime.background_tasks import BackgroundTasks, TaskStatus
from clio_agent.runtime.cee_transport import (
    CEEExpertInvoker,
    CEEMailbox,
    run_worker,
)
from clio_agent.runtime.expert_invoker import (
    ExpertRequest,
    ExpertResult,
    spawn_invocation,
)


async def _child_handler(req: ExpertRequest) -> ExpertResult:
    # the "remote" worker ran the child and produced this
    return ExpertResult(
        expert_id=req.expert_id,
        answer=f"handled:{req.question}",
        workflow_state={"via": "clio-core", "scope": req.scope},
    )


async def _exchange(store) -> ExpertResult:
    """Party A submits + waits; Party B independently drains the mailbox. They share
    only `store`."""
    mailbox = CEEMailbox(store, prefix="cee_t_")
    invoker = CEEExpertInvoker(mailbox, timeout=10)
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(mailbox, _child_handler, stop=stop))
    try:
        return await invoker.invoke(
            ExpertRequest("data", "find X", session_id="s1", scope="agentA/data")
        )
    finally:
        stop.set()
        await worker


async def test_two_parties_exchange_via_shared_store_localfs(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    result = await _exchange(store)
    assert result.answer == "handled:find X"
    assert result.workflow_state == {"via": "clio-core", "scope": "agentA/data"}


async def test_mailbox_pending_discovers_unanswered_requests(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_p_")
    rid = mailbox.submit(ExpertRequest("data", "q1"))
    # a worker that never saw the request in memory still finds it via the store
    assert mailbox.pending() == [rid]
    await serve_one_compat(mailbox, rid)
    assert mailbox.pending() == []  # answered -> no longer pending
    assert mailbox.read_result(rid).answer == "handled:q1"


async def serve_one_compat(mailbox, rid):
    from clio_agent.runtime.cee_transport import serve_one

    await serve_one(mailbox, rid, _child_handler)


async def test_cee_invoker_composes_with_background_monitor(tmp_path):
    """(b)+(c)+(d): a clio-to-clio child runs as a monitored background task."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_bg_")
    invoker = CEEExpertInvoker(mailbox, timeout=10)
    tasks = BackgroundTasks()
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(mailbox, _child_handler, stop=stop))
    try:
        tid = spawn_invocation(tasks, invoker, ExpertRequest("data", "bg work"), label="data")
        rec = await tasks.wait(tid, timeout=10)
        assert rec.status is TaskStatus.COMPLETED
        assert rec.result.answer == "handled:bg work"
    finally:
        stop.set()
        await worker


async def test_successful_delegation_drains_the_mailbox(tmp_path):
    """A completed delegation leaves NO req/res/claim behind (the leak fix: invoke
    discards on success, so the mailbox doesn't grow without bound)."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_drain_")
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(mailbox, _child_handler, stop=stop))
    try:
        res = await CEEExpertInvoker(mailbox, timeout=5).invoke(ExpertRequest("data", "q"))
        assert res.answer == "handled:q"
    finally:
        stop.set()
        await worker
    assert list(store.scan("context", "cee_drain_")) == []  # nothing leaked


# ---- failure modes (found by the depth gap analysis; confirmed by probe) ----


async def test_handler_exception_drains_as_failed_not_hang(tmp_path):
    """A child that raises must NOT hang the parent to timeout — it gets a failed
    result back (the bug: serve_one let the exception propagate, parent hung)."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_f_")

    async def bad(req):
        raise RuntimeError("child exploded")

    invoker = CEEExpertInvoker(mailbox, timeout=2)
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(mailbox, bad, stop=stop))
    try:
        result = await invoker.invoke(ExpertRequest("data", "q"))
        assert result.status == "failed"
        assert "child exploded" in (result.error or "")
    finally:
        stop.set()
        await worker


async def test_worker_survives_a_failing_child(tmp_path):
    """One failing child must not kill the worker loop — the next delegation works."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_s_")

    async def handler(req):
        if req.question == "boom":
            raise RuntimeError("boom")
        return ExpertResult(expert_id=req.expert_id, answer=f"ok:{req.question}")

    invoker = CEEExpertInvoker(mailbox, timeout=2)
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(mailbox, handler, stop=stop))
    try:
        r1 = await invoker.invoke(ExpertRequest("data", "boom"))
        assert r1.status == "failed"
        r2 = await invoker.invoke(ExpertRequest("data", "good"))  # worker still alive
        assert r2.answer == "ok:good"
    finally:
        stop.set()
        await worker
    assert not worker.cancelled()


async def test_corrupted_request_blob_drains_as_failed(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_c_")
    store.put("context", "cee_c_badrid.req", b"{not valid json")  # poison blob

    from clio_agent.runtime.cee_transport import serve_one

    async def handler(req):
        return ExpertResult(expert_id="x", answer="should not run")

    res = await serve_one(mailbox, "cee_c_badrid", handler)
    assert res.status == "failed" and "corrupted" in (res.error or "")


async def test_timeout_discards_orphan_and_raises(tmp_path):
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_t_")
    invoker = CEEExpertInvoker(mailbox, timeout=0.2)  # no worker -> times out
    with pytest.raises(TimeoutError):
        await invoker.invoke(ExpertRequest("data", "q"))
    # the orphaned request blob must be cleaned up, not leaked in clio-core
    assert list(store.scan("context", "cee_t_")) == []


async def test_two_workers_single_result_intact(tmp_path):
    """Two workers on one mailbox: the published result is exactly-once and correct
    (execution is at-least-once; this asserts no corruption / no lost message)."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    mailbox = CEEMailbox(store, prefix="cee_2w_")

    async def handler(req):
        return ExpertResult(expert_id=req.expert_id, answer=f"done:{req.question}")

    invoker = CEEExpertInvoker(mailbox, timeout=3)
    stop = asyncio.Event()
    w1 = asyncio.ensure_future(run_worker(mailbox, handler, stop=stop, worker_id="w1"))
    w2 = asyncio.ensure_future(run_worker(mailbox, handler, stop=stop, worker_id="w2"))
    try:
        result = await invoker.invoke(ExpertRequest("data", "X"))
        assert result.answer == "done:X"
    finally:
        stop.set()
        await asyncio.gather(w1, w2)


@pytest.mark.integration
async def test_two_parties_exchange_via_clio_core_cte():
    """REAL clio-core: two parties share the in-process CTE runtime and hand off a
    delegation entirely through clio-core context. (True multi-PROCESS sharing needs
    a shared-daemon clio.yaml; the embedded runtime shares within a process.)"""
    store = make_arc_store(backend="cte")
    try:
        result = await _exchange(store)
        assert result.answer == "handled:find X"
        assert result.workflow_state["via"] == "clio-core"
    finally:
        store.clear()


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live ALCF run: set CLIO_RUN_LIVE=1 (+ Argonne auth + CLIO_LM_* env)",
)
async def test_concurrent_clio_to_clio_over_cte_real_alcf():
    """Real-deployment stress: N concurrent clio-to-clio delegations over REAL CTE,
    each child a REAL ALCF completion. Proves the clio-core transport carries
    concurrent traffic with no cross-talk / lost messages / orphans — the gap the
    single happy-path CTE test missed."""
    import uuid as _uuid

    import dspy

    from clio_agent.config import create_lm, load_config_from_env

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio")
    lm = create_lm(cfg)

    store = make_arc_store(backend="cte")
    mailbox = CEEMailbox(store, prefix="cee_stress_")

    async def handler(req: ExpertRequest) -> ExpertResult:
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            pred = dspy.Predict("instruction -> answer")(instruction=req.question)
        return ExpertResult(expert_id=req.expert_id, answer=str(getattr(pred, "answer", "") or ""))

    n = 4
    markers = [f"TKN{i}{_uuid.uuid4().hex[:6].upper()}" for i in range(n)]
    invoker = CEEExpertInvoker(mailbox, timeout=120)
    stop = asyncio.Event()
    workers = [
        asyncio.ensure_future(run_worker(mailbox, handler, stop=stop, worker_id=f"w{j}"))
        for j in range(2)
    ]
    try:
        async def one(marker: str):
            req = ExpertRequest("data", f"Reply with exactly this token and nothing else: {marker}")
            return marker, await invoker.invoke(req)

        results = await asyncio.gather(*[one(m) for m in markers])
        # each parent got ITS OWN marker back — no cross-talk between concurrent delegations
        for marker, res in results:
            assert res.status == "completed", f"{marker}: {res.error}"
            assert marker in res.answer, f"marker {marker} missing from {res.answer!r}"
        assert mailbox.pending() == []  # no orphaned requests left in clio-core
    finally:
        stop.set()
        await asyncio.gather(*workers)
        for name, _ in list(store.scan("context", "cee_stress_")):
            store.delete("context", name)
