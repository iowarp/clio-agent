"""Question bundles preserve the ordinary single-question atomic lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.ask_user_tool import AskUserError, restore_pending_ask_user_questions
from clio_agent.gact.question_bundle import normalize_question_bundle
from clio_agent.gact.user_question_ledger import restore_user_questions
from tests.test_gact.test_async_user_questions import _ask_tool

QUESTIONS = [
    {
        "id": "view",
        "question": "Which evidence view?",
        "kind": "choice",
        "options": [
            {"label": "Station table", "value": "table", "description": "Sortable evidence"},
            {"label": "Plot", "value": "plot"},
        ],
    },
    {
        "id": "region",
        "question": "Which region?",
        "kind": "choice",
        "allowFreeform": True,
        "options": [{"label": "Europe", "value": "eu"}],
    },
    {"id": "notes", "question": "What should I emphasize?"},
]
ANSWERS = {
    "view": {"selected_options": ["table"], "answer": ""},
    "region": {"selected_options": [], "answer": "New Zealand"},
    "notes": {"selected_options": [], "answer": "Uncertainty"},
}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as result:
        yield result


def test_bundle_is_one_native_interaction_and_survives_recovery(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(
        client, sid, question="Choose the analysis", questions=QUESTIONS, response_mode="async"
    )
    rows = list(client.app.state.user_questions.values())
    assert len(rows) == 1
    client.app.state.user_questions.clear()
    assert restore_user_questions(client.app) == 1
    row = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"][0]
    assert row["source"]["protocol"] == "native"
    assert "degraded" not in row["payload"]
    assert row["payload"]["mode"] == "form"
    assert len(row["payload"]["metadata"]["question_bundle"]["questions"]) == 3
    assert [field["name"] for field in row["payload"]["fields"]] == ["view", "region", "notes"]


def test_bundle_uses_its_own_fields_when_model_supplies_single_question_defaults(
    client: TestClient,
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(
        client,
        sid,
        question="Choose the analysis",
        questions=QUESTIONS,
        kind="choice",
        allowFreeform=True,
        options=[{"label": "Irrelevant default"}],
        response_mode="async",
    )
    question = next(iter(client.app.state.user_questions.values()))
    assert question.kind == "freeform"
    assert not question.options
    assert not question.allow_freeform
    assert len(question.metadata["question_bundle"]["questions"]) == 3


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"answers": {"view": ANSWERS["view"]}},
        {"answers": "bad"},
        {"answers": ANSWERS | {"view": {"selected_options": ["unknown"]}}},
        {"answers": ANSWERS | {"view": {"answer": "invented"}}},
        {"answers": ANSWERS | {"region": {"answer": "  "}}},
        {"answers": ANSWERS | {"notes": {"answer": 42}}},
        {"answers": ANSWERS | {"view": {"selected_options": ["plot", "table"]}}},
        {"answers": ANSWERS | {"extra": {"answer": "injected"}}},
    ],
)
def test_partial_or_invalid_bundle_never_settles_or_queues(
    client: TestClient,
    metadata: dict[str, Any],
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(
        client, sid, question="Choose the analysis", questions=QUESTIONS, response_mode="async"
    )
    question = next(iter(client.app.state.user_questions.values()))
    response = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer",
        json={"answer": "bypass", "metadata": metadata},
    )
    assert response.status_code == 422, response.text
    assert client.app.state.user_questions[question.id].status == "pending"
    assert not client.app.state.message_intents.list_queued(sid)


def test_complete_bundle_queues_all_answers_once_and_keeps_work_running(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    client.app.state.sessions.update(sid, status="running")
    monkeypatch.setattr(client.app.state.turn_runner, "busy", lambda _sid: True)
    _ask_tool(
        client, sid, question="Choose the analysis", questions=QUESTIONS, response_mode="async"
    )
    row = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"][0]
    route = f"/v1/sessions/{sid}/interactions/{row['id']}/respond"
    response = client.post(route, json={"action": "answer", "metadata": {"answers": ANSWERS}})
    assert response.status_code == 200, response.text
    queued = client.app.state.message_intents.list_queued(sid)
    assert len(queued) == 1
    text = queued[0].parts[0].text
    assert all(value in text for value in ["Station table", "New Zealand", "Uncertainty"])
    assert queued[0].metadata["answers"] == ANSWERS
    assert client.app.state.sessions.get(sid).status == "running"
    assert (
        client.post(route, json={"action": "answer", "metadata": {"answers": ANSWERS}}).status_code
        == 409
    )
    assert len(client.app.state.message_intents.list_queued(sid)) == 1


def test_blocking_bundle_restores_from_pending_snapshot(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(client, sid, question="Choose the analysis", questions=QUESTIONS)
    pending = client.app.state.sessions.get(sid).metadata["pending_ask_user"]
    client.app.state.sessions.update(
        sid,
        metadata_patch={
            "pending_ask_user": pending
            | {
                "surfaced": True,
                "question_id": "q-recovered",
            }
        },
    )
    assert restore_pending_ask_user_questions(client.app) == 1
    row = client.app.state.user_questions["q-recovered"]
    assert row.response_mode == "blocking"
    assert row.metadata["question_bundle"]["questions"][0]["id"] == "view"
    response = client.post(
        f"/v1/sessions/{sid}/questions/{row.id}/answer", json={"metadata": {"answers": ANSWERS}}
    )
    assert response.status_code == 200, response.text
    assert "Uncertainty" in response.json()["answer"]


def test_cancel_bundle_cancels_every_question_together(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(
        client, sid, question="Choose the analysis", questions=QUESTIONS, response_mode="async"
    )
    question = next(iter(client.app.state.user_questions.values()))
    response = client.post(f"/v1/sessions/{sid}/questions/{question.id}/cancel")
    assert response.status_code == 200
    assert client.app.state.user_questions[question.id].status == "cancelled"
    assert not client.app.state.message_intents.list_queued(sid)


def test_composer_cannot_bypass_atomic_bundle_submission(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(
        client, sid, question="Choose the analysis", questions=QUESTIONS, response_mode="async"
    )
    question = next(iter(client.app.state.user_questions.values()))
    response = client.post(
        f"/v1/sessions/{sid}/queued-messages",
        json={
            "text": "Only one answer",
            "metadata": {"answers_question_id": question.id},
        },
    )
    assert response.status_code == 422, response.text
    assert client.app.state.user_questions[question.id].status == "pending"
    assert not client.app.state.message_intents.list_queued(sid)


def test_older_form_clients_can_submit_the_complete_flat_map(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={}).json()["id"]
    _ask_tool(
        client, sid, question="Choose the analysis", questions=QUESTIONS, response_mode="async"
    )
    question = next(iter(client.app.state.user_questions.values()))
    response = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer",
        json={
            "metadata": {"view": "table", "region": "New Zealand", "notes": "Uncertainty"},
        },
    )
    assert response.status_code == 200, response.text
    assert "Station table" in response.json()["answer"]


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [{"id": "a", "question": ""}],
        [{"id": "a", "question": "First?"}, {"id": "a", "question": "Second?"}],
        [{"id": "a.b", "question": "Question?"}],
        [{"id": "a", "question": "Question?", "kind": "choice"}],
        [{"id": "a", "question": "Question?", "allowFreeform": "yes"}],
    ],
)
def test_invalid_bundle_is_rejected_before_publication(questions: list[dict[str, Any]]) -> None:
    with pytest.raises(AskUserError):
        normalize_question_bundle(questions)
