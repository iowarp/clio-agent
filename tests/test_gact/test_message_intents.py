"""Focused contract tests for durable steers and queued future messages."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests.test_gact.test_post_messages import BlockingClioAgent, FakeClioAgent, SlowClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_core_queue_edits_reorder_and_idempotency_use_current_revisions(tmp_path: Path) -> None:
    """Exercise public queue commands while model execution is explicitly held."""
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Core queue revisions"}).json()["id"]
        app.state.sessions.update(sid, metadata_patch={"agent_paused": True})
        endpoint = f"/v1/sessions/{sid}/queued-messages"
        first = client.post(endpoint, json={"text": "first", "idempotency_key": "first"})
        second = client.post(endpoint, json={"text": "second", "idempotency_key": "second"})
        assert first.status_code == second.status_code == 201
        original, other = first.json(), second.json()
        edited = client.patch(
            endpoint + "/" + original["id"],
            json={"revision": original["revision"], "text": "edited first"},
        )
        assert edited.status_code == 200, edited.text
        current = edited.json()
        assert current["revision"] == original["revision"] + 1
        stale = client.patch(
            endpoint + "/" + original["id"],
            json={"revision": original["revision"], "text": "stale"},
        )
        assert stale.status_code == 409
        reordered = client.post(
            endpoint + "/reorder",
            json={
                "items": [
                    {"id": other["id"], "revision": other["revision"]},
                    {"id": current["id"], "revision": current["revision"]},
                ]
            },
        )
        assert reordered.status_code == 200, reordered.text
        assert [row["id"] for row in reordered.json()["queued_messages"]] == [
            other["id"],
            current["id"],
        ]
        app.state.task_history.restore()
        rows = client.get(endpoint).json()["queued_messages"]
        assert [row["id"] for row in rows] == [other["id"], current["id"]]
        assert [(part["type"], part.get("text")) for part in rows[1]["parts"]] == [
            ("text", "edited first")
        ]
        replay = client.post(endpoint, json={"text": "first", "idempotency_key": "first"})
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == current["id"]
        assert len(client.get(endpoint).json()["queued_messages"]) == 2
        assert not app.state.task_history.acknowledged_notifications(sid)


def test_queue_promotion_preflight_failure_preserves_core_pending_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure before promotion cannot lose accepted work or acknowledge model input."""
    from fastapi import HTTPException

    from clio_agent.gact import message_submission

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Promotion recovery"}).json()["id"]
        app.state.sessions.update(sid, metadata_patch={"agent_paused": True})
        endpoint = f"/v1/sessions/{sid}/queued-messages"
        created = client.post(
            endpoint, json={"text": "next", "client_message_id": "promotion-input"}
        )
        assert created.status_code == 201, created.text
        queued = created.json()
        original = message_submission.prepare_references_sync

        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise HTTPException(status_code=400, detail="Reference preflight unavailable")

        monkeypatch.setattr(message_submission, "prepare_references_sync", unavailable)
        failed = client.post(
            endpoint + "/" + queued["id"] + "/promote", json={"revision": queued["revision"]}
        )
        assert failed.status_code == 400
        assert client.get(endpoint).json()["queued_messages"] == [queued]
        assert not app.state.task_history.acknowledged_notifications(sid)
        monkeypatch.setattr(message_submission, "prepare_references_sync", original)
        promoted = client.post(
            endpoint + "/" + queued["id"] + "/promote", json={"revision": queued["revision"]}
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["acceptance"]["message_id"] == "promotion-input"
        assert client.get(endpoint).json()["queued_messages"] == []
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert len([row for row in messages if row["id"] == "promotion-input"]) == 1
        assert not app.state.task_history.acknowledged_notifications(sid)


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
            == 202
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
    """Release a known active turn after its successor is durably queued."""
    # Hold the state transition explicitly; a 150 ms sleep can end before enqueue on CI.
    agent = BlockingClioAgent(timeout_s=15.0)
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
        assert started.status_code == 202, started.text
        assert started.json()["task_handle"]
        try:
            assert agent.entered.wait(timeout=10.0), "The first turn never reached the model"
            queued = client.post(
                f"/v1/sessions/{sid}/queued-messages",
                json={
                    "text": "next turn",
                    "client_message_id": "msg_auto_queue_next",
                    "idempotency_key": "auto-queue-next",
                },
            )
            assert queued.status_code == 201, queued.text
            assert len(agent.calls) == 1
            pending = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
            assert [row["id"] for row in pending] == [queued.json()["id"]]
        finally:
            agent.release.set()

        # This bounds asynchronous integration work, not a startup performance target.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
            if not rows and len(agent.calls) >= 2:
                break
            time.sleep(0.02)

        assert len(agent.calls) == 2, (
            f"Queued input never ran: calls={agent.calls!r}, queue={rows!r}"
        )
        assert agent.calls[0][0] == "first turn"
        # Earlier turns reach the model through its projection, never the question.
        assert agent.calls[1][0] == "next turn"
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert any(row["id"] == "msg_auto_queue_next" for row in messages)


def test_message_state_snapshot_reconciles_transcript_pending_queue_and_cursor(
    tmp_path: Path,
) -> None:
    agent = SlowClioAgent(delay_s=2.0)
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=agent,
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "snapshot"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "hold the slot"}]},
            ).status_code
            == 202
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not agent.calls:
            time.sleep(0.02)
        assert agent.calls, "the first input never committed and reached model execution"
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
