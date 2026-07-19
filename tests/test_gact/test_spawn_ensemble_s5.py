"""S5 part 1 (#948 / #953): parallel fan-out + concurrent same-child ensembles +
deterministic request-order workflow_state merge.

Three layers are exercised:

* **Merge helper** (``workflow_state.merge.merge_run_workflow_states``) — pure,
  deterministic request-order (run_index) merge with typed conflict rows.
* **Ensemble substrate** (``turn_spawn.spawn_child_turn``) — the SAME declared child
  spawned N times concurrently: N task records with run_index 0/1/2, N distinct child
  sessions, overlapping run windows, FIFO queue admission at cap, cancel cascade, and
  per-child ``tool_call_ledger`` attribution under interleaving.
* **Wait aggregation** (``spawn_runtime.wait_agent_tasks``) — collects an ensemble's
  results, merges their workflow_state in request order, and surfaces conflicts.

The concurrency proofs use REAL threads on the dedicated per-depth pool with slow
stub children (never mocks of time); the merge determinism is sabotage-checked with
a reversed-completion/arrival harness (completion-order last-writer → red).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agent_tasks import STATUS_RUNNING, AgentTask, AgentTaskRegistry
from clio_agent.gact.app import build_app
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.gact.turn_spawn import TaskSpec, spawn_child_turn_threadsafe
from clio_agent.gact.types import Message, Part
from clio_agent.gact.workflow_state.merge import (
    MERGE_CONFLICT_REASON,
    RunWorkflowState,
    merge_run_workflow_states,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")


# ===========================================================================
# Part A — the pure request-order merge helper.
# ===========================================================================


def _run(run_index: int, state: dict, task_id: str = "") -> RunWorkflowState:
    return RunWorkflowState(
        run_index=run_index, task_id=task_id or f"task_{run_index}", workflow_state=state
    )


def test_merge_request_order_highest_run_index_wins() -> None:
    runs = [
        _run(0, {"target": {"status": "found", "src": "run0"}}),
        _run(1, {"target": {"status": "found", "src": "run1"}}),
        _run(2, {"target": {"status": "found", "src": "run2"}}),
    ]
    merged, conflicts = merge_run_workflow_states(runs)
    # Request-order last-writer: the highest run_index wins the colliding key.
    assert merged == {"target": {"status": "found", "src": "run2"}}
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["reason"] == MERGE_CONFLICT_REASON
    assert conflict["key"] == "target"
    assert conflict["winner"] == {"run_index": 2, "task_id": "task_2"}
    # Both earlier runs supplied a DIFFERENT value → both are losers, in request order.
    assert conflict["loser_runs"] == [
        {"run_index": 0, "task_id": "task_0"},
        {"run_index": 1, "task_id": "task_1"},
    ]


def test_merge_independent_of_arrival_order_sabotage_lock() -> None:
    """The winner is the highest RUN_INDEX regardless of the order the runs ARRIVE in
    (completion order). Sabotage: an implementation that used arrival/list order
    (completion-order last-writer) would pick a different winner here — this harness
    presents the runs in REVERSED (fastest-last) completion order, so a completion
    -order merge goes red while the request-order merge stays green."""

    forward = [
        _run(0, {"k": {"v": 0}}),
        _run(1, {"k": {"v": 1}}),
        _run(2, {"k": {"v": 2}}),
    ]
    reversed_completion = list(reversed(forward))  # run 2 arrives FIRST, run 0 LAST
    merged_fwd, conflicts_fwd = merge_run_workflow_states(forward)
    merged_rev, conflicts_rev = merge_run_workflow_states(reversed_completion)
    # Identical result independent of arrival order — run_index 2 always wins.
    assert merged_fwd == merged_rev == {"k": {"v": 2}}
    assert conflicts_fwd == conflicts_rev
    assert conflicts_rev[0]["winner"]["run_index"] == 2


def test_merge_no_conflict_when_runs_agree() -> None:
    runs = [
        _run(0, {"shared": {"x": 1}, "only0": {"y": 2}}),
        _run(1, {"shared": {"x": 1}}),
        _run(2, {"shared": {"x": 1}, "only2": {"z": 3}}),
    ]
    merged, conflicts = merge_run_workflow_states(runs)
    # Agreeing key → no conflict; disjoint keys → union.
    assert conflicts == []
    assert merged == {"shared": {"x": 1}, "only0": {"y": 2}, "only2": {"z": 3}}


def test_merge_value_equality_is_key_order_insensitive() -> None:
    # Two dicts with the same content in a different key order are NOT a conflict.
    runs = [_run(0, {"s": {"a": 1, "b": 2}}), _run(1, {"s": {"b": 2, "a": 1}})]
    _merged, conflicts = merge_run_workflow_states(runs)
    assert conflicts == []


def test_merge_partial_conflict_only_disagreeing_runs_are_losers() -> None:
    # run0 and run2 agree; run1 disagrees. Winner is run2 (highest index); the only
    # loser is run1 (run0 agrees with the winner so it is NOT a loser).
    runs = [
        _run(0, {"k": {"v": "A"}}),
        _run(1, {"k": {"v": "B"}}),
        _run(2, {"k": {"v": "A"}}),
    ]
    merged, conflicts = merge_run_workflow_states(runs)
    assert merged == {"k": {"v": "A"}}
    assert len(conflicts) == 1
    assert conflicts[0]["winner"]["run_index"] == 2
    assert conflicts[0]["loser_runs"] == [{"run_index": 1, "task_id": "task_1"}]


# ===========================================================================
# Part B — the ensemble substrate (real child turns on the per-depth pool).
# ===========================================================================


class _RecordingAgent:
    """A stub host agent whose forward sleeps (so concurrent runs overlap) and records
    its run window + the ACTIVE tool session id the observer would attribute its tool
    calls to (``_ctx.active_tool_session_id()``), appending a ledger row exactly as the
    live tool observer does — proving per-child attribution under interleaving."""

    def __init__(self, sleep_s: float = 0.0) -> None:
        self.sleep_s = sleep_s
        self.windows: list[tuple[str, float, float]] = []
        # (session_id kwarg, tool_session the observer would attribute to) per forward.
        self.attributions: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.app: Any = None

    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        start = time.monotonic()
        # The tool session id the live observer keys the ledger on — bound per child
        # turn by _tool_session_context(child_sid) and inherited here via the forward's
        # copied context. Under an ensemble each concurrent forward must see its OWN
        # child sid, never a sibling's. Append a ledger row exactly as the live tool
        # observer does; the turn finalize drains it onto THIS child's assistant message.
        tool_sid = ctx.active_tool_session_id()
        with self._lock:
            self.attributions.append((session_id, tool_sid))
        if self.app is not None:
            ledger = getattr(self.app.state, "tool_call_ledger", None)
            if ledger is not None:
                ledger.setdefault(tool_sid, []).append(
                    {"name": "probe_tool", "attributed_session": tool_sid}
                )
        if self.sleep_s:
            time.sleep(self.sleep_s)
        end = time.monotonic()
        with self._lock:
            self.windows.append((session_id, start, end))
        return type(
            "P",
            (),
            {"answer": f"child {session_id}", "selected_expert": "", "routing_rationale": ""},
        )()


def _declare(monkeypatch: Any, *child_ids: str) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda app, pid, session_id="": set(child_ids),
    )


def _wait_terminal(app: Any, task_id: str, timeout: float = 15.0) -> AgentTask:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = app.state.agent_task_registry.get(task_id)
        if t is not None and t.is_terminal:
            return t
        time.sleep(0.02)
    return app.state.agent_task_registry.get(task_id)


def _bus(app: Any, sid: str, etype: str) -> list[Any]:
    return [e for e in app.state.bus._history.get(sid, []) if e.type == etype]


def _spawn_ensemble(app: Any, parent: str, child: str, n: int) -> list[AgentTask]:
    """Spawn the SAME declared child ``n`` times from one parent turn (an ensemble)."""

    return [
        spawn_child_turn_threadsafe(
            app,
            TaskSpec(
                child_expert_id=child,
                task_text=f"run {i}",
                parent_session_id=parent,
                requesting_expert_id="main",
            ),
        )
        for i in range(n)
    ]


def test_same_child_ensemble_three_distinct_records_sessions_and_run_indexes(
    tmp_path: Path, monkeypatch
) -> None:
    """Spawning the SAME declared child 3× in one parent turn yields 3 task records
    with run_index 0/1/2, 3 DISTINCT child sessions, and 3 started+completed event
    pairs on the parent channel."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_RecordingAgent())
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        spawned = _spawn_ensemble(app, parent, "main", 3)
        # Ensemble run indexes assigned in spawn order, durable on the record.
        assert [t.run_index for t in spawned] == [0, 1, 2]
        # Three UNIQUE task records + three UNIQUE child sessions (task ids unique).
        assert len({t.task_id for t in spawned}) == 3
        assert len({t.child_session_id for t in spawned}) == 3

        settled = [_wait_terminal(app, t.task_id) for t in spawned]
        assert [s.status for s in settled] == ["completed"] * 3
        # run_index survived the queued→running→completed lifecycle (durable).
        assert sorted(s.run_index for s in settled) == [0, 1, 2]

        # Three started + three completed operational events on the PARENT channel.
        started = _bus(app, parent, "agent.task.started")
        completed = _bus(app, parent, "agent.task.completed")
        assert len(started) == 3, [e.payload.get("run_index") for e in started]
        assert len(completed) == 3
        # Every started event carries its run_index.
        assert sorted(e.payload["run_index"] for e in started) == [0, 1, 2]


