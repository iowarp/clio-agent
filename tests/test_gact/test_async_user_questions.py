"""Explicit blocking/async questions, rich surfaces, and ordinary queued answers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.ask_user_tool import AskUserError, build_ask_user_tool
from clio_agent.gact.types import AgentDef, Message, Part
from clio_agent.gact.user_question_ledger import restore_user_questions


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as result:
        yield result


def _ask_tool(client: TestClient, sid: str, **kwargs: Any) -> str:
    tokens = [
        ctx.set_app(client.app),
        ctx.set_session_id(sid),
        ctx.set_turn_id_token("turn-questions"),
    ]
    try:
        tool = build_ask_user_tool(AgentDef(id="main", title="CLIO", tools=["ask_user"]))
        return tool.func(**kwargs)
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def test_async_tool_keeps_work_running_and_multiple_questions_survive_recovery(
    client: TestClient,
) -> None:
    app = client.app
    sid = client.post("/v1/sessions", json={"title": "Questions"}).json()["id"]
    app.state.sessions.update(sid, status="running")
    for question in ["Which view?", "What does X mean?"]:
        result = _ask_tool(client, sid, question=question, response_mode="async")
        assert "continue working" in result
    assert app.state.sessions.get(sid).status == "running"
    assert not app.state.sessions.get(sid).metadata.get("pending_ask_user")
    rows = list(app.state.user_questions.values())
    assert [row.response_mode for row in rows] == ["async", "async"]
    assert len({row.id for row in rows}) == 2
    assert all(row.turn_id == "turn-questions" for row in rows)
    app.state.user_questions.clear()
    assert restore_user_questions(app) == 2
    assert all(row.status == "pending" for row in app.state.user_questions.values())


def test_default_tool_still_yields_blocking_question(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    result = _ask_tool(client, sid, question="Which dataset?")
    assert "END YOUR TURN" in result
    assert client.app.state.sessions.get(sid).metadata["pending_ask_user"]["surfaced"] is False
    assert not client.app.state.user_questions


@pytest.mark.parametrize("mode", ["later", "ASYNC", ""])
def test_unknown_modes_are_rejected(client: TestClient, mode: str) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    with pytest.raises(AskUserError, match="response_mode"):
        _ask_tool(client, sid, question="Explain X?", response_mode=mode)


def test_question_surface_must_exist_in_owning_session(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    with pytest.raises(AskUserError, match="existing surface"):
        _ask_tool(client, sid, question="This image?", surface_id="missing", response_mode="async")


def test_async_answer_joins_fifo_and_does_not_interrupt_running_work(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    app = client.app
    monkeypatch.setattr(app.state.turn_runner, "busy", lambda _sid: True)
    app.state.sessions.update(sid, status="running")
    first = client.post(
        f"/v1/sessions/{sid}/queued-messages", json={"text": "Existing message"}
    ).json()
    _ask_tool(client, sid, question="What does X mean?", response_mode="async")
    question = next(iter(app.state.user_questions.values()))
    response = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer", json={"answer": "X means stiffness."}
    )
    assert response.status_code == 200, response.text
    rows = client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"]
    assert [row["id"] for row in rows] == [
        first["id"],
        response.json()["answer_metadata"]["queued_message_id"],
    ]
    assert rows[1]["parts"][0]["text"].endswith("Answer: X means stiffness.")
    assert rows[1]["metadata"]["ask_user_question_id"] == question.id
    assert app.state.sessions.get(sid).status == "running"
    duplicate = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer", json={"answer": "Duplicate"}
    )
    assert duplicate.status_code == 409
    assert len(app.state.message_intents.list_queued(sid)) == 2


def test_cancelling_async_question_never_stops_running_work(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    client.app.state.sessions.update(sid, status="running")
    _ask_tool(client, sid, question="Explain X?", response_mode="async")
    question = next(iter(client.app.state.user_questions.values()))
    assert client.post(f"/v1/sessions/{sid}/questions/{question.id}/cancel").status_code == 200
    assert client.app.state.sessions.get(sid).status == "running"


def test_async_question_route_and_projection_do_not_pause_session(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    response = client.post(
        f"/v1/sessions/{sid}/questions", json={"prompt": "Explain X?", "response_mode": "async"}
    )
    assert response.status_code == 201, response.text
    assert client.app.state.sessions.get(sid).status == "idle"
    interaction = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"][0]
    assert interaction["payload"]["response_mode"] == "async"


@pytest.mark.parametrize("answer_action", ["question.submit", "", "   "])
def test_surface_action_answers_async_question_while_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answer_action: str
) -> None:
    from tests.test_gact.test_a2ui_actions import _event
    from tests.test_gact.test_a2ui_v3 import HEADERS, _create_message

    sid = client.post("/v1/sessions", json={}).json()["id"]
    result = client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    assert result.status_code == 200, result.text
    app = client.app
    monkeypatch.setattr(app.state.turn_runner, "busy", lambda _sid: True)
    app.state.sessions.update(sid, status="running")
    _ask_tool(
        client,
        sid,
        question="What does X mean?",
        response_mode="async",
        surface_id="surface_1",
        answer_action=answer_action,
    )
    question = next(iter(app.state.user_questions.values()))
    assert question.metadata["a2ui_answer_action"] == "question.submit"
    _ask_tool(client, sid, question="Another question", response_mode="async")
    with pytest.raises(AskUserError, match="already belongs"):
        _ask_tool(client, sid, question="Duplicate", response_mode="async", surface_id="surface_1")
    interaction = next(
        row
        for row in client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"]
        if row["payload"].get("question_id") == question.id
    )
    assert interaction["source"]["surface_id"] == "surface_1"
    action = _event(
        "question.submit", "surface_1", {"explanation": "X is stiffness"}, "2026-10-08T00:00:00Z"
    )
    explored = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={
            "message": _event("chart.inspect", "surface_1", {"point": 3}, "2026-10-08T00:00:00Z")
        },
    )
    assert explored.status_code == 200, explored.text
    assert app.state.user_questions[question.id].status == "pending"
    assert not app.state.message_intents.list_queued(sid)
    other_question = next(q for q in app.state.user_questions.values() if q.id != question.id)
    crossed = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={
            "message": _event(
                "question.submit",
                "surface_1",
                {"question_id": other_question.id},
                "2026-10-08T00:00:01Z",
            )
        },
    )
    assert crossed.status_code == 200, crossed.text
    assert app.state.user_questions[other_question.id].status == "pending"
    assert app.state.user_questions[question.id].status == "pending"
    answered = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert answered.status_code == 200, answered.text
    row = app.state.user_questions[question.id]
    assert row.status == "answered"
    queued = app.state.message_intents.list_queued(sid)
    assert queued[0].metadata["a2ui_action_context"] == {"explanation": "X is stiffness"}
    assert app.state.sessions.get(sid).status == "running"
    assert len([q for q in app.state.user_questions.values() if q.status == "pending"]) == 1
    replay = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={
            "message": _event(
                "question.submit",
                "surface_1",
                {"explanation": "Changed answer"},
                "2026-10-08T00:00:02Z",
            )
        },
    )
    assert replay.status_code == 409, replay.text
    assert "user question is already resolved" in replay.text
    assert len(app.state.message_intents.list_queued(sid)) == 1


def test_blocking_answer_cannot_be_queued(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    question = client.post(f"/v1/sessions/{sid}/questions", json={"prompt": "Units?"}).json()
    response = client.post(
        f"/v1/sessions/{sid}/queued-messages",
        json={"text": "Meters", "metadata": {"answers_question_id": question["id"]}},
    )
    assert response.status_code == 422, response.text
    assert client.app.state.user_questions[question["id"]].status == "pending"
    assert not client.app.state.message_intents.list_queued(sid)


def test_queued_answer_must_belong_to_owning_conversation(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    other = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(client, other, question="Explain X?", response_mode="async")
    question = next(iter(client.app.state.user_questions.values()))
    response = client.post(
        f"/v1/sessions/{sid}/queued-messages",
        json={"text": "Stiffness", "metadata": {"answers_question_id": question.id}},
    )
    assert response.status_code == 404, response.text
    assert client.app.state.user_questions[question.id].status == "pending"
    assert not client.app.state.message_intents.list_queued(sid)


def test_client_delivery_metadata_cannot_skip_async_answer_queue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    monkeypatch.setattr(client.app.state.turn_runner, "busy", lambda _sid: True)
    _ask_tool(client, sid, question="Explain X?", response_mode="async")
    question = next(iter(client.app.state.user_questions.values()))
    response = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer",
        json={
            "answer": "Stiffness",
            "metadata": {"queued_message_id": "forged", "answer_message_id": "forged"},
        },
    )
    assert response.status_code == 200, response.text
    queued = client.app.state.message_intents.list_queued(sid)
    assert len(queued) == 1
    assert response.json()["answer_metadata"]["queued_message_id"] == queued[0].id
    assert "answer_message_id" not in response.json()["answer_metadata"]


def test_async_answer_preserves_planning_and_confirmation_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = client.post("/v1/sessions", json={"mode": "plan", "approval_mode": "auto-edits"}).json()[
        "id"
    ]
    monkeypatch.setattr(client.app.state.turn_runner, "busy", lambda _sid: True)
    _ask_tool(client, sid, question="Explain X?", response_mode="async")
    question = next(iter(client.app.state.user_questions.values()))
    result = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer", json={"answer": "Stiffness"}
    )
    assert result.status_code == 200, result.text
    queued = client.app.state.message_intents.list_queued(sid)
    assert queued[0].behavior.execution_mode == "plan"
    assert queued[0].behavior.confirmation_policy == "auto-edits"


def test_reserved_metadata_refusal_keeps_async_question_open(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(client, sid, question="Explain X?", response_mode="async")
    question = next(iter(client.app.state.user_questions.values()))
    response = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer",
        json={"answer": "Stiffness", "metadata": {"ask_user_resume": True}},
    )
    assert response.status_code == 400, response.text
    assert client.app.state.user_questions[question.id].status == "pending"
    assert not client.app.state.message_intents.list_queued(sid)


def test_async_answer_preserves_source_model_and_per_message_behavior(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    session = client.app.state.sessions.get(sid)
    client.app.state.messages[sid] = [
        Message(
            id="turn-questions",
            session_id=sid,
            role="user",
            created_at=session.created_at,
            updated_at=session.updated_at,
            parts=[Part(type="text", text="Plan this analysis.")],
            metadata={
                "effective_model": {"provider_id": "codex", "model_id": "gpt-6-luna"},
                "behavior": {
                    "execution_mode": "plan",
                    "confirmation_policy": "auto-edits",
                    "reasoning_effort": "off",
                },
            },
        )
    ]
    monkeypatch.setattr(client.app.state.turn_runner, "busy", lambda _sid: True)
    _ask_tool(client, sid, question="Explain X?", response_mode="async")
    question = next(iter(client.app.state.user_questions.values()))
    result = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer", json={"answer": "Stiffness"}
    )
    assert result.status_code == 200, result.text
    queued = client.app.state.message_intents.list_queued(sid)[0]
    assert queued.model.provider_id == "codex"
    assert queued.model.model_id == "gpt-6-luna"
    assert queued.behavior.execution_mode == "plan"
    assert queued.behavior.reasoning_effort == "off"


def test_full_queue_refuses_answer_without_settling_question(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.message_intents import IntentRetention, MessageIntentStore

    app = client.app
    app.state.message_intents = MessageIntentStore(
        tmp_path / "intents.json", retention=IntentRetention(1, 10, 10)
    )
    sid = client.post("/v1/sessions", json={}).json()["id"]
    monkeypatch.setattr(app.state.turn_runner, "busy", lambda _sid: True)
    client.post(f"/v1/sessions/{sid}/queued-messages", json={"text": "Earlier message"})
    _ask_tool(client, sid, question="Explain X?", response_mode="async")
    question = next(iter(app.state.user_questions.values()))
    result = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer", json={"answer": "Stiffness"}
    )
    assert result.status_code == 429, result.text
    assert "queue_capacity_exceeded" in result.text
    assert app.state.user_questions[question.id].status == "pending"
    assert len(app.state.message_intents.list_queued(sid)) == 1
    first = app.state.message_intents.list_queued(sid)[0]
    app.state.message_intents.delete_queued(sid, first.id, first.revision)
    retry = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer", json={"answer": "Stiffness"}
    )
    assert retry.status_code == 200, retry.text
    assert len(app.state.message_intents.list_queued(sid)) == 1


def test_cancelling_last_blocking_question_leaves_async_open_without_pausing(
    client: TestClient,
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(client, sid, question="Explain X?", response_mode="async")
    blocking = client.post(f"/v1/sessions/{sid}/questions", json={"prompt": "Units?"}).json()
    assert client.app.state.sessions.get(sid).status == "waiting_user"
    result = client.post(f"/v1/sessions/{sid}/questions/{blocking['id']}/cancel")
    assert result.status_code == 200, result.text
    assert client.app.state.sessions.get(sid).status == "idle"
    assert any(
        q.status == "pending" and q.response_mode == "async"
        for q in client.app.state.user_questions.values()
    )


def test_cancelled_question_moves_anchor_to_remaining_blocking_question(
    client: TestClient,
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    first = client.post(f"/v1/sessions/{sid}/questions", json={"prompt": "Units?"}).json()
    second = client.post(f"/v1/sessions/{sid}/questions", json={"prompt": "Dataset?"}).json()
    client.app.state.sessions.update(
        sid, metadata_patch={"pending_ask_user": {"surfaced": True, "question_id": second["id"]}}
    )
    response = client.post(f"/v1/sessions/{sid}/questions/{second['id']}/cancel")
    assert response.status_code == 200, response.text
    session = client.app.state.sessions.get(sid)
    assert session.status == "waiting_user"
    assert session.metadata["pending_user_question_id"] == first["id"]
    assert session.metadata["pending_ask_user"]["resolved_status"] == "cancelled"


def test_async_expiry_preserves_blocking_anchor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from clio_agent.gact import ask_user_tool

    sid = client.post("/v1/sessions", json={}).json()["id"]
    blocking = client.post(f"/v1/sessions/{sid}/questions", json={"prompt": "Units?"}).json()
    callbacks: list[Any] = []

    def timer(_delay: float, callback: Any) -> Mock:
        callbacks.append(callback)
        return Mock()

    monkeypatch.setattr(ask_user_tool.threading, "Timer", timer)
    _ask_tool(client, sid, question="Explain X?", response_mode="async", expiresInSeconds=1)
    callbacks[0]()
    session = client.app.state.sessions.get(sid)
    assert session.status == "waiting_user"
    assert session.metadata["pending_user_question_id"] == blocking["id"]
    assert [q.status for q in client.app.state.user_questions.values()] == ["pending", "expired"]
