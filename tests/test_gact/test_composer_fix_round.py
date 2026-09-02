"""Adversarial-review fix round for the re-landed composer lanes.

One module per finding group, failing-first. The lanes under test are the
message-intent planes (``message_intents`` / ``message_submission`` /
``routes.message_intents``), the loop-inbox steer carrier, and the composer
runtime wiring (``composer_runtime``).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.loop_inbox import (
    USER_STEER_MARKER,
    drain_active_session_inbox,
    inbox_for,
)
from tests.test_gact.test_loop_inbox_1036 import _active_turn, _SlowAgent, _wait_busy, _wait_idle
from tests.test_gact.test_post_messages import FakeClioAgent
from tests.test_gact.test_resources import _upload, _workspace

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _user_messages(client: TestClient, sid: str) -> list[dict[str, Any]]:
    return [
        row
        for row in client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        if row["role"] == "user"
    ]


# --------------------------------------------------------------------------- #
# F1 — an attachment-only steer must reach the model, and a claim must not      #
#      strand the intent uncancellable.                                         #
# --------------------------------------------------------------------------- #


def test_attachment_only_steer_reaches_the_model_and_settles(tmp_path: Path) -> None:
    """A steer carrying only a resource_ref is legitimate user intent.

    Pre-fix the drain claimed the intent and then dropped it (``if steer_text``),
    so the model never saw the attachment, ``pending_steer`` stayed True forever,
    and cancelling returned 409 ``steer_already_claimed``.
    """

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=2.0))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        sid = client.post(
            "/v1/sessions", json={"title": "attach", "workspace_id": workspace_id}
        ).json()["id"]
        ready = _upload(
            client,
            workspace_id,
            name="evidence.md",
            content=b"# numbers\n",
            media_type="text/markdown",
        )
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        accepted = client.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "parts": [
                    {
                        "type": "resource_ref",
                        "resource_id": ready["id"],
                        "resource_revision": "1",
                    }
                ]
            },
        )
        assert accepted.status_code == 202, accepted.text
        steer_id = accepted.json()["message_id"]

        with _active_turn(app, sid):
            block = drain_active_session_inbox(app)

        assert USER_STEER_MARKER in block, "an attachment-only steer never reached the model"
        assert "evidence.md" in block, "the steer block does not reference the attached resource"

        settled = {row["id"]: row for row in _user_messages(client, sid)}[steer_id]
        assert settled["metadata"]["pending_steer"] is False
        assert settled["metadata"]["mid_turn_steer"] is True
        assert app.state.message_intents.get_pending(sid, steer_id).state == "consumed"  # type: ignore[union-attr]


def test_steer_with_no_text_and_no_parts_is_refused_typed(tmp_path: Path) -> None:
    """An empty submission is refused at POST rather than accepted and stranded."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "empty"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        refused = client.post(f"/v1/sessions/{sid}/messages", json={"parts": []})
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["error"] == "validation_error"
        assert app.state.message_intents.list_pending(sid) == []


def test_cancel_reaches_a_claimed_but_unconsumed_steer(tmp_path: Path) -> None:
    """A claim that never settles must stay cancellable, not 409 forever."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=2.0))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "claimed"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        accepted = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "steer"}]},
        )
        assert accepted.status_code == 202
        steer_id = accepted.json()["message_id"]
        # Simulate a consumer that claimed the intent and then died before it
        # settled (a crashed drain, a torn-down turn).
        assert app.state.message_intents.claim_pending(sid, steer_id) is not None

        cancelled = client.delete(f"/v1/sessions/{sid}/pending-steers/{steer_id}")
        assert cancelled.status_code == 200, cancelled.text
        assert app.state.message_intents.get_pending(sid, steer_id).state == "cancelled"  # type: ignore[union-attr]
        assert all(row["id"] != steer_id for row in _user_messages(client, sid))


def test_a_cancelled_steer_is_never_surfaced_by_a_racing_drain(tmp_path: Path) -> None:
    """Consumption, not the claim, is the gate: a cancelled steer surfaces nothing."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=2.0))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "race"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        steer_id = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "cancel me"}]},
        ).json()["message_id"]
        # A drain already holding the popped event races the cancel: re-enqueue
        # the same identity after the cancel route cleared the inbox.
        buffered = inbox_for(app, sid).drain()
        assert client.delete(f"/v1/sessions/{sid}/pending-steers/{steer_id}").status_code == 200
        for event in buffered:
            inbox_for(app, sid).put(event)

        with _active_turn(app, sid):
            block = drain_active_session_inbox(app)

        assert block == "", "a cancelled steer was still surfaced to the model"
        assert not inbox_for(app, sid).peek_nonempty()
        assert app.state.message_intents.get_pending(sid, steer_id).state == "cancelled"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# F2 — cancel must stop the composer producers, not start them.                 #
# --------------------------------------------------------------------------- #


def test_cancel_does_not_auto_start_the_queued_head(tmp_path: Path) -> None:
    """Esc must not restart the agent from the queue, and must keep the queue."""

    agent = _SlowAgent(sleep_s=1.5)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "cancel"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        queued = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "do not auto start", "client_message_id": "msg_queued_cancel"},
        )
        assert queued.status_code == 201, queued.text

        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        _wait_idle(app, sid)
        time.sleep(0.4)

        assert not app.state.turn_runner.busy(sid), "cancel restarted the agent from the queue"
        assert client.get(f"/v1/sessions/{sid}").json()["status"] == "cancelled"
        rows = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
        assert [row["id"] for row in rows] == [queued.json()["id"]], (
            "cancelling a session must not delete its queued messages"
        )
        assert all(row["id"] != "msg_queued_cancel" for row in _user_messages(client, sid))


def test_cancel_leaves_a_residual_steer_buffered_instead_of_restarting(tmp_path: Path) -> None:
    """A residual steer must not re-drive a new turn on a cancelled session."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.0))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "cancel steer"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        steer_id = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "steer"}]},
        ).json()["message_id"]
        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204

        _wait_idle(app, sid)
        time.sleep(0.4)
        assert not app.state.turn_runner.busy(sid)
        assert client.get(f"/v1/sessions/{sid}").json()["status"] == "cancelled"
        listed = client.get(f"/v1/sessions/{sid}/pending-steers").json()["pending_steers"]
        assert [row["message_id"] for row in listed] == [steer_id]


def test_an_explicit_send_after_cancel_resumes_queue_promotion(tmp_path: Path) -> None:
    """The suspension lifts on the user's next explicit message."""

    agent = FakeClioAgent(answer="resumed")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "resume"}).json()["id"]
        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        queued = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "later", "client_message_id": "msg_queued_resume"},
        )
        assert queued.status_code == 201
        time.sleep(0.2)
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]

        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "carry on"}]},
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]:
                break
            time.sleep(0.05)
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []
        assert any(row["id"] == "msg_queued_resume" for row in _user_messages(client, sid))
