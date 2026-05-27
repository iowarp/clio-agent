from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part, Tokens


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def _create_session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "ask user"}).json()["id"]


def _seed_message(client: TestClient, sid: str, message_id: str = "msg_seed") -> None:
    now = datetime.now(timezone.utc).isoformat()
    msg = Message(
        id=message_id,
        session_id=sid,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=[Part(id="part_seed", type="text", text="original answer")],
        tokens=Tokens(output=12),
        stop_reason="end_turn",
    )
    client.app.state.messages[sid] = [msg]
    client.app.state.message_store.replace_session(sid, [msg])


def test_capabilities_advertise_ask_user_and_retry(client: TestClient) -> None:
    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_user_questions"] is True
    assert caps["x_clio_retry_attempts"] is True


def test_create_and_answer_user_question_updates_session_state(client: TestClient) -> None:
    sid = _create_session(client)

    created = client.post(
        f"/v1/sessions/{sid}/questions",
        json={
            "prompt": "Which dataset should I inspect?",
            "kind": "choice",
            "options": [
                {"label": "Small", "value": "small"},
                {"label": "Large", "value": "large"},
            ],
            "turn_id": "msg_user_1",
            "attempt_id": "att_1",
            "metadata": {"reason": "ambiguous_dataset"},
        },
    )

    assert created.status_code == 201, created.text
    question = created.json()
    assert question["status"] == "pending"
    assert question["session_id"] == sid
    assert question["metadata"]["reason"] == "ambiguous_dataset"
    session = client.get(f"/v1/sessions/{sid}").json()
    assert session["status"] == "waiting_user"
    assert session["metadata"]["pending_user_question_id"] == question["id"]
    history = client.app.state.bus._history.get(sid, [])
    assert "user_question.created" in [event.type for event in history]

    listed = client.get(f"/v1/sessions/{sid}/questions?status=pending").json()
    assert [q["id"] for q in listed["questions"]] == [question["id"]]

    answered = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer",
        json={
            "answer": "Use the large run.",
            "selected_options": ["large"],
            "metadata": {"answered_from": "tui"},
        },
    )

    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["status"] == "answered"
    assert body["answer"] == "Use the large run."
    assert body["selected_options"] == ["large"]
    assert body["answer_metadata"]["answered_from"] == "tui"
    session = client.get(f"/v1/sessions/{sid}").json()
    assert session["status"] == "idle"
    assert session["metadata"]["pending_user_question_id"] == ""
    history = client.app.state.bus._history.get(sid, [])
    assert "user_question.answered" in [event.type for event in history]


def test_answer_rejects_invalid_choice(client: TestClient) -> None:
    sid = _create_session(client)
    question = client.post(
        f"/v1/sessions/{sid}/questions",
        json={
            "prompt": "Pick one",
            "kind": "choice",
            "options": [{"label": "A", "value": "a"}],
        },
    ).json()

    resp = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer",
        json={"selected_options": ["b"]},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "bad_request"


def test_cancel_last_pending_question_returns_session_to_idle(client: TestClient) -> None:
    sid = _create_session(client)
    question = client.post(
        f"/v1/sessions/{sid}/questions",
        json={"prompt": "Continue?", "kind": "confirmation"},
    ).json()

    assert question["options"] == [
        {"label": "Yes", "value": "yes", "description": ""},
        {"label": "No", "value": "no", "description": ""},
    ]

    resp = client.post(f"/v1/sessions/{sid}/questions/{question['id']}/cancel")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert client.get(f"/v1/sessions/{sid}").json()["status"] == "idle"


def test_retry_records_attempt_with_model_change_warning(client: TestClient) -> None:
    sid = _create_session(client)
    _seed_message(client, sid, "msg_original")

    resp = client.post(
        f"/v1/sessions/{sid}/messages/msg_original/retry",
        json={
            "notes": "Retry with a more capable model.",
            "provider_id": "openai",
            "model_id": "gpt-5.1",
            "metadata": {"requested_by": "user"},
        },
    )

    assert resp.status_code == 202, resp.text
    attempt = resp.json()
    assert attempt["session_id"] == sid
    assert attempt["source_message_id"] == "msg_original"
    assert attempt["status"] == "recorded"
    assert attempt["notes"] == "Retry with a more capable model."
    assert attempt["model"]["provider_id"] == "openai"
    assert attempt["model"]["model_id"] == "gpt-5.1"
    assert "increase time to first token" in attempt["warning"]
    assert attempt["metadata"]["source_message_role"] == "assistant"
    history = client.app.state.bus._history.get(sid, [])
    assert "turn.retry_requested" in [event.type for event in history]

    listed = client.get(f"/v1/sessions/{sid}/attempts").json()
    assert [row["id"] for row in listed["attempts"]] == [attempt["id"]]
