"""Stop settles the old turn and delivers submitted feedback, pausing only the queue."""

from __future__ import annotations

import asyncio
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
from clio_agent.runtime import process_census
from clio_agent.runtime.status import IntegrationStatus

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _BlockedFirstTurn:
    """Wait for the first turn's real cancel signal; record subsequent inputs."""

    def __init__(self) -> None:
        self.app: Any = None
        self.started = threading.Event()
        self.questions: list[str] = []
        self.model_inputs: list[list[str]] = []

    def forward(self, question: str, session_id: str) -> Any:
        """Hold the old turn until Stop and finish follow-ups without a live LM."""
        from dspy.lm15 import TextPart

        from clio_agent.gact import context
        from clio_agent.gact.agents.context_reader import ContextReader

        scope = context.run_keyed_scope(context.current_turn().primary_agent_id or "main")
        messages = ContextReader(self.app.state.arc, session_id, scope).read()
        self.model_inputs.append(
            [
                part.text
                for message in messages
                for part in message.parts
                if isinstance(part, TextPart)
            ]
        )
        self.questions.append(question)
        if len(self.questions) == 1:
            cancel_event = self.app.state.cancel_events[session_id]
            self.started.set()
            assert cancel_event.wait(timeout=5), "the old turn never received Stop"
        return SimpleNamespace(answer="done", selected_expert="", routing_rationale="")


def _wait_for_feedback(app: Any, sid: str, ids: list[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        completed = app.state.task_history.acknowledged_notifications(sid)
        if set(ids) <= completed and not app.state.turn_runner.busy(sid):
            return
        time.sleep(0.01)
    pytest.fail(
        "submitted feedback did not finish automatically after Stop: "
        f"expected={ids!r}, acknowledged={sorted(completed)!r}, "
        f"reservation={app.state.turn_runner.handle(sid)!r}, "
        f"queue_paused={session_autostart_suspended(app, sid)!r}, "
        f"pending={[(item.id, item.kind, item.delivery) for item in app.state.task_history.pending_mail(sid)]!r}"
    )


@pytest.mark.parametrize("retire_before_read", [False, True])
def test_idle_send_resumes_queue_during_empty_admission_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retire_before_read: bool
) -> None:
    """Idle admission owns the slot but must not turn a new human send into steering."""
    from tests.test_gact.test_post_messages import FakeClioAgent

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent())
    entered = threading.Event()
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Paused queue"}).json()["id"]
        app.state.sessions.update(sid, metadata_patch={"composer_queue_paused": True})
        queued = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "retained future work", "client_message_id": "msg_future_idle"},
        )
        assert queued.status_code == 201, queued.text
        original_admit = app.state.turn_runner.admit
        release: asyncio.Event | None = None
        held_once = False

        async def install_hold() -> None:
            nonlocal release
            release = asyncio.Event()

        async def held_admit(session_id: str) -> Any:
            nonlocal held_once
            if held_once:
                return await original_admit(session_id)
            held_once = True
            assert release is not None
            selected = await original_admit(session_id) if retire_before_read else None
            assert selected is None
            entered.set()
            await release.wait()
            return selected if retire_before_read else await original_admit(session_id)

        assert client.portal is not None
        client.portal.call(install_hold)
        monkeypatch.setattr(app.state.turn_runner, "admit", held_admit)
        app.state.turn_runner.signal(sid)
        assert entered.wait(5), "idle admission never reserved the session"
        try:
            reservation = app.state.turn_runner.handle(sid)
            assert reservation is not None and reservation.turn_id == ""
            assert app.state.turn_runner.busy(sid)
            if retire_before_read:
                from clio_agent.gact import message_submission

                original_parts = message_submission._plan_resource_parts
                original_handle = app.state.turn_runner.handle
                armed = threading.Event()

                def prepare_parts(*args: Any, **kwargs: Any) -> Any:
                    planned = original_parts(*args, **kwargs)
                    armed.set()
                    return planned

                def read_after_retirement(session_id: str) -> Any:
                    if armed.is_set():
                        armed.clear()
                        assert release is not None and client.portal is not None
                        client.portal.call(release.set)
                        deadline = time.monotonic() + 5
                        while original_handle(session_id) is not None:
                            assert time.monotonic() < deadline, "empty admission did not retire"
                            time.sleep(0.01)
                    return original_handle(session_id)

                monkeypatch.setattr(message_submission, "_plan_resource_parts", prepare_parts)
                monkeypatch.setattr(app.state.turn_runner, "handle", read_after_retirement)
            response = client.post(
                f"/v1/sessions/{sid}/messages",
                json={"text": "resume normal sending", "client_message_id": "msg_resume_idle"},
            )
            assert response.status_code == 202, response.text
            assert not session_autostart_suspended(app, sid)
            if not retire_before_read:
                assert app.state.turn_runner.handle(sid) == reservation
                assert app.state.task_history.acknowledged_notifications(sid) == set()
        finally:
            assert release is not None
            client.portal.call(release.set)
        _wait_for_feedback(app, sid, [response.json()["message_id"], queued.json()["id"]])
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []


