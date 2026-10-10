"""The response-rating API records grounded, durable feedback and fails loudly."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.response_feedback import ResponseFeedbackLedger
from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as test_client:
        yield test_client


def seed(client: TestClient) -> tuple[str, str]:
    """Seed a recorded user/assistant turn without invoking a language model."""
    sid = client.post("/v1/sessions", json={"title": "Rate response"}).json()["id"]
    now = "2026-10-10T00:00:00Z"
    prompt = Message(
        id="question",
        session_id=sid,
        role="user",
        turn_id="question",
        created_at=now,
        updated_at=now,
        parts=[Part(id="p1", type="text", text="What is 6 times 7?")],
        metadata={"effective_model": {"provider_id": "recorded", "model_id": "original"}},
    )
    reply = Message(
        id="answer",
        session_id=sid,
        role="assistant",
        turn_id="question",
        created_at=now,
        updated_at=now,
        stop_reason="end_turn",
        parts=[
            Part(id="p2", type="text", text="42."),
            Part(id="p3", type="thinking", text="private"),
        ],
    )
    client.app.state.messages[sid] = [prompt, reply]
    client.app.state.message_store.replace_session(sid, [prompt, reply])
    return sid, f"/v1/sessions/{sid}/messages/answer/feedback"


def change(rating: str | None, expected: str | None = None) -> dict[str, Any]:
    """Build one idempotent feedback change."""
    return {"feedback_id": str(uuid4()), "expected_feedback_id": expected, "rating": rating}


def test_rate_change_remove_and_evaluation_history(client: TestClient) -> None:
    sid, url = seed(client)
    assert client.get(url).json() == {"feedback": None}
    body = change("good")
    result = client.put(url, json=body)
    assert result.status_code == 200, result.text
    good = result.json()["feedback"]
    assert good["turn_id"] == "question"
    assert good["prompt_message_id"] == "question"
    assert good["prompt_text"] == "What is 6 times 7?"
    assert good["response_text"] == "42."
    assert good["response_sha256"] == hashlib.sha256(b"42.").hexdigest()
    assert good["model_ref"] == {"provider_id": "recorded", "model_id": "original"}
    assert good["model_ref_source"] == "prompt_selection"
    assert client.put(url, json=body).json()["feedback"] == good
    bad = client.put(url, json=change("bad", good["feedback_id"])).json()["feedback"]
    removed = client.put(url, json=change(None, bad["feedback_id"])).json()["feedback"]
    assert removed["rating"] is None
    assert client.get(url).json()["feedback"] == removed
    # Replacing ARC's ledger proves GET is backed by the store, not route state.
    arc = client.app.state.arc
    arc.response_feedback = ResponseFeedbackLedger(arc._store)
    assert client.get(url).json()["feedback"] == removed
    items = client.get(f"/v1/sessions/{sid}/response-feedback").json()["items"]
    assert [row["rating"] for row in items] == ["good", "bad", None]
    assert len(client.app.state.messages[sid]) == 2


def test_scope_validation_and_revision_conflicts(client: TestClient) -> None:
    sid, url = seed(client)
    assert client.put(url, json=change("great")).status_code == 422
    assert client.put(url, json={"rating": "good"}).status_code == 422
    assert client.put(url.replace("answer/", "question/"), json=change("good")).status_code == 422
    assert client.get(url.replace(sid, "missing")).status_code == 404
    assert client.get(url.replace("answer/", "missing/")).status_code == 404
    body = change("good")
    assert client.put(url, json=body).status_code == 200
    assert client.put(url, json=change("bad")).status_code == 409
    body["rating"] = "bad"
    assert client.put(url, json=body).status_code == 409
    assert client.get(url).json()["feedback"]["rating"] == "good"


def test_unfinished_response_is_not_rateable(client: TestClient) -> None:
    sid, url = seed(client)
    client.app.state.messages[sid][-1].stop_reason = ""
    assert client.put(url, json=change("good")).status_code == 409
    assert client.get(url).json() == {"feedback": None}


def test_store_failure_never_acknowledges_a_rating(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, url = seed(client)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr(client.app.state.arc._store, "put", fail)
    response = client.put(url, json=change("good"))
    assert response.status_code == 503
    assert response.json()["error"]["error"] == "feedback_unavailable"
    assert client.get(url).json() == {"feedback": None}


def test_no_arc_feedback_support_is_unavailable(client: TestClient) -> None:
    _, url = seed(client)
    client.app.state.arc.response_feedback = None
    assert client.put(url, json=change("good")).status_code == 503
