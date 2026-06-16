"""Capability (d): clio-to-clio delegation over a clio-core context store
(epic #667, #659).

Two parties on the SAME machine exchange a delegation through clio-core — sharing
ONLY the store, no in-memory objects. The offline case rides LocalFS (fast); the
integration case rides real clio-core CTE (boots the in-process runtime). On a
cluster the same store spans nodes and the identical code is cross-machine.
"""

from __future__ import annotations

import asyncio

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
