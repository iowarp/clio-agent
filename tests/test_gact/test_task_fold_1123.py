"""P2.5 (#1123): transport TaskEvent/TaskResult folding into AgentTask state."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import clio_agent.gact.agent_tasks as agent_tasks
import clio_agent.gact.task_fold as task_fold
from clio_agent.gact.agent_tasks import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    AgentTask,
    seed_agent_task,
)
from clio_agent.gact.agents.invoker import (
    InProcessExpertInvoker,
    TaskEvent,
    TaskHandle,
    TaskResult,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.enrichment import inject_pending_agent_task_notifications
from clio_agent.gact.turn_spawn import _on_child_done
from clio_agent.gact.types import Message, Part


class _Agent:
    """Minimal host agent; folded tasks never launch an in-process child turn."""

    def forward(self, question: str, session_id: str, **_kwargs: object) -> object:
        return SimpleNamespace(answer="ok", selected_expert="", routing_rationale="")


def _task_event(task: AgentTask, *, session_id: str | None = None) -> TaskEvent:
    return TaskEvent(
        event_type=agent_tasks.AGENT_TASK_EVENTS[task.status],
        task_id=task.task_id,
        session_id=session_id or task.child_session_id,
        status=task.status,
        payload=asdict(task),
    )


def _terminal_events(app: object, session_id: str) -> list[object]:
    return [
        event
        for event in app.state.bus._history.get(session_id, [])
        if event.type == "agent.task.completed"
    ]


def test_folded_task_events_complete_wait_without_in_process_future(tmp_path: Path) -> None:
    """A transport-only started/completed pair fires the existing wait primitive."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        seeded = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
        )
        handle = TaskHandle.from_task(seeded)
        invoker = InProcessExpertInvoker(app)

        running = replace(seeded, status=STATUS_RUNNING, updated_at="2026-07-31T01:00:00+00:00")
        completed = replace(
            running,
            status=STATUS_COMPLETED,
            result={
                "message_ref": "remote-message-1",
                "answer_excerpt": "remote result",
                "workflow_state": {"step": "done"},
            },
            notify_pending=True,
            updated_at="2026-07-31T01:00:01+00:00",
        )

        started_outcome = task_fold.fold_agent_task_event(app, _task_event(running))
        completed_outcome = task_fold.fold_agent_task_event(app, _task_event(completed))
        waited = invoker.wait(handle, timeout_s=0.1)

        assert started_outcome.applied is True
        assert completed_outcome.applied is True
        assert waited == TaskResult.from_task(completed_outcome.task)
        assert waited == TaskResult.from_task(completed)
        assert app.state.agent_task_registry.event(seeded.task_id).is_set()


