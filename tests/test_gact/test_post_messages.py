"""CLIO-BBBBBBBBBB9: tests for POST /v1/sessions/{sid}/messages.

Drives the app with a FakeClioAgent so no LM is needed. Covers:
  - happy path: user message stored, assistant reply returned with
    text + routing_decision parts
  - 404 for unknown session id
  - 503 when no agent is wired
  - agent exception -> assistant message carries error_info envelope
  - session status transitions running -> idle / error
  - message_count is bumped by 2 per turn (user + assistant)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class FakePrediction:
    answer: str
    selected_expert: str = ""
    routing_rationale: str = ""


class FakeClioAgent:
    """Minimal stand-in for ClioAgent. Records invocations so tests
    can assert on them without a real LM."""

    def __init__(
        self,
        answer: str = "hello from fake",
        selected_expert: str = "code_expert",
        routing_rationale: str = "matched coding keywords",
        raise_on_forward: bool = False,
    ) -> None:
        self.answer = answer
        self.selected_expert = selected_expert
        self.routing_rationale = routing_rationale
        self.raise_on_forward = raise_on_forward
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        if self.raise_on_forward:
            raise RuntimeError("simulated agent failure")
        return FakePrediction(
            answer=self.answer,
            selected_expert=self.selected_expert,
            routing_rationale=self.routing_rationale,
        )


@pytest.fixture()
def fake_agent() -> FakeClioAgent:
    return FakeClioAgent()


@pytest.fixture()
def client(tmp_path: Path, fake_agent: FakeClioAgent) -> TestClient:
    return TestClient(
        build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    )


def _create_session(client: TestClient, title: str = "t") -> str:
    return client.post("/v1/sessions", json={"title": title}).json()["id"]


def test_post_message_happy_path(
    client: TestClient, fake_agent: FakeClioAgent
) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/messages", json={"text": "refactor this function"}
    )
    assert resp.status_code == 200
    body = resp.json()

    # User message: text part echoing the prompt.
    u = body["user_message"]
    assert u["role"] == "user"
    assert u["session_id"] == sid
    assert len(u["parts"]) == 1
    assert u["parts"][0]["type"] == "text"
    assert u["parts"][0]["text"] == "refactor this function"

    # Assistant message: routing_decision first, then text answer.
    a = body["assistant_message"]
    assert a["role"] == "assistant"
    assert a["session_id"] == sid
    assert a.get("error_info") is None
    types = [p["type"] for p in a["parts"]]
    assert types == ["routing_decision", "text"]
    rd = a["parts"][0]
    assert rd["selected_agent"] == "code_expert"
    assert rd["rationale"] == "matched coding keywords"
    assert a["parts"][1]["text"] == "hello from fake"

    # Fake agent saw the call with the right session id.
    assert fake_agent.calls == [("refactor this function", sid)]


def test_post_message_bumps_message_count_by_two(client: TestClient) -> None:
    sid = _create_session(client)

    client.post(f"/v1/sessions/{sid}/messages", json={"text": "first"})
    after_one = client.get(f"/v1/sessions/{sid}").json()
    assert after_one["message_count"] == 2, (
        f"one turn should add 2 messages; got {after_one['message_count']}"
    )

    client.post(f"/v1/sessions/{sid}/messages", json={"text": "second"})
    after_two = client.get(f"/v1/sessions/{sid}").json()
    assert after_two["message_count"] == 4


def test_post_message_transitions_session_to_idle(client: TestClient) -> None:
    sid = _create_session(client)
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})
    body = client.get(f"/v1/sessions/{sid}").json()
    assert body["status"] == "idle", (
        f"session should settle back to idle after turn; got {body['status']}"
    )


def test_post_message_session_not_found_is_structured_404(
    client: TestClient,
) -> None:
    resp = client.post(
        "/v1/sessions/sess_nope/messages", json={"text": "hi"}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    inner = body["error"]
    assert isinstance(inner, dict)
    assert inner.get("message")


def test_post_message_without_agent_returns_structured_503(
    tmp_path: Path,
) -> None:
    # Build without an agent.
    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        resp = c.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})
        assert resp.status_code == 503
        body = resp.json()
        assert "error" in body
        inner = body["error"]
        assert inner.get("error") == "config_error"
        assert "ClioAgent not wired" in inner.get("message", "")


def test_post_message_agent_exception_populates_error_info(
    tmp_path: Path,
) -> None:
    failing_agent = FakeClioAgent(raise_on_forward=True)
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=failing_agent
    )
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        resp = c.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})
        # Agent failure is a 200 at the HTTP layer — we still
        # produced an assistant message — but error_info carries the
        # typed failure.
        assert resp.status_code == 200
        body = resp.json()
        a = body["assistant_message"]
        assert a.get("error_info") is not None
        err = a["error_info"]
        assert err["error"] == "agent_error"
        assert "simulated agent failure" in err["message"]

        # Session left in error state.
        sess = c.get(f"/v1/sessions/{sid}").json()
        assert sess["status"] == "error"


def test_post_message_without_routing_emits_text_only(
    tmp_path: Path,
) -> None:
    """When the agent doesn't report a selected_expert (chat /
    default path), the assistant message has just a text part — no
    routing_decision."""

    nochat_agent = FakeClioAgent(answer="chat reply", selected_expert="")
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=nochat_agent
    )
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={}).json()["id"]
        resp = c.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})
        assert resp.status_code == 200
        body = resp.json()
        types = [p["type"] for p in body["assistant_message"]["parts"]]
        assert types == ["text"], f"got parts {types}, want just [text]"
