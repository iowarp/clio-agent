"""AF1 adversarial-review fix round for the campaign-integration composer wave.

One test per reviewed finding, each written failing-first against the reviewed
head (58547561) before its fix landed.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.ask_user_tool import arm_ask_user_deadline
from clio_agent.gact.types import UserQuestion
from tests._config_layer import set_config

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def _armed_question(app: object, sid: str, question_id: str, *, ttl_s: int = 3600) -> UserQuestion:
    now = datetime.now(timezone.utc)
    row = UserQuestion(
        id=question_id,
        session_id=sid,
        owner_session_id=sid,
        attended_session_id=sid,
        prompt="Which dataset?",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
    )
    app.state.user_questions[row.id] = row
    return row


# --------------------------------------------------------------------------- #
# Finding 2: the ask_user expiry timer is retained and cancelled when settled
# --------------------------------------------------------------------------- #


def test_answering_a_question_cancels_its_armed_expiry_timer(tmp_path) -> None:
    """A settled question must not leave a live daemon timer for its whole TTL."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="ask")
    row = _armed_question(app, session.id, "q_settled")
    app.state.sessions.update(
        session.id,
        status="waiting_user",
        metadata_patch={"pending_user_question_id": row.id},
    )

    threads_before = threading.active_count()
    arm_ask_user_deadline(app, row)
    assert row.id in app.state.ask_user_deadlines
    timer = app.state.ask_user_deadlines[row.id]

    with TestClient(app) as client:
        answered = client.post(
            f"/v1/sessions/{session.id}/questions/{row.id}/answer",
            headers=HEADERS,
            json={"answer": "the beam"},
        )
        assert answered.status_code == 200

    assert app.state.user_questions[row.id].status == "answered"
    assert row.id not in app.state.ask_user_deadlines
    timer.join(timeout=5.0)
    assert not timer.is_alive()
    assert threading.active_count() <= threads_before


def test_cancelling_a_question_cancels_its_armed_expiry_timer(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="ask")
    row = _armed_question(app, session.id, "q_cancelled")

    arm_ask_user_deadline(app, row)
    timer = app.state.ask_user_deadlines[row.id]

    with TestClient(app) as client:
        cancelled = client.post(
            f"/v1/sessions/{session.id}/questions/{row.id}/cancel", headers=HEADERS
        )
        assert cancelled.status_code == 200

    assert row.id not in app.state.ask_user_deadlines
    timer.join(timeout=5.0)
    assert not timer.is_alive()


def test_ask_user_ttl_default_and_clamp_are_config_resolved() -> None:
    from clio_agent.gact import ask_user_tool

    set_config("gact.ask_user.ttl_s", 42)
    set_config("gact.ask_user.max_ttl_s", 120)
    assert ask_user_tool.ask_user_ttl_bounds() == (42, 120)
