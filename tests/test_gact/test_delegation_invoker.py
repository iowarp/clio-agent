"""The gact prediction <-> ExpertResult mapping crosses the detached seam intact
(epic #667, #671/#441).

Proves a real-shaped child prediction (answer + routing decision + workflow_state)
survives a JSON wire round-trip through the loopback invoker — the integration's
core, isolated from the load-bearing settle loop.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clio_agent.gact.delegation_invoker import (
    expert_request_for,
    expert_result_from_prediction,
    routing_from_result,
    run_child_via_boundary,
)
from clio_agent.runtime.expert_invoker import (
    ExpertResult,
    InProcessExpertInvoker,
    LoopbackExpertInvoker,
)


def _prediction(**kw):
    base = {
        "answer": "the data shows X",
        "next_expert": "analysis",
        "next_task": "quantify X",
        "expert_handoffs": '[{"agent_id": "analysis", "question": "quantify X"}]',
        "workflow_state": {"stage": "data_collected", "rows": 128},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_request_builder_pulls_agent_id():
    req = expert_request_for(
        SimpleNamespace(id="data"), "find X", session_id="s1", scope="agentA/data"
    )
    assert req.expert_id == "data" and req.question == "find X"
    assert req.session_id == "s1" and req.scope == "agentA/data"


def test_prediction_maps_answer_routing_and_workflow_state():
    res = expert_result_from_prediction(_prediction(), expert_id="data")
    assert res.answer == "the data shows X"
    assert res.workflow_state == {"stage": "data_collected", "rows": 128}
    routing = routing_from_result(res)
    assert routing["next_expert"] == "analysis"
    assert routing["next_task"] == "quantify X"
    assert "analysis" in routing["expert_handoffs"]


def test_mapping_is_json_serializable():
    res = expert_result_from_prediction(_prediction(), expert_id="data")
    # must survive a JSON round-trip with no loss (the detached wire)
    again = ExpertResult.from_wire(json.loads(json.dumps(res.to_wire())))
    assert again == res


async def test_real_prediction_crosses_loopback_intact():
    pred = _prediction()

    async def handler(req):
        # the "remote" worker ran the child and produced this prediction
        return expert_result_from_prediction(pred, expert_id=req.expert_id)

    req = expert_request_for(SimpleNamespace(id="data"), "find X", session_id="s1")
    in_proc = await InProcessExpertInvoker(handler).invoke(req)
    loop = await LoopbackExpertInvoker(handler).invoke(req)

    assert loop == in_proc  # detached == in-process
    assert loop.answer == "the data shows X"
    # the parent's routing decision survived the wire verbatim
    routing = routing_from_result(loop)
    assert routing["next_expert"] == "analysis"
    assert loop.workflow_state["rows"] == 128


def test_non_serializable_workflow_state_degrades_not_breaks():
    # a stray non-JSON object in workflow_state must not break the wire — it degrades
    # to a string so the rest of the result still crosses.
    pred = _prediction(workflow_state={"stage": "x", "obj": object()})
    res = expert_result_from_prediction(pred, expert_id="data")
    again = ExpertResult.from_wire(json.loads(json.dumps(res.to_wire())))
    assert again.workflow_state["stage"] == "x"
    assert isinstance(again.workflow_state["obj"], str)


def test_missing_fields_default_cleanly():
    res = expert_result_from_prediction(SimpleNamespace(answer="just an answer"), expert_id="solo")
    assert res.answer == "just an answer"
    assert res.workflow_state == {}
    assert routing_from_result(res)["next_expert"] == ""


async def test_run_child_default_mode_is_raw_parity():
    """Default mode returns the runner's prediction VERBATIM — the live path is
    behavior-identical (RULE 2: never break baseline)."""
    pred = _prediction()

    async def run_child(agent_def, prompt):
        return pred

    out = await run_child_via_boundary(
        SimpleNamespace(id="data"), "q", run_child=run_child, mode=""
    )
    assert out is pred  # identity preserved — no serialization, no field loss


async def test_run_child_loopback_crosses_the_wire():
    """Loopback mode runs the child behind the serializable boundary; the parent
    gets a prediction rebuilt from what crossed the JSON wire."""
    pred = _prediction()
    seen = {}

    async def run_child(agent_def, prompt):
        seen["prompt"] = prompt  # the question reached the 'remote' runner
        return pred

    out = await run_child_via_boundary(
        SimpleNamespace(id="data"), "q", run_child=run_child, session_id="s1", mode="loopback"
    )
    assert seen["prompt"] == "q"
    assert out.answer == "the data shows X"
    assert out.next_expert == "analysis"
    assert out.next_task == "quantify X"
    assert "analysis" in out.expert_handoffs  # routing decision survived
    assert out.workflow_state["rows"] == 128


async def test_run_child_clio_core_crosses_the_mailbox(tmp_path):
    """clio-core mode runs the child behind the clio-core mailbox transport: the request and
    result cross as blobs in an ARCStore, served by an in-process worker loop. The
    parent recovers a prediction rebuilt from what crossed the mailbox — answer and the
    full routing decision intact. (Same transport a cross-node worker uses.)"""
    from clio_agent.arc.storage import make_arc_store

    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    pred = _prediction()
    seen = {}

    async def run_child(agent_def, prompt):
        seen["prompt"] = prompt  # the question reached the worker that drained the mailbox
        return pred

    out = await run_child_via_boundary(
        SimpleNamespace(id="data"),
        "q",
        run_child=run_child,
        session_id="s1",
        mode="clio_core",
        store=store,
    )
    assert seen["prompt"] == "q"
    assert out.answer == "the data shows X"
    assert out.next_expert == "analysis"
    assert out.next_task == "quantify X"
    assert "analysis" in out.expert_handoffs  # routing decision survived the mailbox
    assert out.workflow_state["rows"] == 128
    # the mailbox drained clean — no leaked req/res/claim blobs
    assert [n for n, _ in store.scan("context", "clio_core_")] == []


async def test_clio_core_mode_cleans_owned_tmp_dir_when_store_creation_fails(monkeypatch):
    """When clio_core mode owns its throwaway LocalFS store and store construction fails (e.g. a
    mkdir on a read-only/full mount), the just-created temp dir must not orphan — the
    cleanup runs even though the delegation's main try/finally was never entered."""
    import glob

    import clio_agent.arc.storage as storage

    def boom(**_kwargs):
        raise OSError("simulated mkdir failure")

    monkeypatch.setattr(storage, "make_arc_store", boom)
    before = set(glob.glob("/tmp/clio_core_*"))

    async def run_child(agent_def, prompt):
        raise AssertionError("child must not run when the store could not be built")

    with pytest.raises(OSError):
        await run_child_via_boundary(
            SimpleNamespace(id="data"), "q", run_child=run_child, mode="clio_core"  # store=None -> owns it
        )
    assert set(glob.glob("/tmp/clio_core_*")) == before  # no orphaned temp dir


