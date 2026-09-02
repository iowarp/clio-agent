"""Adversarial-review fix round for the re-landed composer lanes.

One module per finding group, failing-first. The lanes under test are the
message-intent planes (``message_intents`` / ``message_submission`` /
``routes.message_intents``), the loop-inbox steer carrier, and the composer
runtime wiring (``composer_runtime``).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.loop_inbox import (
    USER_STEER_MARKER,
    drain_active_session_inbox,
    inbox_for,
)
from clio_agent.gact.protocol.v3.composer import queue_entity_id
from clio_agent.gact.protocol.v3.event import event_to_v3
from tests.test_gact.test_loop_inbox_1036 import _active_turn, _SlowAgent, _wait_busy, _wait_idle
from tests.test_gact.test_post_messages import FakeClioAgent
from tests.test_gact.test_resources import _upload, _workspace

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _RecordingSlowAgent:
    """Holds a turn open long enough to steer/queue against, and records calls."""

    def __init__(self, delay_s: float = 1.0) -> None:
        self.delay_s = delay_s
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        time.sleep(self.delay_s)
        return SimpleNamespace(answer="done", selected_expert="", routing_rationale="")


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


# --------------------------------------------------------------------------- #
# F4 — the composer events must reach a 0.3 client as 0.3 envelopes.            #
# --------------------------------------------------------------------------- #


def _projected(app: Any, sid: str, event_type: str) -> list[dict[str, Any]]:
    session = app.state.sessions.get(sid)
    return [
        event_to_v3(event, session=session)
        for event in app.state.bus.session_events_since(sid, cursor=1)
        if event.type == event_type
    ]


def test_every_composer_event_carries_an_entity_and_a_v3_payload(tmp_path: Path) -> None:
    """The queue events project; none ships a 0.2 payload or a null entity id."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=4.0))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "v3"}).json()["id"]
        # Queueing is a while-busy affordance: hold a turn so the rows survive
        # long enough to be edited, reordered and deleted.
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "hold the slot"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        first = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "one", "client_message_id": "queued_one"},
        ).json()
        second = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "two", "client_message_id": "queued_two"},
        ).json()
        updated = client.patch(
            f"/v1/sessions/{sid}/queued-messages/{first['id']}",
            json={"revision": first["revision"], "metadata": {"note": "edited"}},
        ).json()
        reordered = client.post(
            f"/v1/sessions/{sid}/queued-messages/reorder",
            json={
                "ordered_ids": [second["id"], first["id"]],
                "revisions": {second["id"]: second["revision"], first["id"]: updated["revision"]},
            },
        )
        assert reordered.status_code == 200, reordered.text
        head = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"][0]
        assert (
            client.delete(
                f"/v1/sessions/{sid}/queued-messages/{head['id']}?revision={head['revision']}"
            ).status_code
            == 204
        )
        survivor = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"][0]
        promoted = client.post(
            f"/v1/sessions/{sid}/queued-messages/{survivor['id']}/promote",
            json={"revision": survivor["revision"]},
        )
        assert promoted.status_code == 200, promoted.text

        created = _projected(app, sid, "queued_message.created")
        assert created and created[0]["type"] == "queued_message.upserted"
        assert created[0]["entity_id"] == "queued_one"
        assert [block["type"] for block in created[0]["payload"]["blocks"]] == ["text"]
        assert created[0]["payload"]["revision"] == 1

        edited = _projected(app, sid, "queued_message.updated")
        assert edited and edited[0]["entity_id"] == "queued_one"
        assert edited[0]["payload"]["revision"] == 2

        order = _projected(app, sid, "queued_message.reordered")
        assert order and order[0]["type"] == "queued_message.reordered"
        assert order[0]["entity_id"] == queue_entity_id(sid)
        assert order[0]["payload"]["ordered_ids"] == ["queued_two", "queued_one"]
        assert [row["revision"] for row in order[0]["payload"]["queued_messages"]]

        deleted = _projected(app, sid, "queued_message.deleted")
        assert deleted and deleted[0]["entity_id"] == "queued_two"
        assert deleted[0]["payload"]["deleted_at"]

        promotion = _projected(app, sid, "queued_message.promoted")
        assert promotion and promotion[0]["entity_id"] == "queued_one"
        assert promotion[0]["payload"]["message_id"] == "queued_one"
        # Promoted against a running turn, so acceptance lands as a steer.
        assert promotion[0]["payload"]["state"] == "pending_steer"

        for row in created + edited + order + deleted + promotion:
            assert row["protocol_version"] == "0.3"
            assert row["entity_revision"] > 0
        _wait_idle(app, sid)


