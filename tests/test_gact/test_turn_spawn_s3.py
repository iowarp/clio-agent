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

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import STATUS_FAILED, STATUS_RUNNING, AgentTask
from clio_agent.gact.app import build_app
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
