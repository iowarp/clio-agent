"""Runtime limit updates over real task admission, worker pools and persistent settings."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.agent  # noqa: F401 - preload the real agent before worker admission deadlines
from clio_agent.gact.app import build_app
from clio_agent.gact.protocol.v3.event import _task_path
from clio_agent.gact.provenance.child_projection import child_session_lineage
from clio_agent.gact.session_descendants import descendant_session_ids
from clio_agent.gact.turn_spawn import SpawnError, TaskSpec, spawn_child_turn_threadsafe
from clio_agent.gact.turn_spawn_executor import (
    agent_task_executor_for_depth,
    install_agent_task_executor,
    shutdown_agent_task_executors,
)
from tests._config_layer import set_config
from tests.test_gact.test_turn_spawn_s3 import _declare, _wait_terminal

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_saved_unlimited_concurrency_restores_at_startup() -> None:
    """A persisted zero must not revert to the old three-worker fallback at boot."""
    set_config("agent_tasks.max_concurrent", 0)
    app = SimpleNamespace(state=SimpleNamespace())
    install_agent_task_executor(app)
    assert app.state.max_concurrent_agent_tasks == 0
    barrier = threading.Barrier(5)
    try:
        pool = agent_task_executor_for_depth(app, 1)
        futures = [pool.submit(barrier.wait, 5) for _ in range(4)]
        barrier.wait(5)
        assert all(future.result(5) >= 0 for future in futures)
    finally:
        shutdown_agent_task_executors(app)


class HeldCalls:
    """Hold test agent calls independently while real executor/admission code runs."""

    def __init__(self, count: int = 4) -> None:
        self.entered = [threading.Event() for _ in range(count)]
        self.release = [threading.Event() for _ in range(count)]
        self.lock = threading.Lock()
        self.count = 0
        self.by_session: dict[str, threading.Event] = {}
        self.parent_session_id = ""

    def forward(self, question: str, session_id: str, **kwargs: Any) -> SimpleNamespace:
        del question, kwargs
        if session_id == self.parent_session_id:
            return SimpleNamespace(answer="Collected.", selected_expert="", routing_rationale="")
        with self.lock:
            index = self.count
            self.count += 1
            self.by_session[session_id] = self.release[index]
        self.entered[index].set()
        assert self.release[index].wait(10), "test must release every real worker"
        return SimpleNamespace(answer="Finished.", selected_expert="", routing_rationale="")

    def finish(self) -> None:
        """Release every worker, including after a failed assertion."""
        for event in self.release:
            event.set()


def _save(client: TestClient, changes: dict[str, int | None]) -> dict[str, Any]:
    current = client.get("/v1/settings/runtime")
    assert current.status_code == 200, current.text
    saved = client.patch(
        "/v1/settings/runtime", json={"revision": current.json()["revision"], "changes": changes}
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def test_live_concurrency_increase_starts_queued_work_and_decrease_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(monkeypatch, "main")
    calls = HeldCalls()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=calls)
    # Populate the real prompt cache before measuring worker admission; the
    # retained stack showed cold packaged-prompt reads consuming this deadline.
    app.state.prompt_registry._builtins()
    with TestClient(app) as client:
        _save(client, {"agent_tasks.max_concurrent": 1})
        parent = app.state.sessions.create(workspace_id="ws_default", title="parent")
        calls.parent_session_id = parent.id

        def spawn() -> Any:
            return spawn_child_turn_threadsafe(
                app, TaskSpec(child_expert_id="main", task_text="Work", parent_session_id=parent.id)
            )

        try:
            first, second, third = spawn(), spawn(), spawn()
            assert calls.entered[0].wait(5)
            assert second.status == third.status == "queued"
            _save(client, {"agent_tasks.max_concurrent": 3})
            assert calls.entered[1].wait(5) and calls.entered[2].wait(5)
            assert app.state.max_concurrent_agent_tasks == 3
            _save(client, {"agent_tasks.max_concurrent": 1})
            fourth = spawn()
            assert fourth.status == "queued"
            for task in (first, second):
                calls.by_session[task.child_session_id].set()
                assert _wait_terminal(app, task.task_id).status == "completed"
                assert app.state.agent_task_registry.get(fourth.task_id).status == "queued"
            calls.by_session[third.child_session_id].set()
            assert _wait_terminal(app, third.task_id).status == "completed"
            assert calls.entered[3].wait(5)
            calls.by_session[fourth.child_session_id].set()
            assert _wait_terminal(app, fourth.task_id).status == "completed"
        finally:
            calls.finish()


@pytest.mark.parametrize("fanout_bound,expected_running", [(0, 4), (2, 2)])
def test_unlimited_concurrency_has_no_default_pool_cap_and_keeps_agent_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fanout_bound: int, expected_running: int
) -> None:
    _declare(monkeypatch, "main")
    calls = HeldCalls()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=calls)
    with TestClient(app) as client:
        _save(client, {"agent_tasks.max_concurrent": 0})
        parent = app.state.sessions.create(workspace_id="ws_default", title="parent")
        calls.parent_session_id = parent.id
        try:
            tasks = [
                spawn_child_turn_threadsafe(
                    app,
                    TaskSpec(
                        child_expert_id="main",
                        task_text="Work",
                        parent_session_id=parent.id,
                        requesting_expert_id="main",
                        fanout_bound=fanout_bound,
                    ),
                )
                for _ in range(4)
            ]
            assert all(event.wait(5) for event in calls.entered[:expected_running])
            assert (
                sum(t.status == "running" for t in app.state.agent_task_registry.snapshot())
                == expected_running
            )
            assert (
                sum(t.status == "queued" for t in app.state.agent_task_registry.snapshot())
                == 4 - expected_running
            )
        finally:
            calls.finish()
        assert all(_wait_terminal(app, task.task_id).status == "completed" for task in tasks)


def test_depth_updates_and_unlimited_preserve_deep_history_and_task_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=HeldCalls())
    with TestClient(app) as client:
        root = app.state.sessions.create(workspace_id="ws_default", title="root")
        _save(client, {"agent_tasks.max_depth": 1})
        with pytest.raises(SpawnError, match="max 1"):
            spawn_child_turn_threadsafe(
                app,
                TaskSpec(
                    child_expert_id="main", task_text="Work", parent_session_id=root.id, depth=2
                ),
            )
        _save(client, {"agent_tasks.max_depth": 0})
        parent = root.id
        tasks = []
        for depth in range(1, 13):
            task = spawn_child_turn_threadsafe(
                app,
                TaskSpec(
                    child_expert_id="main",
                    task_text="Work",
                    parent_session_id=parent,
                    depth=depth,
                    start_turn=False,
                ),
            )
            tasks.append(task)
            parent = task.child_session_id
        _save(client, {"agent_tasks.max_depth": 2})
        ids = descendant_session_ids(app, root.id)
        assert ids == [task.child_session_id for task in tasks]
        lineage, truncated = child_session_lineage(app, root.id)
        assert not truncated and lineage[-1]["depth"] == 12
        assert lineage[-1]["task_path"] == [task.task_id for task in tasks]
        monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
        assert _task_path(
            {"task_id": tasks[-1].task_id, "parent_session_id": tasks[-1].parent_session_id}
        ) == [task.task_id for task in tasks]


def test_runtime_limit_settings_validate_zero_and_reject_invalid_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=HeldCalls())
    with TestClient(app) as client:
        snapshot = _save(client, {"agent_tasks.max_concurrent": 0, "agent_tasks.max_depth": 0})
        for row in snapshot["settings"]:
            if row["key"].startswith("agent_tasks."):
                assert row["value"] == row["unlimited_value"] == 0
                assert row["kind"] == "integer"
        for invalid in (-1, 1.5, True, "unlimited"):
            response = client.patch(
                "/v1/settings/runtime",
                json={
                    "revision": snapshot["revision"],
                    "changes": {"agent_tasks.max_depth": invalid},
                },
            )
            assert response.status_code == 422
        _save(client, {"agent_tasks.max_concurrent": None, "agent_tasks.max_depth": None})
        assert app.state.max_concurrent_agent_tasks == 3