def test_message_accepted_is_uniform_across_start_and_steer(tmp_path: Path) -> None:
    """A client listening for acceptance sees it for a start, not only a steer."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "accepted"}).json()["id"]
        started = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "start"}], "client_message_id": "msg_start"},
        )
        assert started.status_code == 200
        _wait_busy(app, sid)
        steered = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "steer"}], "client_message_id": "msg_steer"},
        )
        assert steered.status_code == 202

        accepted = _projected(app, sid, "message.accepted")
        by_id = {row["entity_id"]: row for row in accepted}
        assert set(by_id) == {"msg_start", "msg_steer"}
        assert by_id["msg_start"]["payload"]["delivery"] == "start"
        assert by_id["msg_start"]["payload"]["state"] == "started"
        assert by_id["msg_steer"]["payload"]["delivery"] == "steer"
        assert by_id["msg_steer"]["payload"]["state"] == "pending_steer"
        for row in accepted:
            assert row["type"] == "message.accepted"
            assert row["payload"]["message"]["blocks"][0]["type"] == "text"


def test_cancelled_message_and_steer_intent_both_project(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "cancelled"}).json()["id"]
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
            json={"parts": [{"type": "text", "text": "drop me"}]},
        ).json()["message_id"]
        assert client.delete(f"/v1/sessions/{sid}/pending-steers/{steer_id}").status_code == 200

        cancelled = _projected(app, sid, "message.cancelled")
        assert cancelled and cancelled[0]["entity_id"] == steer_id
        assert cancelled[0]["payload"]["cancelled_at"]
        intent = _projected(app, sid, "pending_steer.cancelled")
        assert intent and intent[0]["entity_id"] == steer_id
        assert intent[0]["payload"]["state"] == "cancelled"


def test_v3_session_projection_carries_the_cancellation_envelope(tmp_path: Path) -> None:
    """The typed cancellation honesty the same code computes must reach 0.3."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "honesty"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204

        rows = _projected(app, sid, "session.status_changed")
        carrying = [row for row in rows if "cancellation" in row["payload"]]
        assert carrying, "the v3 session projection dropped the cancellation envelope"
        cancellation = carrying[-1]["payload"]["cancellation"]
        assert cancellation["execution_cancellation"] == "cooperative_pending"
        assert cancellation["executor_work_may_continue"] is True
        assert cancellation["cancellation_attempt"]
        assert cancellation["composer_autostart"]["reason"] == "session_cancelled"
        _wait_idle(app, sid)


# --------------------------------------------------------------------------- #
# F5 — a recovered pending steer must actually be deliverable.                  #
# --------------------------------------------------------------------------- #


def test_a_recovered_pending_steer_is_delivered_after_a_restart(tmp_path: Path) -> None:
    """Restoring the transcript row is only half of recovery.

    ``LoopInbox`` is in-memory, so a durable ``PendingSteer`` with no re-enqueued
    inbox event is stranded: no drain and no idle re-drive ever looks at it, and
    the user's accepted message is silently never delivered.
    """

    sessions_path = tmp_path / "s.json"
    first = build_app(sessions_path=sessions_path, agent=_SlowAgent(sleep_s=1.5))
    with TestClient(first) as client:
        sid = client.post("/v1/sessions", json={"title": "restart"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(first, sid)
        steer_id = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "recovered steer"}]},
        ).json()["message_id"]

    agent = FakeClioAgent(answer="after restart")
    restarted = build_app(sessions_path=sessions_path, agent=agent)
    assert inbox_for(restarted, sid).peek_nonempty(), "recovery did not re-enqueue the steer"

    with TestClient(restarted) as client:
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "second"}]},
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and len(agent.calls) < 2:
            time.sleep(0.05)

    assert len(agent.calls) >= 2, "the recovered steer never reached the model"
    assert "recovered steer" in agent.calls[1][0]
    assert restarted.state.message_intents.get_pending(sid, steer_id) is None or (
        restarted.state.message_intents.get_pending(sid, steer_id).state == "consumed"  # type: ignore[union-attr]
    )


# --------------------------------------------------------------------------- #
# F6 — the queue must never freeze with promotable work in it.                  #
# --------------------------------------------------------------------------- #


