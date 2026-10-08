"""Stop settles the old turn and delivers submitted feedback, pausing only the queue."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.composer_runtime import session_autostart_suspended
from clio_agent.gact.protocol.v3.event import event_to_v3
from clio_agent.gact.protocol.v3.session import session_to_v3
from clio_agent.gact.sessions import SessionStore

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _BlockedFirstTurn:
    """Wait for the first turn's real cancel signal; record subsequent inputs."""

    def __init__(self) -> None:
        self.app: Any = None
        self.started = threading.Event()
        self.questions: list[str] = []

    def forward(self, question: str, session_id: str) -> Any:
        """Hold the old turn until Stop and finish follow-ups without a live LM."""
        self.questions.append(question)
        if len(self.questions) == 1:
            cancel_event = self.app.state.cancel_events[session_id]
            self.started.set()
            assert cancel_event.wait(timeout=5), "the old turn never received Stop"
        return SimpleNamespace(answer="done", selected_expert="", routing_rationale="")


def _wait_for_feedback(app: Any, sid: str, ids: list[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        completed = {
            row.turn_id
            for row in app.state.messages.get(sid, [])
            if row.role == "assistant" and row.error_info is None
        }
        if set(ids) <= completed and not app.state.turn_runner.busy(sid):
            return
        time.sleep(0.01)
    pytest.fail("submitted feedback did not finish automatically after Stop")


def test_stop_delivers_sent_feedback_in_order_and_keeps_future_queue_paused(
    tmp_path: Path,
) -> None:
    """The reported stalled-tool sequence needs no extra send and preserves every ID."""
    agent = _BlockedFirstTurn()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    agent.app = app
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Stop and feedback"}).json()["id"]
        first = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "check the repo"}]},
        )
        assert first.status_code == 200, first.text
        assert agent.started.wait(timeout=5), "first forward never started"
        first_id = first.json()["message_id"]
        feedback = ["can you actually use gh to clone it", "then inspect its latest release"]
        ids = ["msg_feedback_clone", "msg_feedback_release"]
        for text, message_id in zip(feedback, ids, strict=True):
            response = client.post(
                f"/v1/sessions/{sid}/messages",
                json={
                    "parts": [{"type": "text", "text": text}],
                    "delivery": "steer",
                    "client_message_id": message_id,
                },
            )
            assert response.status_code == 202, response.text
            assert response.json()["message_id"] == message_id
        queued = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "future task", "client_message_id": "msg_future"},
        )
        assert queued.status_code == 201, queued.text

        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        _wait_for_feedback(app, sid, ids)

        assert len(agent.questions) == 3
        for question, expected in zip(agent.questions[1:], feedback, strict=True):
            assert expected in question
        rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        user_ids = [row["id"] for row in rows if row["role"] == "user"]
        assert len(user_ids) == 3
        assert set(user_ids) == {first_id, *ids}
        for message_id in ids:
            row = next(row for row in rows if row["id"] == message_id)
            assert row["turn_id"] == message_id
            assert row["metadata"]["pending_steer"] is False
            assistant = next(
                row for row in rows if row["turn_id"] == message_id and row["role"] == "assistant"
            )
            assert assistant.get("error_info") is None
        old_assistant = next(
            row for row in rows if row["turn_id"] == first_id and row["role"] == "assistant"
        )
        assert old_assistant["error_info"]["error"] == "cancelled"
        assert client.get(f"/v1/sessions/{sid}/pending-steers").json()["pending_steers"] == []
        queue = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
        assert [row["id"] for row in queue] == [queued.json()["id"]]
        assert session_autostart_suspended(app, sid)
        session = client.get(f"/v1/sessions/{sid}").json()
        assert session["status"] == "idle"
        assert session["metadata"]["composer_queue_paused"] is True
        events = app.state.bus.session_events_since(sid, cursor=1)
        assert [event.payload["turn_id"] for event in events if event.type == "turn.started"] == [
            first_id,
            *ids,
        ]
        cancelled_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "session.status_changed" and event.payload["status"] == "cancelled"
        )
        feedback_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "turn.started" and event.payload["turn_id"] == ids[0]
        )
        assert cancelled_index < feedback_index
        current = app.state.sessions.get(sid)
        assert session_to_v3(current)["metadata"]["composer_queue_paused"] is True
        # The real session event used by the frontend carries the same pause.
        status_event = next(
            event for event in reversed(events) if event.type == "session.status_changed"
        )
        projected = event_to_v3(status_event, session=current)
        assert projected["payload"]["metadata"]["composer_queue_paused"] is True
        cancellation = next(
            event.payload["composer_autostart"]
            for event in app.state.bus.session_events_since(sid, cursor=1)
            if event.type == "session.status_changed" and "composer_autostart" in event.payload
        )
        assert cancellation["pending_steers_suspended"] is False
        assert cancellation["queued_messages_paused"] is True

        # The existing explicit Send now action is still usable after Stop.
        response = client.post(
            f"/v1/sessions/{sid}/queued-messages/{queue[0]['id']}/promote",
            json={"revision": queue[0]["revision"], "delivery": "start"},
        )
        assert response.status_code == 200, response.text
        _wait_for_feedback(app, sid, ["msg_future"])
        assert len(agent.questions) == 4
        assert not session_autostart_suspended(app, sid)


def test_queue_pause_survives_completed_feedback_and_store_reload(tmp_path: Path) -> None:
    """Session status alone cannot represent a paused queue after feedback finishes."""
    path = tmp_path / "sessions.json"
    sessions = SessionStore(path=path)
    session = sessions.create(workspace_id="ws_default", title="Paused future work")
    sessions.update(session.id, status="idle", metadata_patch={"composer_queue_paused": True})
    app = SimpleNamespace(state=SimpleNamespace(sessions=SessionStore(path=path)))
    assert session_autostart_suspended(app, session.id)
    app.state.sessions.update(session.id, metadata_patch={"composer_queue_paused": False})
    app.state.sessions = SessionStore(path=path)
    assert not session_autostart_suspended(app, session.id)


def test_legacy_cancelled_session_retains_queue_pause() -> None:
    """An older persisted cancelled session has no queue-pause metadata yet."""
    sessions = SessionStore()
    session = sessions.create(workspace_id="ws_default", title="Legacy cancelled")
    sessions.update(session.id, status="cancelled")
    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions))
    assert session_autostart_suspended(app, session.id)


def test_queue_pause_projection_does_not_expose_private_session_metadata() -> None:
    """The normalized session sends the public queue state without private bookkeeping."""
    sessions = SessionStore()
    session = sessions.create(
        workspace_id="ws_default",
        metadata={"composer_queue_paused": True, "private_runtime_state": {"secret": "private"}},
    )
    assert session_to_v3(session)["metadata"] == {"composer_queue_paused": True}
