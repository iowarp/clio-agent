"""S3 (#948 / #951): spawn_child_turn — children as REAL turns in REAL child
sessions, projected as AgentTasks.

Uses a stub agent (the child turn RUNS a real turn cycle — persist, finalize,
completion hook — the LM's answer is orthogonal to the substrate; the live gate
uses the real agent). Declared-children resolution is monkeypatched so the guard
has a child to accept.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import STATUS_FAILED, STATUS_RUNNING, AgentTask
from clio_agent.gact.app import build_app
from clio_agent.gact.turn_forward import _forward_executor
from clio_agent.gact.turn_spawn import (
    MAX_SPAWN_DEPTH,
    SpawnError,
    TaskSpec,
    _on_child_done,
    spawn_child_turn_threadsafe,
)


class _Agent:
    def __init__(self, sleep_s: float = 0.0) -> None:
        self.sleep_s = sleep_s

    def forward(self, question: str, session_id: str, **_kw):
        if self.sleep_s:
            time.sleep(self.sleep_s)
        return type(
            "P", (), {"answer": f"child did: {question[:20]}", "selected_expert": "", "routing_rationale": ""}
        )()


def _declare(monkeypatch, *child_ids: str) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda app, pid, session_id="": set(child_ids),
    )


def _wait_terminal(app, task_id: str, timeout: float = 10.0) -> AgentTask:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = app.state.agent_task_registry.get(task_id)
        if t is not None and t.is_terminal:
            return t
        time.sleep(0.05)
    return app.state.agent_task_registry.get(task_id)


def _bus(app, sid, etype):
    return [e for e in app.state.bus._history.get(sid, []) if e.type == etype]


def test_spawn_produces_child_session_and_completed_record(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        spec = TaskSpec(
            child_expert_id="main",
            task_text="analyze the dataset",
            parent_session_id=parent,
            requesting_expert_id="main",
            workflow_state={"plan": "P1"},
        )
        task = spawn_child_turn_threadsafe(app, spec)
        assert task.status == STATUS_RUNNING

        settled = _wait_terminal(app, task.task_id)
        assert settled.status == "completed", settled.status
        assert settled.result and settled.result.get("message_ref"), "no result message ref"
        assert "child did" in settled.result.get("answer_excerpt", "")

        # A real child session with parent lineage + the agent-task marker.
        child = app.state.sessions.get(settled.child_session_id)
        assert child.parent_session_id == parent
        assert child.metadata.get("session_type") == "agent_task"
        # Parent-visible completion event.
        assert _bus(app, parent, "agent.task.completed"), "no parent-visible completion event"


def test_depth_cap_rejected(tmp_path: Path, monkeypatch) -> None:
    # Unit guard on the backstop itself. The TOOL-PATH lock (that the tools actually
    # COMPUTE a depth that reaches this guard) lives in test_spawn_runtime_s4.py
    # (test_spawn_at_backstop_depth_rejected_through_tool_path).
    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        spec = TaskSpec(
            child_expert_id="data_expert",
            task_text="x",
            parent_session_id="sess_p",
            depth=MAX_SPAWN_DEPTH + 1,
        )
        with pytest.raises(SpawnError) as exc:
            spawn_child_turn_threadsafe(app, spec)
        assert exc.value.reason == "spawn_depth_exceeded"


def test_undeclared_child_rejected(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        spec = TaskSpec(child_expert_id="hpc_expert", task_text="x", parent_session_id="sess_p")
        with pytest.raises(SpawnError) as exc:
            spawn_child_turn_threadsafe(app, spec)
        assert exc.value.reason == "undeclared_child"


def test_queue_admission_at_cap(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(sleep_s=1.0))
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 1  # force the second spawn to queue
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        def _spec():
            return TaskSpec(child_expert_id="main", task_text="x", parent_session_id=parent)

        first = spawn_child_turn_threadsafe(app, _spec())
        second = spawn_child_turn_threadsafe(app, _spec())
        assert first.status == STATUS_RUNNING
        assert second.status == "queued" and second.queued_reason == "concurrency_cap"

        # When the first completes it admits the queued one (FIFO) — both terminal.
        assert _wait_terminal(app, first.task_id).status == "completed"
        assert _wait_terminal(app, second.task_id).status == "completed"


def test_cancel_cascade_from_parent(tmp_path: Path, monkeypatch) -> None:
    """Cancelling a parent session cascades to cancel its spawned child tasks."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(sleep_s=3.0))
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = spawn_child_turn_threadsafe(
            app, TaskSpec(child_expert_id="main", task_text="x", parent_session_id=parent)
        )
        assert task.status == STATUS_RUNNING
        assert client.post(f"/v1/sessions/{parent}/cancel").status_code == 204
        settled = _wait_terminal(app, task.task_id, timeout=6.0)
        assert settled.status == "cancelled", settled.status
        assert _bus(app, parent, "agent.task.cancelled"), "no cascade cancel event on parent"


