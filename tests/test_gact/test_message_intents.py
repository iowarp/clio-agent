"""Focused contract tests for durable steers and queued future messages."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.message_intents import (
    IntentStoreReadError,
    MessageIntentStore,
    PendingSteer,
    QueuedMessage,
    RevisionConflictError,
)
from clio_agent.gact.types import Part, PostMessageResponse
from tests.test_gact.test_post_messages import FakeClioAgent, SlowClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_pending_steer_and_acceptance_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "message-intents.json"
    store = MessageIntentStore(path)
    pending = PendingSteer(
        message_id="msg_user_client_1",
        session_id="sess_1",
        parts=[Part(type="text", text="change course")],
        text="change course",
        accepted_at="2026-08-30T12:00:00+00:00",
    )
    response = PostMessageResponse(
        message_id=pending.message_id,
        accepted_at=pending.accepted_at,
        delivery="steer",
        state="pending_steer",
    )

    assert store.accept_pending(pending, "idem-1", response) is None
    replay = store.accept_pending(pending, "idem-1", response)
    assert replay == response

    restored = MessageIntentStore(path)
    assert [row.message_id for row in restored.list_pending("sess_1")] == [pending.message_id]
    assert restored.acceptance("sess_1", "idem-1") == response

    claimed = restored.claim_pending("sess_1", pending.message_id)
    assert claimed is not None and claimed.state == "claimed"
    # A process restart before consumption makes the claim available again.
    restarted = MessageIntentStore(path)
    assert restarted.get_pending("sess_1", pending.message_id).state == "pending"  # type: ignore[union-attr]


def test_restart_repairs_acceptance_committed_before_transcript_append(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    first_app = build_app(sessions_path=sessions_path, agent=FakeClioAgent(answer="unused"))
    with TestClient(first_app) as client:
        sid = client.post("/v1/sessions", json={"title": "restart repair"}).json()["id"]

    accepted_at = "2026-08-30T12:00:00+00:00"
    pending = PendingSteer(
        message_id="msg_restart_gap",
        session_id=sid,
        parts=[Part(type="text", text="recover me")],
        text="recover me",
        accepted_at=accepted_at,
        metadata={"delivery": "steer", "pending_steer": True},
    )
    response = PostMessageResponse(
        message_id=pending.message_id,
        accepted_at=accepted_at,
        delivery="steer",
        state="pending_steer",
    )
    MessageIntentStore(tmp_path / "message_intents.json").accept_pending(
        pending, "restart-gap", response
    )

    restarted_app = build_app(
        sessions_path=sessions_path,
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(restarted_app) as client:
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        recovered = [row for row in messages if row["id"] == pending.message_id]
        assert len(recovered) == 1
        assert recovered[0]["metadata"]["pending_steer"] is True
        snapshot = client.get(f"/v1/sessions/{sid}/message-state").json()
        assert [row["message_id"] for row in snapshot["pending_steers"]] == [pending.message_id]


def test_queue_is_revisioned_ordered_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "message-intents.json"
    store = MessageIntentStore(path)
    first = store.create_queued(
        QueuedMessage(
            id="queued_1",
            session_id="sess_1",
            idempotency_key="client-key-1",
            parts=[Part(type="text", text="first")],
        )
    )
    second = store.create_queued(
        QueuedMessage(
            id="queued_2",
            session_id="sess_1",
            parts=[Part(type="text", text="second")],
        )
    )
    assert [row.id for row in store.list_queued("sess_1")] == ["queued_1", "queued_2"]
    assert store.find_queued_by_idempotency("sess_1", "client-key-1") == first

    edited = store.update_queued(
        "sess_1",
        first.id,
        first.revision,
        parts=[Part(type="text", text="edited first")],
    )
    assert edited is not None and edited.revision == 2
    with pytest.raises(RevisionConflictError):
        store.update_queued("sess_1", first.id, first.revision, metadata={"stale": True})

    reordered = store.reorder(
        "sess_1",
        [second.id, first.id],
        {second.id: second.revision, first.id: edited.revision},
    )
    assert [row.id for row in reordered] == ["queued_2", "queued_1"]
    assert [row.id for row in MessageIntentStore(path).list_queued("sess_1")] == [
        "queued_2",
        "queued_1",
    ]


def test_queue_promotion_is_atomic_and_retains_row_on_acceptance_failure(tmp_path: Path) -> None:
    store = MessageIntentStore(tmp_path / "message-intents.json")
    queued = store.create_queued(
        QueuedMessage(
            id="queued_atomic",
            session_id="sess_atomic",
            parts=[Part(type="text", text="next")],
        )
    )

    def fail_acceptance(_row: QueuedMessage) -> str:
        raise RuntimeError("acceptance failed")

    with pytest.raises(RuntimeError, match="acceptance failed"):
        store.promote_queued(
            "sess_atomic",
            queued.id,
            queued.revision,
            fail_acceptance,
        )
    assert [row.id for row in store.list_queued("sess_atomic")] == [queued.id]

    promoted = store.promote_queued(
        "sess_atomic",
        queued.id,
        queued.revision,
        lambda row: row.parts[0].text,
    )
    assert promoted is not None and promoted[1] == "next"
    assert store.list_queued("sess_atomic") == []


def test_delete_session_removes_all_intent_planes(tmp_path: Path) -> None:
    path = tmp_path / "message-intents.json"
    store = MessageIntentStore(path)
    pending = PendingSteer(
        message_id="msg_pending",
        session_id="sess_delete",
        parts=[Part(type="text", text="now")],
        text="now",
        accepted_at="2026-08-30T12:00:00+00:00",
    )
    response = PostMessageResponse(
        message_id=pending.message_id,
        accepted_at=pending.accepted_at,
        delivery="steer",
        state="pending_steer",
    )
    store.accept_pending(pending, "accepted", response)
    store.create_queued(
        QueuedMessage(
            id="queued_delete",
            session_id="sess_delete",
            parts=[Part(type="text", text="later")],
        )
    )
    store.create_queued(
        QueuedMessage(
            id="queued_keep",
            session_id="sess_keep",
            parts=[Part(type="text", text="keep")],
        )
    )

    store.delete_session("sess_delete")

    restored = MessageIntentStore(path)
    assert restored.list_pending("sess_delete") == []
    assert restored.list_queued("sess_delete") == []
    assert restored.acceptance("sess_delete", "accepted") is None
    assert [row.id for row in restored.list_queued("sess_keep")] == ["queued_keep"]


def test_corrupt_intent_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "message-intents.json"
    path.write_text(
        json.dumps({"pending": [{"message_id": "missing-required-fields"}]}),
        encoding="utf-8",
    )

    with pytest.raises(IntentStoreReadError, match="invalid pending message intent"):
        MessageIntentStore(path)


def test_queue_routes_keep_future_messages_out_of_transcript_until_promoted(
    tmp_path: Path,
) -> None:
    """A queued row is intent, not transcript state, until something promotes it.

    Queueing is a while-BUSY affordance: on an idle session the head auto-starts
    immediately (a queued message nothing could ever promote was the lifecycle
    gap the composer fix round closed), so this drives the deferred case with a
    turn holding the slot.
    """

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=SlowClioAgent(delay_s=2.0),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "queue"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "hold the slot"}]},
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not app.state.turn_runner.busy(sid):
            time.sleep(0.02)
        assert app.state.turn_runner.busy(sid)

        created = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={
                "text": "future work",
                "client_message_id": "msg_client_future",
                "idempotency_key": "queue-create-1",
                "behavior": {
                    "execution_mode": "plan",
                    "confirmation_policy": "ask",
                    "reasoning_effort": "high",
                },
            },
        )
        assert created.status_code == 201, created.text
        queued = created.json()
        assert queued["position"] == 0
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert all(row["id"] != "msg_client_future" for row in messages)

        replay = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={
                "text": "future work",
                "client_message_id": "another-client-id",
                "idempotency_key": "queue-create-1",
            },
        )
        assert replay.json()["id"] == queued["id"]

        promoted = client.post(
            f"/v1/sessions/{sid}/queued-messages/{queued['id']}/promote",
            json={"revision": queued["revision"], "delivery": "auto"},
        )
        assert promoted.status_code == 200, promoted.text
        acceptance = promoted.json()["acceptance"]
        assert acceptance["message_id"] == "msg_client_future"
        assert acceptance["behavior"]["execution_mode"] == "plan"
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert any(row["id"] == "msg_client_future" for row in messages)


def test_queue_head_starts_automatically_when_active_turn_becomes_idle(tmp_path: Path) -> None:
    agent = SlowClioAgent(delay_s=0.15)
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=agent,
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "auto queue"}).json()["id"]
        started = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "first turn"}]},
        )
        assert started.status_code == 200, started.text
        queued = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={
                "text": "next turn",
                "client_message_id": "msg_auto_queue_next",
                "idempotency_key": "auto-queue-next",
            },
        )
        assert queued.status_code == 201, queued.text

        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            rows = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
            if not rows and len(agent.calls) >= 2:
                break
            time.sleep(0.02)

        assert agent.calls[0][0] == "first turn"
        assert agent.calls[1][0].endswith("\nnext turn")
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert any(row["id"] == "msg_auto_queue_next" for row in messages)


def test_message_state_snapshot_reconciles_transcript_pending_queue_and_cursor(
    tmp_path: Path,
) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=SlowClioAgent(delay_s=2.0),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "snapshot"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "hold the slot"}]},
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not app.state.turn_runner.busy(sid):
            time.sleep(0.02)
        queued = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "later", "client_message_id": "queued_client"},
        )
        assert queued.status_code == 201

        snapshot = client.get(f"/v1/sessions/{sid}/message-state")
        assert snapshot.status_code == 200
        payload = snapshot.json()
        assert payload["protocol_version"] == "0.3"
        assert payload["authoritative"] is True
        assert payload["session"]["id"] == sid
        assert [row["role"] for row in payload["messages"]] == ["user"]
        assert payload["pending_steers"] == []
        assert [row["id"] for row in payload["queued_messages"]] == [queued.json()["id"]]
        # The cursor is directly usable as Last-Event-ID: the highest event id
        # this snapshot already accounts for.
        assert payload["next_cursor"] == app.state.bus.latest_event_id(sid)
        assert payload["dropped_events"] == 0
