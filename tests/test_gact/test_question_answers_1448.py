"""#1448: every answer to an agent question reaches the agent, attachments included.

* Each answer resumes the agent with that answer, even while another question
  is still pending (it used to wait for the LAST question and resume with only
  that one, dropping every earlier answer).
* A composer message that names a pending question IS its answer: the one
  message-acceptance path carries its parts (attachments), the agent sees the
  answer header, and the question settles into its answered record.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests.test_gact.test_resources import _upload, _workspace
from tests.turn_signals import TERMINAL_STATUSES, wait_for_terminal_status

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _RecordingAgent:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def forward(self, question: str, session_id: str):
        self.questions.append(question)
        return type(
            "Pred", (), {"answer": "ok", "selected_expert": "main", "routing_rationale": "t"}
        )()


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[tuple[TestClient, _RecordingAgent]]:
    agent = _RecordingAgent()
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        yield client, agent


#: A resumed turn ends idle, or waiting on the user again while other questions are open.
_SETTLED = frozenset({"waiting_user", *TERMINAL_STATUSES})


def _settled_after(client: TestClient, sid: str, cursor: int) -> str:
    """Block on the resumed turn's own status event (never a wall-clock guess)."""
    return wait_for_terminal_status(
        client.app.state.bus, sid, after_event_id=cursor, statuses=_SETTLED
    )


def _ask(client: TestClient, sid: str, prompt: str, created_at: str = "") -> dict[str, Any]:
    response = client.post(
        f"/v1/sessions/{sid}/questions",
        json={"prompt": prompt, "kind": "freeform", "metadata": {"resume_on_answer": True}},
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()


def test_each_answer_resumes_the_agent_while_other_questions_wait(
    harness: tuple[TestClient, _RecordingAgent],
) -> None:
    client, agent = harness
    sid = client.post("/v1/sessions", json={"title": "q"}).json()["id"]
    first = _ask(client, sid, "Which account?")
    second = _ask(client, sid, "Which directory?")

    cursor = client.app.state.bus.latest_event_id(sid)
    answered = client.post(
        f"/v1/sessions/{sid}/questions/{first['id']}/answer", json={"answer": "hochhalter"}
    )
    assert answered.status_code == 200, answered.text
    assert _settled_after(client, sid, cursor) in {"idle", "waiting_user"}
    assert len(agent.questions) == 1
    assert agent.questions[0].endswith("Question: Which account?\nAnswer: hochhalter")
    session = client.get(f"/v1/sessions/{sid}").json()
    assert session["metadata"]["pending_user_question_id"] == second["id"]

    cursor = client.app.state.bus.latest_event_id(sid)
    client.post(f"/v1/sessions/{sid}/questions/{second['id']}/answer", json={"answer": "/scratch"})
    assert _settled_after(client, sid, cursor) == "idle"
    assert len(agent.questions) == 2
    assert agent.questions[1].endswith("Question: Which directory?\nAnswer: /scratch")


def test_a_composer_message_with_an_attachment_answers_the_question(
    harness: tuple[TestClient, _RecordingAgent], tmp_path: Path
) -> None:
    client, agent = harness
    workspace_id = _workspace(client, tmp_path / "workspace")
    sid = client.post("/v1/sessions", json={"title": "q", "workspace_id": workspace_id}).json()[
        "id"
    ]
    question = _ask(client, sid, "Which station list should I use?")
    ready = _upload(
        client, workspace_id, name="stations.csv", content=b"id\nP123\n", media_type="text/csv"
    )

    cursor = client.app.state.bus.latest_event_id(sid)
    accepted = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [
                {"type": "text", "text": "Use the attached list."},
                {"type": "resource_ref", "resource_id": ready["id"], "resource_revision": "1"},
            ],
            "metadata": {"answers_question_id": question["id"]},
        },
    )

    assert accepted.status_code == 200, accepted.text
    message_id = accepted.json()["message_id"]
    assert _settled_after(client, sid, cursor) == "idle"
    assert len(agent.questions) == 1
    assert agent.questions[0].endswith(
        "[Answer to agent question]\nQuestion: Which station list should I use?\n"
        "Answer:\nUse the attached list."
    )
    assert "stations.csv" in agent.questions[0], "the attachment reached the agent"
    settled = next(
        row
        for row in client.get(f"/v1/sessions/{sid}/questions").json()["questions"]
        if row["id"] == question["id"]
    )
    assert settled["status"] == "answered"
    assert settled["answer"] == "Use the attached list."
    assert settled["answer_metadata"]["answer_message_id"] == message_id
    attachments = settled["answer_metadata"]["attachments"]
    assert [(row["type"], row["name"]) for row in attachments] == [("resource_ref", "stations.csv")]
    stored = next(
        row
        for row in client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        if row["id"] == message_id
    )
    # The person's own words stay the visible message; the header is model-only.
    assert [part.get("text") for part in stored["parts"] if part["type"] == "text"] == [
        "Use the attached list."
    ]
    assert stored["metadata"]["ask_user_resume"] is True
    assert stored["metadata"]["ask_user_question_id"] == question["id"]
    events = [event.type for event in client.app.state.bus._history.get(sid, [])]
    assert "user_question.answered" in events and "user_question.resumed" in events