def test_cancel_frees_slot_and_admits_queued(tmp_path: Path, monkeypatch) -> None:
    """Cancelling a parent frees its child's concurrency slot and admits a QUEUED
    task of ANOTHER parent — it must not strand forever (the completion hook won't
    admit a cascade-cancelled task, which is already terminal when its callback runs)."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(sleep_s=3.0))
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 1
        pa = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        pb = client.post("/v1/sessions", json={"title": "B"}).json()["id"]
        ta = spawn_child_turn_threadsafe(
            app, TaskSpec(child_expert_id="main", task_text="a", parent_session_id=pa)
        )
        tb = spawn_child_turn_threadsafe(
            app, TaskSpec(child_expert_id="main", task_text="b", parent_session_id=pb)
        )
        assert ta.status == STATUS_RUNNING
        assert tb.status == "queued"
        # Cancel parent A -> frees the only slot -> B's queued child is admitted.
        assert client.post(f"/v1/sessions/{pa}/cancel").status_code == 204
        settled_b = _wait_terminal(app, tb.task_id, timeout=8.0)
        assert settled_b.status == "completed", (
            f"queued task of another parent stranded: {settled_b.status}"
        )


def test_hitl_in_child_fails_typed(tmp_path: Path, monkeypatch) -> None:
    """An unattended child whose turn paused for user input fails with a typed
    reason (child_requires_user_input), never hangs."""

    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        # Seed a running task whose child session paused for user input.
        child = app.state.sessions.create(
            workspace_id="ws_default", title="c", parent_session_id="sess_p"
        )
        app.state.sessions.update(child.id, status="waiting_user")
        task = AgentTask(
            task_id="task_hitl",
            parent_session_id="sess_p",
            child_session_id=child.id,
            status=STATUS_RUNNING,
            created_at="2026-07-17T00:00:00+00:00",
            updated_at="2026-07-17T00:00:00+00:00",
        )
        app.state.agent_task_registry.register(task)
        _on_child_done(app, task.task_id, child.id, "async")
        settled = app.state.agent_task_registry.get(task.task_id)
        assert settled.status == STATUS_FAILED
        assert settled.error_reason == "child_requires_user_input"


# ---------------------------------------------------------------------------
# Fix 5 (#948 S4 adversarial review): per-depth child-forward pools so a nested
# orchestrator blocked in wait never starves its own deeper children.
# ---------------------------------------------------------------------------


def _child_session_at_depth(app, depth: int, sid_hint: str) -> str:
    """Create a child session stamped with an agent-task projection at ``depth``."""

    child = app.state.sessions.create(
        workspace_id="ws_default", title=sid_hint, parent_session_id="root"
    )
    task = AgentTask(
        task_id=f"task_{sid_hint}",
        parent_session_id="root",
        child_session_id=child.id,
        agent_ref={"expert_id": "main"},
        depth=depth,
        status=STATUS_RUNNING,
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
    )
    app.state.sessions.update(child.id, metadata_patch=task.to_metadata())
    return child.id


def test_forward_executor_is_per_depth(tmp_path: Path, monkeypatch) -> None:
    """A child turn runs on the pool for ITS depth: same depth → same pool, deeper
    child → a different pool, a root (non-child) turn → the default pool (None)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        d1a = _child_session_at_depth(app, 1, "d1a")
        d1b = _child_session_at_depth(app, 1, "d1b")
        d2 = _child_session_at_depth(app, 2, "d2")
        root = app.state.sessions.create(workspace_id="ws_default", title="root")

        e1a = _forward_executor(SimpleNamespace(app=app, sid=d1a))
        e1b = _forward_executor(SimpleNamespace(app=app, sid=d1b))
        e2 = _forward_executor(SimpleNamespace(app=app, sid=d2))
        eroot = _forward_executor(SimpleNamespace(app=app, sid=root.id))

        assert e1a is not None
        assert e1a is e1b, "same-depth children must share one pool"
        assert e2 is not e1a, "a deeper child must get its own pool"
        assert eroot is None, "a root (non-agent-task) turn uses the default pool"