def test_ensemble_of_three_runs_concurrently_overlapping_windows(
    tmp_path: Path, monkeypatch
) -> None:
    """A same-child ensemble of 3 with cap>=3 runs 3 CONCURRENT child turns: their run
    windows overlap (there is an instant all three are executing). Proof uses real
    threads on the dedicated depth pool with slow stub children — max(starts) <
    min(ends) is impossible under sequential execution (each 0.5s child would finish
    before the next starts)."""

    _declare(monkeypatch, "main")
    agent = _RecordingAgent(sleep_s=0.5)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.app = app
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        spawned = _spawn_ensemble(app, parent, "main", 3)
        for t in spawned:
            assert _wait_terminal(app, t.task_id).status == "completed"

        assert len(agent.windows) == 3
        starts = [w[1] for w in agent.windows]
        ends = [w[2] for w in agent.windows]
        # All three intervals share a common instant → genuinely concurrent.
        assert max(starts) < min(ends), (
            f"runs did not overlap (sequential?): windows={agent.windows}"
        )
        # And the whole ensemble finished in ~one child's time, not three.
        wall = max(ends) - min(starts)
        assert wall < 3 * 0.5, f"wall {wall:.2f}s ~ serialized, not concurrent"


def test_ledger_rows_attribute_to_each_child_session_under_interleaving(
    tmp_path: Path, monkeypatch
) -> None:
    """Under a concurrent ensemble each child turn's tool call attributes to ITS OWN
    child session — the per-turn tool-session binding is isolated (contextvars copy per
    child turn), so interleaved forwards never cross-attribute — and the observer's
    ledger row lands on THAT child's assistant message (drained at the child's finalize),
    never the parent's.

    The ledger dict is drained per turn (finalize pops it onto the assistant message),
    so the durable evidence is (a) the tool-session each forward SAW == its own child
    sid and (b) the child's persisted ``tools_called`` metadata carrying the probe row."""

    _declare(monkeypatch, "main")
    agent = _RecordingAgent(sleep_s=0.4)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.app = app
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        spawned = _spawn_ensemble(app, parent, "main", 3)
        for t in spawned:
            assert _wait_terminal(app, t.task_id).status == "completed"

        child_sids = {t.child_session_id for t in spawned}
        # (a) Every forward saw its OWN child sid as the tool session (no cross-attribution
        # under concurrency); the set of attributed sessions is exactly the three children.
        assert len(agent.attributions) == 3
        for session_id, tool_sid in agent.attributions:
            assert tool_sid == session_id, f"forward for {session_id} attributed to {tool_sid}"
        assert {tool_sid for _s, tool_sid in agent.attributions} == child_sids

        # (b) The probe ledger row landed on EACH child's assistant message (drained at
        # that child's finalize) — never on the parent session.
        for t in spawned:
            child_msgs = app.state.messages.get(t.child_session_id, []) or []
            finals = [m for m in child_msgs if getattr(m, "role", "") == "assistant"]
            tools_called = (getattr(finals[-1], "metadata", {}) or {}).get("tools_called", [])
            names = {row.get("name") for row in tools_called}
            assert "probe_tool" in names, f"child {t.child_session_id} tools_called={tools_called}"
        parent_msgs = app.state.messages.get(parent, []) or []
        for m in parent_msgs:
            names = {
                r.get("name") for r in (getattr(m, "metadata", {}) or {}).get("tools_called", [])
            }
            assert "probe_tool" not in names, "a child's tool row leaked onto the parent message"