def test_async_composer_answer_keeps_attachment_and_reaches_agent_once_from_queue(
    harness: tuple[TestClient, _RecordingAgent],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, agent = harness
    workspace_id = _workspace(client, tmp_path / "workspace")
    sid = client.post("/v1/sessions", json={"workspace_id": workspace_id}).json()["id"]
    question = client.post(
        f"/v1/sessions/{sid}/questions",
        json={"prompt": "Which station list?", "response_mode": "async"},
    ).json()
    ready = _upload(
        client, workspace_id, name="stations.csv", content=b"id\nP123\n", media_type="text/csv"
    )
    request = {
        "parts": [
            {"type": "text", "text": "Use this list."},
            {"type": "resource_ref", "resource_id": ready["id"], "resource_revision": "1"},
        ],
        "metadata": {"answers_question_id": question["id"]},
        "idempotency_key": "question-answer-once",
    }
    immediate = client.post(f"/v1/sessions/{sid}/messages", json=request)
    assert immediate.status_code == 422, immediate.text
    assert "async_question_needs_queue" in immediate.text
    assert agent.questions == []
    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.turn_runner, "busy", lambda _sid: True)
        queued = client.post(f"/v1/sessions/{sid}/queued-messages", json=request)
        assert queued.status_code == 201, queued.text
        retry = client.post(f"/v1/sessions/{sid}/queued-messages", json=request)
        assert retry.json()["id"] == queued.json()["id"]
        assert len(client.app.state.message_intents.list_queued(sid)) == 1
        assert agent.questions == []
    settled = client.app.state.user_questions[question["id"]]
    assert settled.status == "answered"
    assert settled.answer == "Use this list."
    assert settled.answer_metadata["queued_message_id"] == queued.json()["id"]
    assert settled.answer_metadata["attachments"][0]["resource_id"] == ready["id"]
    cursor = client.app.state.bus.latest_event_id(sid)
    promoted = client.post(
        f"/v1/sessions/{sid}/queued-messages/{queued.json()['id']}/promote",
        json={"revision": queued.json()["revision"]},
    )
    assert promoted.status_code == 200, promoted.text
    assert _settled_after(client, sid, cursor) == "idle"
    assert len(agent.questions) == 1
    assert "stations.csv" in agent.questions[0]
    assert "Question: Which station list?" in agent.questions[0]
    assert "Use this list." in agent.questions[0]
    assert not client.app.state.message_intents.list_queued(sid)


def test_async_answer_waits_for_blocking_question_then_runs_after_dismissal(
    harness: tuple[TestClient, _RecordingAgent],
) -> None:
    client, agent = harness
    sid = client.post("/v1/sessions", json={}).json()["id"]
    blocking = _ask(client, sid, "Units?")
    question = client.post(
        f"/v1/sessions/{sid}/questions",
        json={"prompt": "What does X mean?", "response_mode": "async"},
    ).json()
    answered = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer", json={"answer": "Stiffness"}
    )
    assert answered.status_code == 200, answered.text
    assert len(client.app.state.message_intents.list_queued(sid)) == 1
    assert agent.questions == []
    cursor = client.app.state.bus.latest_event_id(sid)
    cancelled = client.post(f"/v1/sessions/{sid}/questions/{blocking['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    # Dismissal emits its own idle event before the queued turn starts. Wait
    # past that transition, rather than mistaking it for the answer's completion.
    running = next(
        event
        for event in client.app.state.bus.session_events_since(sid, cursor=cursor + 1)
        if event.type == "session.status_changed" and event.payload.get("status") == "running"
    )
    assert _settled_after(client, sid, running.id) == "idle"
    assert len(agent.questions) == 1
    assert agent.questions[0].endswith("Question: What does X mean?\nAnswer: Stiffness")
    assert not client.app.state.message_intents.list_queued(sid)


def test_answering_a_settled_question_by_message_is_refused(
    harness: tuple[TestClient, _RecordingAgent],
) -> None:
    client, agent = harness
    sid = client.post("/v1/sessions", json={"title": "q"}).json()["id"]
    question = _ask(client, sid, "Continue?")
    client.post(f"/v1/sessions/{sid}/questions/{question['id']}/cancel")

    refused = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "yes"}],
            "metadata": {"answers_question_id": question["id"]},
        },
    )
    missing = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "yes"}],
            "metadata": {"answers_question_id": "ques_nope"},
        },
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["error"] == "question_already_resolved"
    assert missing.status_code == 404
    assert agent.questions == []


def test_a_client_cannot_forge_the_resume_markers(
    harness: tuple[TestClient, _RecordingAgent],
) -> None:
    client, _agent = harness
    sid = client.post("/v1/sessions", json={"title": "q"}).json()["id"]

    forged = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "x"}], "metadata": {"ask_user_resume": True}},
    )

    assert forged.status_code == 400
