"""P2.14 (#1131): typed background-exit injection parts."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agent_tasks import STATUS_COMPLETED, STATUS_RUNNING, seed_agent_task
from clio_agent.gact.agents.invoker import TaskEvent
from clio_agent.gact.app import build_app
from clio_agent.gact.background_exit import background_exit_part
from clio_agent.gact.enrichment import (
    consume_pending_agent_task_notifications,
    inject_pending_agent_task_notifications,
)
from clio_agent.gact.loop_inbox import InboxEvent, drain_active_session_inbox, inbox_for
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.gact.task_fold import fold_agent_task_event

from .conftest import complete_turn

pytestmark = pytest.mark.usefixtures("host_agent_executor")
FIXTURE = Path(__file__).parents[1] / "fixtures" / "background_exit_part_1131.json"


class _Agent:
    """Deterministic host agent for parent-turn injection tests."""

    def forward(self, question: str, session_id: str, **_kwargs: object) -> Any:
        return SimpleNamespace(answer="parent continued", selected_expert="", routing_rationale="")


def _event(task: Any) -> TaskEvent:
    """Build the relay TaskEvent boundary shape for ``task``."""

    return TaskEvent(
        event_type=f"agent.task.{task.status}",
        task_id=task.task_id,
        session_id=task.child_session_id,
        status=task.status,
        payload=asdict(task),
    )


@contextmanager
def _active_turn(app: Any, session_id: str) -> Iterator[None]:
    """Bind the app/session context used by live transcript helpers."""

    with _gact_app_context(app):
        token = ctx.set_session_id(session_id)
        try:
            yield
        finally:
            ctx.reset(token)


def _complete_pending(app: Any, parent: str, task_id: str = "task_pending") -> Any:
    """Seed and fold one relay task to completed/notify-pending."""

    running = seed_agent_task(
        app,
        parent_session_id=parent,
        agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
        status=STATUS_RUNNING,
        task_id=task_id,
        placement="relay:ares",
        host="ares",
    )
    completed = replace(
        running,
        status=STATUS_COMPLETED,
        live_state=STATUS_COMPLETED,
        result={"answer_excerpt": "remote finished"},
        notify_pending=True,
        updated_at="2026-08-01T12:00:00+00:00",
    )
    return fold_agent_task_event(app, _event(completed)).task


def test_idle_parent_next_turn_carries_background_exit_exactly_once(tmp_path: Path) -> None:
    """A relay completion while idle injects one typed part on only the next turn."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
            placement="relay:ares",
            host="ares",
        )
        assert app.state.turn_runner.busy(parent) is False
        completed = replace(
            running,
            status=STATUS_COMPLETED,
            live_state=STATUS_COMPLETED,
            result={"message_ref": "relay-message", "answer_excerpt": "remote finished"},
            artifact_ref="artifact://ws_default/result@v1",
            notify_pending=True,
            updated_at="2026-08-01T12:00:00+00:00",
        )

        outcome = fold_agent_task_event(app, _event(completed))
        assert outcome.applied is True

        first = complete_turn(client, parent, "continue after the remote app")
        exits = [part for part in first["parts"] if part["type"] == "background_exit"]
        assert len(exits) == 1
        assert exits[0]["task_id"] == running.task_id
        assert exits[0]["job_id"] == running.task_id
        assert exits[0]["exit_status"] == "completed"
        assert exits[0]["artifact_ref"] == "artifact://ws_default/result@v1"
        assert exits[0]["placement"] == "relay:ares"

        second = complete_turn(client, parent, "continue once more")
        assert [part for part in second["parts"] if part["type"] == "background_exit"] == []


def test_aborted_staging_leaves_exit_for_next_successful_turn(tmp_path: Path) -> None:
    """Staging without reaching consume does not lose the typed part."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _complete_pending(app, parent, "task_aborted_stage")

        _text, staged = inject_pending_agent_task_notifications(app, parent, "aborted turn")
        assert staged == [task.task_id]
        assert app.state.agent_task_registry.get(task.task_id).notify_pending is True

        successful = complete_turn(client, parent, "retry after abort")
        exits = [part for part in successful["parts"] if part["type"] == "background_exit"]
        assert [part["task_id"] for part in exits] == [task.task_id]
        assert app.state.agent_task_registry.get(task.task_id).notify_pending is False


def test_midturn_drain_races_next_turn_injection_to_one_exit_part(tmp_path: Path) -> None:
    """Both consumers race through mark_consumed and only the winner emits the twin."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _complete_pending(app, parent, "task_racing_consumers")
        _text, staged = inject_pending_agent_task_notifications(app, parent, "next")
        inbox_for(app, parent).put(InboxEvent(kind="child_completed", task_id=task.task_id))

        registry = app.state.agent_task_registry
        original_mark_consumed = registry.mark_consumed
        contenders = threading.Barrier(2)

        def synchronized_mark_consumed(task_id: str, consumed_at: str) -> Any:
            contenders.wait(timeout=5.0)
            return original_mark_consumed(task_id, consumed_at)

        registry.mark_consumed = synchronized_mark_consumed

        def drain_midturn() -> str:
            with _active_turn(app, parent):
                return drain_active_session_inbox(app)

        def consume_next_turn() -> None:
            with _active_turn(app, parent):
                consume_pending_agent_task_notifications(app, parent, staged)

        with ThreadPoolExecutor(max_workers=2) as pool:
            drain_future = pool.submit(drain_midturn)
            consume_future = pool.submit(consume_next_turn)
            drain_future.result(timeout=10.0)
            consume_future.result(timeout=10.0)

        added = [
            event.payload["part"]
            for event in app.state.bus._history.get(parent, [])
            if event.type == "message.part.added"
            and event.payload.get("part", {}).get("type") == "background_exit"
        ]
        assert [part["task_id"] for part in added] == [task.task_id]
        assert app.state.agent_task_registry.get(task.task_id).notify_pending is False


def test_background_exit_without_artifact_omits_default_field(tmp_path: Path) -> None:
    """A terminal fold without an artifact keeps the additive wire field absent."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _complete_pending(app, parent, "task_without_artifact")

        wire = background_exit_part(task).to_wire()

        assert wire["type"] == "background_exit"
        assert wire["task_id"] == task.task_id
        assert "artifact_ref" not in wire


def test_background_exit_part_matches_committed_fixture() -> None:
    """The additive field vocabulary stays pinned for gact-tui consumers."""

    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    from clio_agent.gact.parts import Part

    assert Part(**expected).to_wire() == expected