def test_ensemble_queue_admission_fifo_per_depth(tmp_path: Path, monkeypatch) -> None:
    """At cap=1 an ensemble of 3 admits run 0, queues runs 1 & 2 (concurrency_cap),
    then admits them FIFO (run_index order) as slots free — all three complete."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_RecordingAgent(sleep_s=0.3))
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 1
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        spawned = _spawn_ensemble(app, parent, "main", 3)
        assert spawned[0].status == STATUS_RUNNING
        assert [t.status for t in spawned[1:]] == ["queued", "queued"]
        assert all(t.queued_reason == "concurrency_cap" for t in spawned[1:])
        assert [t.run_index for t in spawned] == [0, 1, 2]

        for t in spawned:
            assert _wait_terminal(app, t.task_id).status == "completed"

        # FIFO per depth: the started events fire in run_index order (0 then 1 then 2).
        started = _bus(app, parent, "agent.task.started")
        assert [e.payload["run_index"] for e in started] == [0, 1, 2]


def test_cancel_cascade_kills_all_ensemble_runs(tmp_path: Path, monkeypatch) -> None:
    """Cancelling the parent cancels EVERY run of a concurrent ensemble (the cascade
    iterates for_parent, which lists all task records regardless of shared child id)."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_RecordingAgent(sleep_s=3.0))
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 3
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        spawned = _spawn_ensemble(app, parent, "main", 3)
        assert all(t.status == STATUS_RUNNING for t in spawned)
        assert client.post(f"/v1/sessions/{parent}/cancel").status_code == 204

        for t in spawned:
            assert _wait_terminal(app, t.task_id, timeout=8.0).status == "cancelled"
        # One cascade-cancel event per run on the parent channel.
        assert len(_bus(app, parent, "agent.task.cancelled")) == 3