class _NestingAgent:
    """Drives a real nested-orchestrator topology: a turn at depth < 3 spawns a
    child one level deeper and BLOCKS waiting on it (the tier-N orchestrator that
    calls wait_agent_tasks). On a single shared pool + cap=1 this deadlocks; on
    per-depth pools it completes."""

    def __init__(self) -> None:
        self.app = None

    def forward(self, question: str, session_id: str, **_kw):
        app = self.app
        sess = app.state.sessions.get(session_id)
        task = AgentTask.from_session(sess) if sess is not None else None
        depth = task.depth if task is not None else 0
        if depth < 3:
            child = spawn_child_turn_threadsafe(
                app,
                TaskSpec(
                    child_expert_id="main",
                    task_text="go",
                    parent_session_id=session_id,
                    requesting_expert_id="main",
                    depth=depth + 1,
                ),
            )
            # LONG wait (outlasts the test's terminal poll below): under a deadlock a
            # level cannot self-heal by timing out inside the poll window — the poll
            # sees a still-RUNNING parent and the assertion fails. Only genuine
            # per-depth scheduling lets the child fire the Event promptly.
            app.state.agent_task_registry.event(child.task_id).wait(timeout=90.0)
        return type(
            "P", (), {"answer": f"depth {depth} ok", "selected_expert": "", "routing_rationale": ""}
        )()


def test_nested_sync_wait_completes_without_deadlock(tmp_path: Path, monkeypatch) -> None:
    """depth1 waits on depth2 waits on depth3, ONE worker PER POOL. This exact
    topology hard-stalls on a single shared pool / global cap (depth2 queues behind
    the blocked depth1 and can never launch); per-depth pools + per-depth cap let
    each level run. The 90s inner waits ensure a deadlock does NOT self-heal within
    the 25s terminal poll."""

    _declare(monkeypatch, "main")
    agent = _NestingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.app = app
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 1  # one worker PER depth pool
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task1 = spawn_child_turn_threadsafe(
            app,
            TaskSpec(
                child_expert_id="main",
                task_text="go",
                parent_session_id=parent,
                requesting_expert_id="main",
                depth=1,
            ),
        )
        settled = _wait_terminal(app, task1.task_id, timeout=25.0)
        assert settled.status == "completed", settled.status
        # The FULL chain ran (not a timeout-degraded partial): a depth-3 grandchild
        # task exists and completed — impossible under the deadlock (depth2 would be
        # stuck queued and depth3 would never be spawned).
        all_tasks = app.state.agent_task_registry.snapshot()
        depths = {t.depth for t in all_tasks if t.status == "completed"}
        assert depths == {1, 2, 3}, sorted(depths)
