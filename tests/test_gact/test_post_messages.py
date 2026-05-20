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
    error_info: dict[str, Any] | None = None


class FakeClioAgent:
    """Minimal stand-in for ClioAgent. Records invocations so tests
    can assert on them without a real LM."""

    def __init__(
        self,
        answer: str = "hello from fake",
        selected_expert: str = "code_expert",
        routing_rationale: str = "matched coding keywords",
        error_info: dict[str, Any] | None = None,
        raise_on_forward: bool = False,
    ) -> None:
        self.answer = answer
        self.selected_expert = selected_expert
        self.routing_rationale = routing_rationale
        self.error_info = error_info
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
            error_info=self.error_info,
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
    from .conftest import complete_turn

    sid = _create_session(client)
    a = complete_turn(client, sid, "refactor this function")

    # Assistant message: routing_decision first, then text answer.
    assert a["role"] == "assistant"
    assert a["session_id"] == sid
    assert a.get("error_info") is None
    types = [p["type"] for p in a["parts"]]
    assert types == ["routing_decision", "text"]
    rd = a["parts"][0]
    assert rd["selected_agent"] == "code_expert"
    assert rd["rationale"] == "matched coding keywords"
    assert a["parts"][1]["text"] == "hello from fake"

    # User message persisted under the session.
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    user_msg = next(m for m in msgs if m["role"] == "user")
    assert user_msg["parts"][0]["text"] == "refactor this function"

    # Fake agent saw the call with the right session id.
    assert fake_agent.calls == [("refactor this function", sid)]


def test_post_message_bumps_message_count_by_two(client: TestClient) -> None:
    from .conftest import complete_turn

    sid = _create_session(client)

    complete_turn(client, sid, "first")
    after_one = client.get(f"/v1/sessions/{sid}").json()
    assert after_one["message_count"] == 2, (
        f"one turn should add 2 messages; got {after_one['message_count']}"
    )

    complete_turn(client, sid, "second")
    after_two = client.get(f"/v1/sessions/{sid}").json()
    assert after_two["message_count"] == 4


def test_post_message_transitions_session_to_idle(client: TestClient) -> None:
    from .conftest import complete_turn

    sid = _create_session(client)
    complete_turn(client, sid, "hi")
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
    from .conftest import complete_turn

    failing_agent = FakeClioAgent(raise_on_forward=True)
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=failing_agent
    )
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        a = complete_turn(c, sid, "hi")
        # Agent failure: we still produced an assistant message,
        # error_info carries the typed failure.
        assert a.get("error_info") is not None
        err = a["error_info"]
        assert err["error"] == "agent_error"
        assert "simulated agent failure" in err["message"]
        # Session left in error state.
        sess = c.get(f"/v1/sessions/{sid}").json()
        assert sess["status"] == "error"


def test_post_message_agent_exception_includes_error_info_on_completed_event(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    failing_agent = FakeClioAgent(raise_on_forward=True)
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=failing_agent
    )
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        assistant = complete_turn(c, sid, "hi")

    completed = [
        ev for ev in app.state.bus._history.get(sid, [])
        if ev.type == "message.completed"
    ]
    assert completed, "turn did not publish message.completed"
    payload = completed[-1].payload
    assert payload["message_id"] == assistant["id"]
    assert payload["stop_reason"] == "error"
    assert payload["error_info"]["error"] == "agent_error"
    assert "simulated agent failure" in payload["error_info"]["message"]


def test_post_message_prediction_error_info_sets_error_turn(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    agent = FakeClioAgent(
        answer="",
        selected_expert="chat",
        routing_rationale="planner selected a direct chat route",
        error_info={
            "error": "routing_error",
            "message": "Session routing_mode='experts' rejected chat.",
            "details": {"recovery_actions": ["retry_with_auto_routing"]},
            "recoverable": True,
        },
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "routing_mode": "experts"},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")

    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "routing_error"
    assert "rejected chat" in assistant["error_info"]["message"]
    assert assistant["error_info"]["details"]["recovery_actions"] == [
        "retry_with_auto_routing"
    ]
    assert [part["type"] for part in assistant["parts"]] == ["routing_decision"]

    completed = [
        ev for ev in app.state.bus._history.get(sid, [])
        if ev.type == "message.completed"
    ]
    assert completed, "turn did not publish message.completed"
    payload = completed[-1].payload
    assert payload["message_id"] == assistant["id"]
    assert payload["stop_reason"] == "error"
    assert payload["error_info"]["error"] == "routing_error"


def test_post_message_empty_prediction_without_error_info_sets_error_turn(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    agent = FakeClioAgent(
        answer="",
        selected_expert="chat",
        routing_rationale="planner selected chat",
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "routing_mode": "experts"},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "empty_response"
    assert "without user-visible output" in assistant["error_info"]["message"]
    assert assistant["error_info"]["details"]["routing_mode"] == "experts"
    assert [part["type"] for part in assistant["parts"]] == ["routing_decision"]
    assert sess["status"] == "error"


def test_post_message_without_routing_emits_text_only(
    tmp_path: Path,
) -> None:
    """When the agent doesn't report a selected_expert (chat /
    default path), the assistant message has just a text part — no
    routing_decision."""

    from .conftest import complete_turn

    nochat_agent = FakeClioAgent(answer="chat reply", selected_expert="")
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=nochat_agent
    )
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={}).json()["id"]
        a = complete_turn(c, sid, "hi")
        types = [p["type"] for p in a["parts"]]
        assert types == ["text"], f"got parts {types}, want just [text]"