def test_stop_delivers_sent_feedback_in_order_and_keeps_future_queue_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported stalled-tool sequence needs no extra send and preserves every ID."""

    async def _isolated_process_scan() -> list[IntegrationStatus]:
        # This checks the turn/feedback lifecycle, not the host's cold process
        # census. The deferred-scan tests cover that independent boot service.
        return []

    monkeypatch.setattr(process_census, "boot_reap_off_loop", _isolated_process_scan)
    agent = _BlockedFirstTurn()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    agent.app = app
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Stop and feedback"}).json()["id"]
        first = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "check the repo"}]},
        )
        assert first.status_code == 202, first.text
        started = agent.started.wait(timeout=5)
        assert started, "first forward never started"
        first_id = first.json()["message_id"]
        feedback = ["can you actually use gh to clone it", "then inspect its latest release"]
        client_ids = ["msg_feedback_clone", "msg_feedback_release"]
        ids = []
        for text, message_id in zip(feedback, client_ids, strict=True):
            response = client.post(
                f"/v1/sessions/{sid}/messages",
                json={
                    "parts": [{"type": "text", "text": text}],
                    "delivery": "steer",
                    "client_message_id": message_id,
                },
            )
            assert response.status_code == 202, response.text
            accepted_id = response.json()["message_id"]
            assert accepted_id and accepted_id not in ids
            ids.append(accepted_id)
        queued = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "future task", "client_message_id": "msg_future"},
        )
        assert queued.status_code == 201, queued.text
        remaining = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "remaining future task", "client_message_id": "msg_remaining"},
        )
        assert remaining.status_code == 201, remaining.text

        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        _wait_for_feedback(app, sid, ids)

        assert len(agent.questions) == 2
        # Both accepted arrivals enter the same next model iteration, in order.
        # A separate turn per message is not the queue's delivery boundary.
        delivered_text = "\n".join(agent.model_inputs[1])
        assert delivered_text.index(feedback[0]) < delivered_text.index(feedback[1])
        proofs = app.state.task_history.context_inputs(sid, "standard")
        [batch] = [proof for proof in proofs.values() if set(ids) <= set(proof.notification_ids)]
        assert [identity for identity in batch.notification_ids if identity in ids] == ids
        rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        user_ids = [row["id"] for row in rows if row["role"] == "user"]
        assert len(user_ids) == 3
        assert set(user_ids) == {first_id, *ids}
        for message_id, client_id in zip(ids, client_ids, strict=True):
            row = next(row for row in rows if row["id"] == message_id)
            assert row["metadata"]["client_message_id"] == client_id
            assert row["turn_id"] == message_id
            assert row["metadata"]["pending_steer"] is False
        from clio_agent.gact.transcript_file import load_durable_transcript

        reloaded = load_durable_transcript(app, sid)
        assert reloaded is not None
        for message_id in ids:
            replayed = next(message for message in reloaded if message.id == message_id)
            assert replayed.metadata["pending_steer"] is False
            assert app.state.task_history.committed_notification_input(sid, message_id) == batch
        assistant = next(
            row for row in rows if row["turn_id"] == ids[0] and row["role"] == "assistant"
        )
        assert assistant.get("error_info") is None
        old_assistant = next(
            row for row in rows if row["turn_id"] == first_id and row["role"] == "assistant"
        )
        assert old_assistant["error_info"]["error"] == "cancelled"
        assert client.get(f"/v1/sessions/{sid}/pending-steers").json()["pending_steers"] == []
        queue = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
        assert [row["id"] for row in queue] == [queued.json()["id"], remaining.json()["id"]]
        assert session_autostart_suspended(app, sid)
        session = client.get(f"/v1/sessions/{sid}").json()
        assert session["status"] == "idle"
        assert session["metadata"]["composer_queue_paused"] is True
        events = app.state.bus.session_events_since(sid, cursor=1)
        assert [event.payload["turn_id"] for event in events if event.type == "turn.started"] == [
            first_id,
            ids[0],
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
        assert response.json()["acceptance"]["message_id"] == "msg_future"
        # Promotion preserves the accepted mailbox identity; the transcript's
        # client message ID remains a separate public correlation identity.
        _wait_for_feedback(app, sid, [queued.json()["id"]])
        proof = app.state.task_history.committed_notification_input(sid, queued.json()["id"])
        assert proof is not None and proof.turn_id == "msg_future"
        rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        promoted = next(row for row in rows if row["id"] == "msg_future")
        assert promoted["metadata"].get("pending_steer", False) is False
        assert promoted["metadata"]["client_message_id"] == "msg_future"
        assert len(agent.questions) == 3
        # Send now is a one-item override, preserving the independently paused
        # future queue. An ordinary new idle send explicitly resumes that queue.
        assert session_autostart_suspended(app, sid)
        assert [
            row["id"]
            for row in client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
        ] == [remaining.json()["id"]]
        assert (
            app.state.task_history.committed_notification_input(sid, remaining.json()["id"]) is None
        )
        resumed = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"text": "Resume normal sending", "client_message_id": "msg_explicit_resume"},
        )
        assert resumed.status_code == 202, resumed.text
        _wait_for_feedback(app, sid, [resumed.json()["message_id"], remaining.json()["id"]])
        assert not session_autostart_suspended(app, sid)
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []
        assert len(agent.questions) == 5
        final_rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        for identity, client_id in (
            ("msg_future", "msg_future"),
            ("msg_remaining", "msg_remaining"),
            (resumed.json()["message_id"], "msg_explicit_resume"),
        ):
            [recorded] = [row for row in final_rows if row["id"] == identity]
            assert recorded["role"] == "user"
            assert recorded["metadata"]["client_message_id"] == client_id
            assert (
                len(
                    [
                        row
                        for row in final_rows
                        if row["metadata"].get("client_message_id") == client_id
                    ]
                )
                == 1
            )


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


def test_startup_migrates_legacy_cancelled_queue_pause_once(tmp_path: Path) -> None:
    """Legacy Stop is imported at startup; ordinary reads do not migrate state."""
    path = tmp_path / "sessions.json"
    sessions = SessionStore(path=path)
    session = sessions.create(workspace_id="ws_default", title="Legacy cancelled")
    sessions.update(session.id, status="cancelled")
    app = build_app(sessions_path=path)
    with TestClient(app):
        assert session_autostart_suspended(app, session.id)
        first = app.state.sessions.get(session.id)
        from clio_agent.gact.task_migration import migrate_history

        migrate_history(app)
        assert app.state.sessions.get(session.id) == first
        assert app.state.turn_runner.handle(session.id) is None


def test_queue_pause_projection_does_not_expose_private_session_metadata() -> None:
    """The normalized session sends the public queue state without private bookkeeping."""
    sessions = SessionStore()
    session = sessions.create(
        workspace_id="ws_default",
        metadata={"composer_queue_paused": True, "private_runtime_state": {"secret": "private"}},
    )
    assert session_to_v3(session)["metadata"] == {"composer_queue_paused": True}
