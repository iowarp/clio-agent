"""The gact prediction <-> ExpertResult mapping crosses the detached seam intact
(epic #667, #671/#441).

Proves a real-shaped child prediction (answer + routing decision + workflow_state)
survives a JSON wire round-trip through the loopback invoker — the integration's
core, isolated from the load-bearing settle loop.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

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
    base = dict(
        answer="the data shows X",
        next_expert="analysis",
        next_task="quantify X",
        expert_handoffs='[{"agent_id": "analysis", "question": "quantify X"}]',
        workflow_state={"stage": "data_collected", "rows": 128},
    )
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


async def test_run_child_cee_crosses_the_mailbox(tmp_path):
    """CEE mode runs the child behind the clio-core mailbox transport: the request and
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
        mode="cee",
        store=store,
    )
    assert seen["prompt"] == "q"
    assert out.answer == "the data shows X"
    assert out.next_expert == "analysis"
    assert out.next_task == "quantify X"
    assert "analysis" in out.expert_handoffs  # routing decision survived the mailbox
    assert out.workflow_state["rows"] == 128
    # the mailbox drained clean — no leaked req/res/claim blobs
    assert [n for n, _ in store.scan("context", "cee_")] == []