def test_queueing_on_an_idle_session_starts_the_head_immediately(tmp_path: Path) -> None:
    """The only auto-promoter used to be the turn-done hook.

    A message queued while the session is idle therefore sat forever: no turn was
    running, so no turn could end and promote it.
    """

    agent = FakeClioAgent(answer="promoted from idle")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "idle queue"}).json()["id"]
        created = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "start me", "client_message_id": "msg_idle_queue"},
        )
        assert created.status_code == 201, created.text

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and agent.calls == []:
            time.sleep(0.03)
        assert agent.calls, "a queued message on an idle session was never promoted"
        assert "start me" in agent.calls[0][0]
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []
        assert any(row["id"] == "msg_idle_queue" for row in _user_messages(client, sid))


def test_a_revision_race_retries_once_instead_of_freezing_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing edit bumps the head's revision between the read and the promote."""

    from clio_agent.gact.message_intents import MessageIntentStore, RevisionConflictError

    agent = FakeClioAgent(answer="won the retry")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "race"}).json()["id"]
        real = MessageIntentStore.promote_queued
        state = {"raised": False}

        def flaky(self: Any, session_id: str, message_id: str, revision: int, promote: Any) -> Any:
            if not state["raised"]:
                state["raised"] = True
                current = self.get_queued(session_id, message_id)
                raise RevisionConflictError(current)
            return real(self, session_id, message_id, revision, promote)

        monkeypatch.setattr(MessageIntentStore, "promote_queued", flaky)
        created = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "retry me", "client_message_id": "msg_race"},
        )
        assert created.status_code == 201, created.text

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and agent.calls == []:
            time.sleep(0.03)

    assert state["raised"], "the conflict path never ran"
    assert agent.calls, "the queue froze on a stale revision instead of re-reading the head"
    assert "retry me" in agent.calls[0][0]


def test_a_failed_promotion_stays_typed_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion failure must not freeze the queue until an unrelated turn ends."""

    from clio_agent.gact import composer_runtime

    agent = FakeClioAgent(answer="second chance")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "failed"}).json()["id"]
        state = {"fail": True}
        import clio_agent.gact.message_submission as submission

        real_accept = submission.accept_message

        def flaky_accept(*args: Any, **kwargs: Any) -> Any:
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("simulated acceptance failure")
            return real_accept(*args, **kwargs)

        monkeypatch.setattr(submission, "accept_message", flaky_accept)
        created = client.post(
            f"/v1/sessions/{sid}/queued-messages",
            json={"text": "eventually", "client_message_id": "msg_failed_once"},
        )
        assert created.status_code == 201, created.text
        assert state["fail"] is False, "the failing promotion never ran"
        rows = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
        assert [row["id"] for row in rows] == ["msg_failed_once"], "the durable row was lost"

        failures = _projected(app, sid, "queued_message.promotion_failed")
        assert failures, "a failed promotion published no typed reason"
        assert failures[0]["entity_id"] == "msg_failed_once"
        assert failures[0]["payload"]["recoverable"] is True
        assert "queue_mutation" in failures[0]["payload"]["retry_on"]

        # A later queue MUTATION re-drives it rather than waiting for a turn that
        # may never run.
        head = rows[0]
        patched = client.patch(
            f"/v1/sessions/{sid}/queued-messages/{head['id']}",
            json={"revision": head["revision"], "metadata": {"nudge": True}},
        )
        assert patched.status_code == 200, patched.text
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and agent.calls == []:
            time.sleep(0.03)
        assert agent.calls, "the queue never retried after the typed failure"
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []
    del composer_runtime


def test_an_unclaimable_steer_does_not_strand_the_steers_behind_it(tmp_path: Path) -> None:
    """The idle re-drive used to `return` on a claim miss, freezing the rest."""

    agent = _RecordingSlowAgent(delay_s=0.8)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "strand"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        _wait_busy(app, sid)
        first_steer = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "s1"}]},
        ).json()["message_id"]
        client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "s2"}]},
        )
        # A concurrent consumer owns the oldest steer; the idle re-drive must move
        # on to the next identity rather than giving up on the whole pass.
        assert app.state.message_intents.claim_pending(sid, first_steer) is not None

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and len(agent.calls) < 2:
            time.sleep(0.05)

    assert len(agent.calls) >= 2, "the steers behind an unclaimable one were stranded"
    assert "s2" in agent.calls[1][0]
