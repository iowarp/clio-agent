"""#1448: every answer to an agent question reaches the agent, attachments included.

* Each answer resumes the agent with that answer, even while another question
  is still pending (it used to wait for the LAST question and resume with only
  that one, dropping every earlier answer).
* A composer message that names a pending question IS its answer: the one
  message-acceptance path carries its parts (attachments), the agent sees the
  answer header, and the question settles into its answered record.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests.test_gact.test_resources import _upload, _workspace

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


def _wait(predicate: Any, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _idle(client: TestClient, sid: str) -> bool:
    return client.get(f"/v1/sessions/{sid}").json()["status"] in {"idle", "waiting_user"}


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

    answered = client.post(
        f"/v1/sessions/{sid}/questions/{first['id']}/answer", json={"answer": "hochhalter"}
    )
    assert answered.status_code == 200, answered.text
    _wait(lambda: len(agent.questions) == 1)
    assert agent.questions[0].endswith("Question: Which account?\nAnswer: hochhalter")
    _wait(lambda: _idle(client, sid))
    session = client.get(f"/v1/sessions/{sid}").json()
    assert session["metadata"]["pending_user_question_id"] == second["id"]

    client.post(f"/v1/sessions/{sid}/questions/{second['id']}/answer", json={"answer": "/scratch"})
    _wait(lambda: len(agent.questions) == 2)
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
    _wait(lambda: len(agent.questions) == 1)
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