async def test_run_child_isolated_routes_to_a_detached_worker_pool(tmp_path):
    """clio_core_isolated mode is the DETACHED hinge: the parent does NOT run the child
    (``run_child`` is never called); it routes the request to a live isolated worker over a
    shared store and folds the worker's result back. Here an in-process isolated worker stands
    in for the separate-process worker (identical code path), proving the hinge drives
    ``IsolatedExpertInvoker`` and recovers answer + routing + workflow_state intact."""
    import asyncio

    from clio_agent.arc.storage import make_arc_store
    from clio_agent.runtime.clio_core_transport import run_isolated_worker

    store = make_arc_store(backend="local", data_dir=str(tmp_path))
    pred = _prediction()
    seen = {}

    async def worker_handler(req):
        # the DETACHED worker owns the child run; it produces the result the parent reads back
        seen["worker_q"] = req.question
        return expert_result_from_prediction(pred, expert_id=req.expert_id)

    async def parent_run_child(agent_def, prompt):
        raise AssertionError("isolated mode must NOT run the child on the parent side")

    stop = asyncio.Event()
    worker = asyncio.ensure_future(
        run_isolated_worker(store, worker_handler, role="data", worker_id="w1", stop=stop, poll=0.05)
    )
    try:
        # wait until the worker has announced presence so the parent can route to it
        from clio_agent.runtime.clio_core_transport import live_workers  # noqa: PLC0415

        for _ in range(200):
            if live_workers(store, "data"):
                break
            await asyncio.sleep(0.02)

        out = await run_child_via_boundary(
            SimpleNamespace(id="data"),
            "q",
            run_child=parent_run_child,
            session_id="s1",
            mode="clio_core_isolated",
            store=store,
            role="data",
        )
    finally:
        stop.set()
        worker.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await worker

    assert seen["worker_q"] == "q"  # the detached worker, not the parent, ran it
    assert out.answer == "the data shows X"
    assert out.next_expert == "analysis"
    assert "analysis" in out.expert_handoffs  # routing decision crossed the isolated mailbox
    assert out.workflow_state["rows"] == 128


async def test_isolated_mode_requires_a_shared_store():
    """The detached model can't rendezvous without a shared store — the hinge raises a clear
    error rather than silently degrading to an in-process run (RULE: surface reality)."""

    async def run_child(agent_def, prompt):
        raise AssertionError("must not run the child")

    with pytest.raises(ValueError, match="shared store"):
        await run_child_via_boundary(
            SimpleNamespace(id="data"), "q", run_child=run_child, mode="clio_core_isolated", store=None
        )