# ===========================================================================
# Part C — wait aggregation: request-order merge + typed conflict rows + run_index
# on the return Parts. Bare fake app (like the S4 wire-parity tests).
# ===========================================================================


@contextmanager
def _active_turn(app: Any, session_id: str = "sess_x") -> Iterator[None]:
    with _gact_app_context(app):
        token = ctx.set_session_id(session_id)
        try:
            yield
        finally:
            ctx.reset(token)


class _Agent:
    def forward(self, question: str, session_id: str) -> Any:
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


class _Def:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.metadata = {"agent_blueprint_id": "bp"}


class _StubSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, SimpleNamespace] = {}

    def get(self, sid: str) -> Any:
        return self._sessions.get(sid)

    def update(self, sid: str, *, metadata_patch: dict | None = None, **_kw: Any) -> Any:
        sess = self._sessions.get(sid) or SimpleNamespace(id=sid, metadata={})
        sess.metadata.update(metadata_patch or {})
        self._sessions[sid] = sess
        return sess


def _fake_app(registry: AgentTaskRegistry, messages: dict[str, list[Message]]) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            agent_task_registry=registry, sessions=_StubSessions(), messages=dict(messages)
        )
    )


def _assistant_message(msg_id: str, sid: str, text: str) -> Message:
    return Message(
        id=msg_id,
        session_id=sid,
        role="assistant",
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
        parts=[Part(type="text", text=text)],
    )


def _ensemble_task(run_index: int, wf: dict) -> AgentTask:
    return AgentTask(
        task_id=f"task_run{run_index}",
        parent_session_id="sess_x",
        child_session_id=f"child_{run_index}",
        agent_ref={"expert_id": "worker", "requesting_expert_id": "main"},
        run_index=run_index,
        status="completed",
        result={
            "answer_excerpt": f"run {run_index}",
            "workflow_state": wf,
            "message_ref": f"msg_{run_index}",
        },
    )


def _capture_emits(monkeypatch) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.spawn_runtime._emit_semantic_event",
        lambda app, sid, event_type, **kw: emitted.append({"event_type": event_type, **kw}) or {},
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.spawn_runtime._append_live_assistant_part",
        lambda app, sid, part: None,
    )
    return emitted


