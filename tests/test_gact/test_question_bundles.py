"""Bundles use one durable Question and one atomic answer through public controls."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.question_bundle import normalize_question_bundle
from clio_agent.gact.question_surface import question_view
from clio_agent.gact.task_declarations import build_ask_user_tool
from clio_agent.gact.task_history import TaskHistory
from tests.test_gact.task_records import active_turn_fixture

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


def post_bundle(app: Any, sid: str, **kwargs: Any) -> str:
    """Use the registered FastMCP control, not a second Question authority."""
    with active_turn_fixture(app, sid):
        receipt = asyncio.run(
            app.state.builtin_boundary.bind(build_ask_user_tool()).acall(
                question="Choose the analysis", questions=QUESTIONS, **kwargs
            )
        )
    assert receipt["accepted"] and receipt["kind"] == "Question"
    return receipt["handle"]


def test_bundle_is_one_native_interaction_and_survives_recovery(
    world: tuple[Any, TestClient, str],
) -> None:
    app, client, sid = world
    handle = post_bundle(app, sid)
    restored = TaskHistory(app.state.arc)
    restored.restore()
    task = restored.task_snapshot(handle)
    assert task is not None and task.kind == "Question" and task.raw_status == "pending"
    assert len(restored.task_handles(kind="Question")) == 1
    row = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"][0]
    assert row["source"]["protocol"] == "native"
    assert "degraded" not in row["payload"]
    assert row["payload"]["mode"] == "form"
    assert len(row["payload"]["metadata"]["question_bundle"]["questions"]) == 3
    assert [field["name"] for field in row["payload"]["fields"]] == ["view", "region", "notes"]


def test_bundle_uses_its_own_fields_when_model_supplies_single_question_defaults(
    world: tuple[Any, TestClient, str],
) -> None:
    app, _client, sid = world
    handle = post_bundle(
        app, sid, kind="choice", allowFreeform=True, options=[{"label": "Irrelevant default"}]
    )
    question = question_view(app, app.state.task_history.task_snapshot(handle))
    assert question["kind"] == "freeform" and not question["options"]
    assert not question["allow_freeform"]
    assert len(question["metadata"]["question_bundle"]["questions"]) == 3


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"answers": {"view": ANSWERS["view"]}},
        {"answers": "bad"},
        {"answers": ANSWERS | {"view": {"selected_options": ["unknown"]}}},
        {"answers": ANSWERS | {"view": {"selected_options": ["unknown"], "answer": "Detail"}}},
        {"answers": ANSWERS | {"view": {"answer": "invented"}}},
        {"answers": ANSWERS | {"region": {"answer": "  "}}},
        {"answers": ANSWERS | {"notes": {"answer": 42}}},
        {"answers": ANSWERS | {"view": {"selected_options": ["plot", "table"]}}},
        {"answers": ANSWERS | {"extra": {"answer": "injected"}}},
    ],
)
def test_partial_or_invalid_bundle_never_settles_or_queues(
    world: tuple[Any, TestClient, str],
    metadata: dict[str, Any],
) -> None:
    app, client, sid = world
    handle = post_bundle(app, sid)
    response = client.post(
        f"/v1/sessions/{sid}/questions/{handle}/answer",
        json={"answer": "bypass", "metadata": metadata},
    )
    assert response.status_code == 422, response.text
    assert app.state.task_history.task_snapshot(handle).raw_status == "pending"
    assert not [item for item in app.state.task_history.pending_mail(sid) if item.handle == handle]


def test_complete_bundle_queues_all_answers_once_and_keeps_work_running(
    world: tuple[Any, TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, sid = world
    app.state.sessions.update(sid, status="running")
    monkeypatch.setattr(app.state.conversations, "execution_gate", lambda _sid: "held_test_segment")
    handle = post_bundle(app, sid)
    route = f"/v1/sessions/{sid}/interactions/question:{handle}/respond"
    response = client.post(route, json={"action": "answer", "metadata": {"answers": ANSWERS}})
    assert response.status_code == 200, response.text
    notices = [item for item in app.state.task_history.pending_mail(sid) if item.handle == handle]
    assert len(notices) == 1 and notices[0].kind == "question_answer"
    task = app.state.task_history.task_snapshot(handle)
    reply = app.state.task_history.read_content(task.result)
    assert all(
        value in reply["answer"] for value in ["Station table", "New Zealand", "Uncertainty"]
    )
    assert reply["metadata"]["answers"] == ANSWERS
    assert app.state.sessions.get(sid).status == "running"
    assert (
        client.post(route, json={"action": "answer", "metadata": {"answers": ANSWERS}}).status_code
        == 409
    )
    assert (
        len([item for item in app.state.task_history.pending_mail(sid) if item.handle == handle])
        == 1
    )


def test_cancel_bundle_cancels_every_question_together(world: tuple[Any, TestClient, str]) -> None:
    app, client, sid = world
    handle = post_bundle(app, sid)
    response = client.post(f"/v1/sessions/{sid}/questions/{handle}/cancel")
    assert response.status_code == 200
    assert app.state.task_history.task_snapshot(handle).raw_status == "cancelled"


def test_bundle_keeps_optional_details_with_an_offered_choice(
    world: tuple[Any, TestClient, str],
) -> None:
    """Detail text supplements a valid choice even when custom answers are disabled."""
    app, client, sid = world
    handle = post_bundle(app, sid)
    answers = ANSWERS | {"view": {"selected_options": ["table"], "answer": "Keep sortable columns"}}
    response = client.post(
        f"/v1/sessions/{sid}/questions/{handle}/answer",
        json={"metadata": {"answers": answers}},
    )
    assert response.status_code == 200, response.text
    assert "Station table: Keep sortable columns" in response.json()["answer"]
    task = app.state.task_history.task_snapshot(handle)
    reply = app.state.task_history.read_content(task.result)
    assert reply["metadata"]["answers"] == answers


def test_composer_cannot_bypass_atomic_bundle_submission(
    world: tuple[Any, TestClient, str],
) -> None:
    app, client, sid = world
    handle = post_bundle(app, sid)
    response = client.post(
        f"/v1/sessions/{sid}/queued-messages",
        json={"text": "Only one answer", "metadata": {"answers_question_id": handle}},
    )
    assert response.status_code == 422, response.text
    assert app.state.task_history.task_snapshot(handle).raw_status == "pending"
    assert not [item for item in app.state.task_history.pending_mail(sid) if item.handle == handle]


def test_older_form_clients_can_submit_the_complete_flat_map(
    world: tuple[Any, TestClient, str],
) -> None:
    app, client, sid = world
    handle = post_bundle(app, sid)
    response = client.post(
        f"/v1/sessions/{sid}/questions/{handle}/answer",
        json={"metadata": {"view": "table", "region": "New Zealand", "notes": "Uncertainty"}},
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
    with pytest.raises(ValueError):
        normalize_question_bundle(questions)
