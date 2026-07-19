"""S5 part 3 (#948 / #953 work item 4): declared deterministic workflows.

Four layers are exercised:

* **Declaration matrix** — a valid ``workflow:`` block parses; an unknown child, a
  dependency cycle, a malformed predicate, and an unproduced ``when_child_completed``
  each become a typed row error that composes with the S4 hierarchy rules
  (``validate_expert_hierarchy``) and disables the expert.
* **Runner determinism** — a->b->c executes in DECLARATION order over the real spawn
  substrate; each step is its own :class:`AgentTask` record; step b's task sees step
  a's accumulated ``workflow_state`` (injected through the substrate).
* **Typed stall** — an unmet ``when_state`` predicate stops the run with
  ``stalled{step, predicate, observed}`` (never a guess); a child that FAILS stalls
  rather than continuing. Sabotage: forcing the gate always-satisfied changes a stall
  into a completion, proving the gate is load-bearing.
* **``run_workflow`` gating** — the tool is present ONLY when the blueprint declares a
  workflow (mirroring the children-gated toolset) — plus the ``fanout.max_workers``
  admission bound (batch beyond the bound queues typed; unbounded when absent).

The runner/substrate proofs use REAL child turns on the dedicated per-depth pool with
a stub host agent (never mocks of the substrate).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agents import spawn_runtime
from clio_agent.gact.app import build_app
from clio_agent.gact.expert_packs import validate_expert_hierarchy
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.gact.types import AgentDef
from clio_agent.gact.workflows import (
    STALL_CHILD_FAILED,
    STALL_PREDICATE_UNSATISFIED,
    DeclaredWorkflow,
    StatePredicate,
    WorkflowStep,
    evaluate_step_gate,
    parse_workflow,
    run_declared_workflow,
    workflow_validation_errors,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")


# ===========================================================================
# Helpers
# ===========================================================================


def _wf_def(steps: list[dict], *, agent_id: str = "main", parent_id: str = "") -> AgentDef:
    # The workflow declaration lives on metadata (its home — no AgentDef field, #948 S5).
    return AgentDef(
        id=agent_id,
        title=agent_id,
        parent_id=parent_id,
        module={"kind": "react"},
        metadata={"workflow": {"steps": steps}},
    )


def _child_def(agent_id: str, parent_id: str = "main") -> AgentDef:
    return AgentDef(id=agent_id, title=agent_id, parent_id=parent_id, module={"kind": "react"})


def _declare(monkeypatch: Any, *child_ids: str) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda app, pid, session_id="": set(child_ids),
    )


def _errors_for(parent: AgentDef, children: list[AgentDef]) -> list[str]:
    """Full loader-path errors for ``parent`` after hierarchy validation."""

    rows = validate_expert_hierarchy([parent, *children])
    by_id = {row.id: row for row in rows}
    return by_id[parent.id].validation_errors


# ===========================================================================
# Part A — declaration matrix (pure loader validation)
# ===========================================================================


def test_valid_workflow_parses_and_expert_stays_enabled() -> None:
    steps = [
        {"id": "s_a", "child": "a", "task": "do a", "when_state": {"acq.status": {"exists": False}}},
        {"id": "s_b", "child": "b", "task": "do b", "when_child_completed": "a"},
        {"id": "s_c", "child": "c", "task": "do c", "when_child_completed": "b"},
    ]
    parent = _wf_def(steps)
    children = [_child_def("a"), _child_def("b"), _child_def("c")]
    assert _errors_for(parent, children) == []
    rows = validate_expert_hierarchy([parent, *children])
    assert next(r for r in rows if r.id == "main").enabled is True

    workflow = parse_workflow(parent)
    assert isinstance(workflow, DeclaredWorkflow)
    assert [s.child for s in workflow.steps] == ["a", "b", "c"]
    # exists predicate parsed typed; when_child_completed threaded.
    assert workflow.steps[0].when_state == (
        StatePredicate(field_path="acq.status", kind="exists", exists=False),
    )
    assert workflow.steps[1].when_child_completed == "a"


def test_unknown_child_is_typed_error_and_disables() -> None:
    parent = _wf_def([{"id": "s", "child": "ghost", "task": "x"}])
    errors = _errors_for(parent, [_child_def("a")])
    assert any("undeclared child 'ghost'" in e for e in errors), errors
    rows = validate_expert_hierarchy([parent, _child_def("a")])
    assert next(r for r in rows if r.id == "main").enabled is False


def test_dependency_cycle_is_typed_error() -> None:
    steps = [
        {"id": "s1", "child": "a", "when_child_completed": "b"},
        {"id": "s2", "child": "b", "when_child_completed": "a"},
    ]
    errors = _errors_for(_wf_def(steps), [_child_def("a"), _child_def("b")])
    assert any("dependency cycle" in e for e in errors), errors


def test_malformed_predicate_neither_exists_nor_equals_is_typed_error() -> None:
    steps = [{"id": "s", "child": "a", "when_state": {"acq.status": {"nope": 1}}}]
    errors = _errors_for(_wf_def(steps), [_child_def("a")])
    assert any("exactly one of 'exists' or 'equals'" in e for e in errors), errors


def test_malformed_predicate_non_bool_exists_is_typed_error() -> None:
    steps = [{"id": "s", "child": "a", "when_state": {"acq.status": {"exists": "yes"}}}]
    errors = _errors_for(_wf_def(steps), [_child_def("a")])
    assert any("'exists' must be a bool" in e for e in errors), errors


def test_when_child_completed_unproduced_is_typed_error() -> None:
    # 'b' is a declared child but no STEP produces it → the gate can never fire.
    steps = [{"id": "s", "child": "a", "when_child_completed": "b"}]
    errors = _errors_for(_wf_def(steps), [_child_def("a"), _child_def("b")])
    assert any("is never produced by any step" in e for e in errors), errors


def test_empty_steps_is_typed_error() -> None:
    parent = AgentDef(
        id="main", title="main", module={"kind": "react"}, metadata={"workflow": {"steps": []}}
    )
    errors = _errors_for(parent, [_child_def("a")])
    assert any("workflow.steps must be a non-empty list" in e for e in errors), errors


def test_no_workflow_block_yields_no_errors_and_no_parsed_workflow() -> None:
    parent = AgentDef(id="main", title="main", module={"kind": "react"})
    assert workflow_validation_errors(parent, {"a"}) == []
    assert parse_workflow(parent) is None


def test_equals_predicate_parses() -> None:
    steps = [{"id": "s", "child": "a", "when_state": {"impact.status": {"equals": "present"}}}]
    workflow = parse_workflow(_wf_def(steps))
    assert workflow is not None
    assert workflow.steps[0].when_state == (
        StatePredicate(field_path="impact.status", kind="equals", equals="present"),
    )


# ===========================================================================
# Part B — pure predicate evaluation (dotted path, exists/equals, completed)
# ===========================================================================


def _step(child: str, *, when_state=(), when_child="") -> WorkflowStep:
    return WorkflowStep(
        id=child, child=child, task="t", when_state=tuple(when_state), when_child_completed=when_child
    )


def test_gate_exists_false_holds_when_field_absent() -> None:
    step = _step("a", when_state=[StatePredicate("acq.status", "exists", exists=False)])
    assert evaluate_step_gate(step, {}, set()).satisfied is True


def test_gate_exists_true_unmet_surfaces_observed() -> None:
    step = _step("a", when_state=[StatePredicate("acq.ready", "exists", exists=True)])
    result = evaluate_step_gate(step, {"acq": {"status": "done"}}, set())
    assert result.satisfied is False
    assert result.predicate == {"when_state": {"acq.ready": {"exists": True}}}
    assert result.observed == {"field": "acq.ready", "exists": False}


def test_gate_equals_holds_and_unmet() -> None:
    hold = _step("a", when_state=[StatePredicate("impact.status", "equals", equals="present")])
    assert evaluate_step_gate(hold, {"impact": {"status": "present"}}, set()).satisfied is True
    miss = evaluate_step_gate(hold, {"impact": {"status": "absent"}}, set())
    assert miss.satisfied is False
    assert miss.observed == {"field": "impact.status", "found": True, "value": "absent"}


def test_gate_when_child_completed_gates_on_completed_set() -> None:
    step = _step("b", when_child="a")
    assert evaluate_step_gate(step, {}, set()).satisfied is False
    assert evaluate_step_gate(step, {}, {"a"}).satisfied is True


# ===========================================================================
# Part C — the runner over the real spawn substrate.
# ===========================================================================


class _WorkflowAgent:
    """Stub host agent: each child forward resolves its own expert id from the child
    session, records the question it received (to prove state threading), writes its
    per-expert typed ``workflow_state`` onto the child session (the substrate reads it
    back onto the task result), and returns an answer. A named expert can be forced to
    FAIL (raise) to exercise the child-failure stall."""

    def __init__(self, states: dict[str, dict], fail_experts: tuple[str, ...] = ()) -> None:
        self.states = states
        self.fail_experts = set(fail_experts)
        self.app: Any = None
        self.seen: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        sess = self.app.state.sessions.get(session_id) if self.app is not None else None
        raw_agent = getattr(sess, "agent", None)
        # The child session's agent rides as a plain dict in the store (not coerced to
        # AgentRef); read the id from either shape.
        expert = (
            raw_agent.get("id")
            if isinstance(raw_agent, dict)
            else getattr(raw_agent, "id", "")
        ) or ""
        with self._lock:
            self.seen.append((expert, question))
        if expert in self.fail_experts:
            raise RuntimeError(f"forced failure for {expert}")
        wf = self.states.get(expert, {})
        if wf and self.app is not None:
            self.app.state.sessions.update(session_id, metadata_patch={"workflow_state": wf})
        return type(
            "P", (), {"answer": f"{expert} done", "selected_expert": "", "routing_rationale": ""}
        )()


def _route_children(monkeypatch, *child_ids: str) -> None:
    """Make the workflow's child experts resolve to blueprint-runtime react agents.

    A spawned child turn resolves its agent via ``_resolve_runtime_dynamic_agent``;
    without this it would be ``not_implemented`` (only ``main``/``default`` are
    built-in executable). The returned AgentDef carries an ``agent_blueprint_id`` so
    it routes through the blueprint runtime, where the ``host_agent_executor`` fixture
    delegates its forward to the stub host agent (never a real LM)."""

    known = set(child_ids)

    def _resolve(app, agent_id, *, session_id="", workspace_id="", prompt_registry=None):
        if agent_id in known:
            return AgentDef(
                id=agent_id,
                title=agent_id,
                module={"kind": "react"},
                source="expert_pack",
                metadata={"agent_blueprint_id": "wf_bp"},
            )
        return None

    monkeypatch.setattr("clio_agent.gact.turn_forward._resolve_runtime_dynamic_agent", _resolve)


def _run_workflow_app(tmp_path: Path, agent: _WorkflowAgent, monkeypatch, *child_ids: str):
    _declare(monkeypatch, *child_ids)
    _route_children(monkeypatch, *child_ids)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.app = app
    return app


def test_runner_executes_a_b_c_in_order_with_per_step_records(tmp_path: Path, monkeypatch) -> None:
    steps = [
        {"id": "s_a", "child": "a", "task": "do a", "when_state": {"acq.status": {"exists": False}}},
        {"id": "s_b", "child": "b", "task": "do b", "when_child_completed": "a"},
        {"id": "s_c", "child": "c", "task": "do c", "when_child_completed": "b"},
    ]
    agent = _WorkflowAgent(
        states={
            "a": {"acq": {"status": "done", "src": "a"}},
            "b": {"impact": {"status": "ranked"}},
            "c": {"map": {"path": "/x.png"}},
        }
    )
    app = _run_workflow_app(tmp_path, agent, monkeypatch, "a", "b", "c")
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        record = run_declared_workflow(
            app, _wf_def(steps), parent, requesting_expert_id="main", request="LA fires"
        )

    assert record["status"] == "completed", record
    # a->b->c executed in declaration order, each its own step record.
    assert [s["step_id"] for s in record["steps"]] == ["s_a", "s_b", "s_c"]
    assert [s["child"] for s in record["steps"]] == ["a", "b", "c"]
    assert all(s["child_status"] == "completed" for s in record["steps"])
    # Each step is a real AgentTask (per-step records on the registry).
    task_ids = [s["task_id"] for s in record["steps"]]
    assert len(set(task_ids)) == 3
    reg = app.state.agent_task_registry
    assert {reg.get(tid).agent_ref["expert_id"] for tid in task_ids} == {"a", "b", "c"}
    # Accumulated typed workflow_state carries every step's contribution.
    assert record["workflow_state"] == {
        "acq": {"status": "done", "src": "a"},
        "impact": {"status": "ranked"},
        "map": {"path": "/x.png"},
    }
    # b's task template SAW a's accumulated workflow_state (injected by the substrate).
    seen = dict(agent.seen)
    assert "acq" in seen["b"] and "done" in seen["b"], seen["b"]
    # ...and c saw both a's and b's state.
    assert "impact" in seen["c"] and "ranked" in seen["c"], seen["c"]
    # The request grounds each step's task text.
    assert "LA fires" in seen["a"]


def test_runner_stalls_on_unmet_state_predicate(tmp_path: Path, monkeypatch) -> None:
    """s_a runs; s_b is gated on a field a never produced → typed predicate stall. The
    runner STOPS at s_b (c never runs) and returns the observed reality — never a
    guess."""

    steps = [
        {"id": "s_a", "child": "a", "task": "do a"},
        {"id": "s_b", "child": "b", "task": "do b", "when_state": {"acq.ready": {"exists": True}}},
        {"id": "s_c", "child": "c", "task": "do c", "when_child_completed": "b"},
    ]
    agent = _WorkflowAgent(states={"a": {"acq": {"status": "done"}}})
    app = _run_workflow_app(tmp_path, agent, monkeypatch, "a", "b", "c")
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        record = run_declared_workflow(app, _wf_def(steps), parent, requesting_expert_id="main")

    assert record["status"] == "stalled", record
    stall = record["stall"]
    assert stall["reason"] == STALL_PREDICATE_UNSATISFIED
    assert stall["step"] == "s_b"
    assert stall["predicate"] == {"when_state": {"acq.ready": {"exists": True}}}
    assert stall["observed"] == {"field": "acq.ready", "exists": False}
    # Only s_a ran; c was never reached (no silent continuation past the stall).
    assert [s["step_id"] for s in record["steps"]] == ["s_a"]
    assert "c" not in {e for e, _ in agent.seen}


def test_runner_stalls_on_child_failure_not_a_guess(tmp_path: Path, monkeypatch) -> None:
    steps = [
        {"id": "s_a", "child": "a", "task": "do a"},
        {"id": "s_b", "child": "b", "task": "do b", "when_child_completed": "a"},
        {"id": "s_c", "child": "c", "task": "do c", "when_child_completed": "b"},
    ]
    agent = _WorkflowAgent(states={"a": {"acq": {"status": "done"}}}, fail_experts=("b",))
    app = _run_workflow_app(tmp_path, agent, monkeypatch, "a", "b", "c")
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        record = run_declared_workflow(app, _wf_def(steps), parent, requesting_expert_id="main")

    assert record["status"] == "stalled", record
    stall = record["stall"]
    assert stall["reason"] == STALL_CHILD_FAILED
    assert stall["step"] == "s_b"
    assert stall["observed"]["child_status"] == "failed"
    # b's failed record is present (the step ran); c never ran (stall, not guess).
    assert [s["step_id"] for s in record["steps"]] == ["s_a", "s_b"]
    assert "c" not in {e for e, _ in agent.seen}


def test_gate_is_load_bearing_sabotage_lock(tmp_path: Path, monkeypatch) -> None:
    """Sabotage: force the gate ALWAYS-satisfied (as a runner that skipped an unmet
    predicate would). The same workflow that stalls under the real gate now COMPLETES —
    proving the predicate check is what enforces the stall (remove it → red here)."""

    from clio_agent.gact.workflows import GateResult

    steps = [
        {"id": "s_a", "child": "a", "task": "do a"},
        {"id": "s_b", "child": "b", "task": "do b", "when_state": {"acq.ready": {"exists": True}}},
    ]
    agent = _WorkflowAgent(states={"a": {"acq": {"status": "done"}}, "b": {"impact": {"ok": True}}})
    app = _run_workflow_app(tmp_path, agent, monkeypatch, "a", "b")
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        # Real gate → stall at s_b.
        stalled = run_declared_workflow(app, _wf_def(steps), parent, requesting_expert_id="main")
        assert stalled["status"] == "stalled"

        # Sabotage the gate to always-true → s_b now runs → completion.
        monkeypatch.setattr(
            "clio_agent.gact.workflows.evaluate_step_gate",
            lambda step, state, completed: GateResult(satisfied=True),
        )
        parent2 = client.post("/v1/sessions", json={"title": "p2"}).json()["id"]
        completed = run_declared_workflow(app, _wf_def(steps), parent2, requesting_expert_id="main")
        assert completed["status"] == "completed", completed


# ===========================================================================
# Part D — run_workflow tool gating + fanout.max_workers admission bound.
# ===========================================================================


class _Agent:
    def forward(self, question: str, session_id: str) -> Any:
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


@contextmanager
def _active(app: Any, session_id: str) -> Iterator[None]:
    with _gact_app_context(app):
        token = ctx.set_session_id(session_id)
        try:
            yield
        finally:
            ctx.reset(token)


def _tool_names(base_agent: Any, agent_def: AgentDef, app: Any, session_id: str, monkeypatch) -> set:
    _declare(monkeypatch, "a", "b", "c")
    with _active(app, session_id):
        tools = spawn_runtime.build_spawn_runtime_tools(base_agent, agent_def)
    return {t.name for t in tools}


def test_run_workflow_tool_present_only_when_workflow_declared(tmp_path: Path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        with_wf = _wf_def([{"id": "s", "child": "a", "task": "x"}])
        without_wf = AgentDef(id="main", title="main", module={"kind": "react"})

        names_with = _tool_names(_Agent(), with_wf, app, parent, monkeypatch)
        names_without = _tool_names(_Agent(), without_wf, app, parent, monkeypatch)

    assert "run_workflow" in names_with
    assert "run_workflow" not in names_without
    # The base spawn toolset is present in both cases.
    assert {"spawn_agent_task", "wait_agent_tasks", "spawn_agents_parallel"} <= names_without


def _parallel_tool(base_agent: Any, agent_def: AgentDef, app: Any, session_id: str, monkeypatch):
    _declare(monkeypatch, "w")
    _route_children(monkeypatch, "w")
    with _active(app, session_id):
        tools = spawn_runtime.build_spawn_runtime_tools(base_agent, agent_def)
    return {t.name: t for t in tools}["spawn_agents_parallel"]


def test_fanout_max_workers_bounds_batch_admission(tmp_path: Path, monkeypatch) -> None:
    """A parent declaring ``fanout.max_workers: 2`` fanning out 4 children (global cap
    3) admits exactly 2 RUNNING and QUEUES the other 2 with the typed ``concurrency_cap``
    reason — then drains them within the bound until all four complete."""

    from tests.test_gact.test_spawn_ensemble_s5 import _RecordingAgent, _wait_terminal

    agent = _RecordingAgent(sleep_s=0.6)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.app = app
    fan_def = AgentDef(
        id="main", title="main", module={"kind": "react"},
        fanout={"enabled": True, "max_workers": 2},
    )
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        with _active(app, parent):
            tool = _parallel_tool(_RecordingAgent(), fan_def, app, parent, monkeypatch)
            out = json.loads(tool.func(spawns=[{"agent": "w", "task": f"r{i}"} for i in range(4)]))

        reg = app.state.agent_task_registry
        tasks = [reg.get(s["task_id"]) for s in out["spawned"]]
        running = [t for t in tasks if t.status == "running"]
        queued = [t for t in tasks if t.status == "queued"]
        assert len(running) == 2, [t.status for t in tasks]
        assert len(queued) == 2, [t.status for t in tasks]
        assert all(t.queued_reason == "concurrency_cap" for t in queued)

        # Drain: admission honors the bound, all four eventually complete.
        for t in tasks:
            assert _wait_terminal(app, t.task_id, timeout=30.0).status == "completed"


def test_fanout_unbounded_when_absent_uses_global_depth_cap(tmp_path: Path, monkeypatch) -> None:
    """With NO fanout declaration the batch admits up to the GLOBAL per-depth cap (3):
    a batch of 4 → 3 running + 1 queued (concurrency_cap), not fanout-bounded."""

    from tests.test_gact.test_spawn_ensemble_s5 import _RecordingAgent, _wait_terminal

    agent = _RecordingAgent(sleep_s=0.6)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.app = app
    plain_def = AgentDef(id="main", title="main", module={"kind": "react"})
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        with _active(app, parent):
            tool = _parallel_tool(_RecordingAgent(), plain_def, app, parent, monkeypatch)
            out = json.loads(tool.func(spawns=[{"agent": "w", "task": f"r{i}"} for i in range(4)]))

        reg = app.state.agent_task_registry
        tasks = [reg.get(s["task_id"]) for s in out["spawned"]]
        assert sum(1 for t in tasks if t.status == "running") == 3, [t.status for t in tasks]
        assert sum(1 for t in tasks if t.status == "queued") == 1, [t.status for t in tasks]
        for t in tasks:
            assert _wait_terminal(app, t.task_id, timeout=30.0).status == "completed"