def _capture_parts(monkeypatch) -> list[Part]:
    parts: list[Part] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.spawn_runtime._append_live_assistant_part",
        lambda app, sid, part: parts.append(part),
    )
    return parts


def _wait_tool(app: Any, monkeypatch) -> Any:
    from clio_agent.gact.agents import spawn_runtime

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": {"worker"},
    )
    tools = spawn_runtime.build_spawn_runtime_tools(_Agent(), _Def("main"))
    return {t.name: t for t in tools}["wait_agent_tasks"]


def _seed_ensemble_registry(wfs: dict[int, dict]) -> tuple[AgentTaskRegistry, dict]:
    registry = AgentTaskRegistry()
    messages = {}
    for run_index, wf in wfs.items():
        registry.register(_ensemble_task(run_index, wf))
        messages[f"child_{run_index}"] = [
            _assistant_message(f"msg_{run_index}", f"child_{run_index}", f"run {run_index}")
        ]
    return registry, messages


def test_wait_merges_ensemble_workflow_state_in_request_order_with_conflict_rows(
    monkeypatch,
) -> None:
    import json

    wfs = {
        0: {"target": {"status": "found", "src": "run0"}, "shared": {"ok": True}},
        1: {"target": {"status": "found", "src": "run1"}, "shared": {"ok": True}},
        2: {"target": {"status": "found", "src": "run2"}, "shared": {"ok": True}},
    }
    registry, messages = _seed_ensemble_registry(wfs)
    app = _fake_app(registry, messages)
    _capture_emits(monkeypatch)

    with _active_turn(app):
        wait = _wait_tool(app, monkeypatch)
        result = json.loads(
            wait.func(task_ids=["task_run0", "task_run1", "task_run2"], timeout_s=1.0)
        )

    # Deterministic request-order merge: highest run_index wins the colliding key.
    assert result["merged_workflow_state"]["target"] == {"status": "found", "src": "run2"}
    assert result["merged_workflow_state"]["shared"] == {"ok": True}
    conflicts = result["workflow_state_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "workflow_state_merge_conflict"
    assert conflicts[0]["key"] == "target"
    assert conflicts[0]["winner"] == {"run_index": 2, "task_id": "task_run2"}
    assert conflicts[0]["loser_runs"] == [
        {"run_index": 0, "task_id": "task_run0"},
        {"run_index": 1, "task_id": "task_run1"},
    ]
    # Each per-run row is still returned individually with its run_index.
    assert sorted(r["run_index"] for r in result["results"]) == [0, 1, 2]


def test_wait_merge_is_completion_order_independent_sabotage_lock(monkeypatch) -> None:
    """The merged winner is the highest RUN_INDEX even when the tasks are COLLECTED in
    reversed (fastest-completed-first) order. Sabotage: a wait aggregation that merged
    in arrival/collection order would let run 0 win here → red."""

    import json

    wfs = {
        0: {"k": {"v": 0}},
        1: {"k": {"v": 1}},
        2: {"k": {"v": 2}},
    }
    registry, messages = _seed_ensemble_registry(wfs)
    app = _fake_app(registry, messages)
    _capture_emits(monkeypatch)

    with _active_turn(app):
        wait = _wait_tool(app, monkeypatch)
        # Collect in REVERSED order — run 2 first, run 0 last.
        result = json.loads(
            wait.func(task_ids=["task_run2", "task_run1", "task_run0"], timeout_s=1.0)
        )

    assert result["merged_workflow_state"]["k"] == {"v": 2}, "arrival order leaked into the merge"
    assert result["workflow_state_conflicts"][0]["winner"]["run_index"] == 2


def test_ensemble_return_parts_carry_run_index(monkeypatch) -> None:
    import json

    wfs = {0: {"k": {"v": 0}}, 1: {"k": {"v": 1}}}
    registry, messages = _seed_ensemble_registry(wfs)
    app = _fake_app(registry, messages)
    _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        wait = _wait_tool(app, monkeypatch)
        json.loads(wait.func(task_ids=["task_run0", "task_run1"], timeout_s=1.0))

    # One return Part per run, each carrying its own run_index (ensemble identity).
    handoffs = [p for p in parts if p.type == "expert_handoff"]
    assert len(handoffs) == 2
    assert sorted(p.metadata["run_index"] for p in handoffs) == [0, 1]
    assert all(p.child_agent == "worker" for p in handoffs)