def test_folded_terminal_has_callback_payload_and_observe_later_parity(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Fold uses the callback publisher shape, wakes, and stages observe-later output."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )
        app.state.bus._history[parent].clear()
        app.state.bus._history[running.child_session_id].clear()
        monkeypatch.setattr(app.state.turn_runner, "busy", lambda _sid: True)
        completed = replace(
            running,
            status=STATUS_COMPLETED,
            result={
                "message_ref": "remote-message-2",
                "answer_excerpt": "folded answer",
                "workflow_state": {},
            },
            notify_pending=True,
            updated_at="2026-07-31T02:00:00+00:00",
        )

        outcome = task_fold.fold_agent_task_event(app, _task_event(completed))

        assert outcome.applied is True
        assert app.state.agent_task_registry.event(running.task_id).is_set()
        assert [
            (event.kind, event.task_id) for event in app.state.loop_inboxes[parent].drain()
        ] == [("child_completed", running.task_id)]
        parent_events = _terminal_events(app, parent)
        child_events = _terminal_events(app, running.child_session_id)
        assert len(parent_events) == len(child_events) == 1
        assert parent_events[0].payload == child_events[0].payload == asdict(outcome.task)
        assert parent_events[0].payload == completed.to_metadata()["agent_task"]
        injected, task_ids = inject_pending_agent_task_notifications(app, parent, "NEXT")
        assert task_ids == [running.task_id]
        assert "folded answer" in injected


def test_duplicate_folded_terminal_is_typed_noop(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """The second copy cannot overwrite the first result or repeat side effects."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )
        app.state.bus._history[parent].clear()
        app.state.bus._history[running.child_session_id].clear()
        monkeypatch.setattr(app.state.turn_runner, "busy", lambda _sid: True)
        first_result = replace(
            TaskResult.from_task(running),
            status=STATUS_COMPLETED,
            result={"answer_excerpt": "first"},
            updated_at="2026-07-31T04:00:00+00:00",
        )
        second_result = replace(
            first_result,
            result={"answer_excerpt": "second"},
            updated_at="2026-07-31T04:00:01+00:00",
        )

        first = task_fold.fold_agent_task_event(app, first_result, notify_pending=True)
        second = task_fold.fold_agent_task_event(app, second_result, notify_pending=True)

        assert first.applied is True and first.reason == ""
        assert second.applied is False and second.reason == "already_terminal"
        assert second.task == first.task
        assert second.task.result == {"answer_excerpt": "first"}
        assert len(_terminal_events(app, parent)) == 1
        assert len(app.state.loop_inboxes[parent].drain()) == 1


def test_folded_terminal_racing_local_callback_is_first_wins(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A callback/fold race has one transition, publish, and wake; either may win."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )
        now = "2026-07-31T03:00:00+00:00"
        app.state.messages[running.child_session_id] = [
            Message(
                id="local-message",
                session_id=running.child_session_id,
                role="assistant",
                created_at=now,
                updated_at=now,
                parts=[Part(id="part-1", type="text", text="local result")],
            )
        ]
        app.state.bus._history[parent].clear()
        app.state.bus._history[running.child_session_id].clear()
        monkeypatch.setattr(app.state.turn_runner, "busy", lambda _sid: True)
        remote = replace(
            running,
            status=STATUS_COMPLETED,
            result={
                "message_ref": "remote-message",
                "answer_excerpt": "remote result",
                "workflow_state": {},
            },
            notify_pending=True,
            updated_at=now,
        )

        original_transition = app.state.agent_task_registry.transition
        contenders_ready = threading.Barrier(2)

        def synchronized_transition(task_id: str, new_status: str, **kwargs: object) -> AgentTask:
            if new_status == STATUS_COMPLETED:
                contenders_ready.wait(timeout=5.0)
            return original_transition(task_id, new_status, **kwargs)

        app.state.agent_task_registry.transition = synchronized_transition
        with ThreadPoolExecutor(max_workers=2) as pool:
            callback_future = pool.submit(
                _on_child_done, app, running.task_id, running.child_session_id, "async"
            )
            fold_future = pool.submit(task_fold.fold_agent_task_event, app, _task_event(remote))
            callback_future.result(timeout=10.0)
            fold_outcome = fold_future.result(timeout=10.0)

        final = app.state.agent_task_registry.get(running.task_id)
        assert final is not None and final.status == STATUS_COMPLETED
        assert fold_outcome.reason in {"", "already_terminal"}
        assert len(_terminal_events(app, parent)) == 1
        assert len(_terminal_events(app, running.child_session_id)) == 1
        assert [
            (event.kind, event.task_id) for event in app.state.loop_inboxes[parent].drain()
        ] == [("child_completed", running.task_id)]


def test_nonterminal_fold_updates_progress_without_terminalizing(tmp_path: Path) -> None:
    """A started/progress fold updates state but leaves wait unresolved."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        queued = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
        )
        app.state.bus._history[parent].clear()
        running = replace(queued, status=STATUS_RUNNING, updated_at="2026-07-31T05:00:00+00:00")

        outcome = task_fold.fold_agent_task_event(app, _task_event(running))

        assert outcome.applied is True
        assert outcome.task.status == STATUS_RUNNING
        assert outcome.task.updated_at == "2026-07-31T05:00:00+00:00"
        assert not app.state.agent_task_registry.event(queued.task_id).is_set()
        assert [event.type for event in app.state.bus._history[parent]] == ["agent.task.started"]
