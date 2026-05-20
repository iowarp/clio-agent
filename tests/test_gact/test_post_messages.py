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

import time
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


def test_routing_override_restored_after_agent_exception(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    failing_agent = FakeClioAgent(raise_on_forward=True)
    failing_agent._routing_mode_override = "auto"
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=failing_agent
    )
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "routing_mode": "experts"},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")

    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "agent_error"
    assert failing_agent._routing_mode_override == "auto"


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


def test_post_message_unsupported_session_agent_sets_error_turn(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    agent = FakeClioAgent(answer="should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "agent": {"id": "code_reviewer"}},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert agent.calls == []
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "not_implemented"
    assert assistant["error_info"]["details"]["agent_id"] == "code_reviewer"
    assert [part["type"] for part in assistant["parts"]] == ["routing_decision"]
    assert assistant["parts"][0]["selected_agent"] == "code_reviewer"
    assert sess["status"] == "error"


def test_post_message_prompt_user_agent_executes_registered_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        calls.append((agent_def.id, question, session_id))
        return FakePrediction(
            answer="USER_AGENT_OK",
            selected_expert=agent_def.id,
            routing_rationale="selected registered user agent",
        )

    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    agent = FakeClioAgent(answer="should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        created = c.post(
            "/v1/agents",
            json={
                "id": "reviewer",
                "title": "Reviewer",
                "system_prompt": "Reply exactly USER_AGENT_OK.",
            },
        )
        assert created.status_code == 201
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "agent": {"id": "reviewer"}},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert agent.calls == []
    assert calls == [("reviewer", "hi", sid)]
    assert assistant["stop_reason"] == "end_turn"
    assert assistant.get("error_info") is None
    assert [part["type"] for part in assistant["parts"]] == [
        "routing_decision",
        "text",
    ]
    assert assistant["parts"][0]["selected_agent"] == "reviewer"
    assert assistant["parts"][1]["text"] == "USER_AGENT_OK"
    assert sess["status"] == "idle"


def test_post_message_tool_user_agent_executes_registered_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []

    def fake_tool_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        calls.append((agent_def.id, question, session_id))
        assert _GLOBAL_TOOL_OBSERVER is not None
        _GLOBAL_TOOL_OBSERVER("fs_read_file", {"path": "README.md"}, "started", None)
        _GLOBAL_TOOL_OBSERVER("fs_read_file", {"path": "README.md"}, "completed", None)
        return FakePrediction(
            answer="TOOL_USER_AGENT_OK",
            selected_expert=agent_def.id,
            routing_rationale="selected registered tool user agent",
        )

    def fail_prompt_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tool-declaring user agent should use the tool runner")

    monkeypatch.setattr("clio_agent.gact.app._run_tool_user_agent", fake_tool_agent)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fail_prompt_agent)

    agent = FakeClioAgent(answer="should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        created = c.post(
            "/v1/agents",
            json={
                "id": "tool_reviewer",
                "title": "Tool Reviewer",
                "system_prompt": "Read files before answering.",
                "tools": ["fs_read_file"],
            },
        )
        assert created.status_code == 201
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "agent": {"id": "tool_reviewer"}},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert agent.calls == []
    assert calls == [("tool_reviewer", "hi", sid)]
    assert assistant["stop_reason"] == "end_turn"
    assert assistant.get("error_info") is None
    assert [part["type"] for part in assistant["parts"]] == [
        "routing_decision",
        "text",
    ]
    assert assistant["parts"][0]["selected_agent"] == "tool_reviewer"
    assert assistant["parts"][1]["text"] == "TOOL_USER_AGENT_OK"
    assert assistant["metadata"]["tools_called"][0]["name"] == "fs_read_file"
    assert assistant["metadata"]["tools_called"][0]["args"] == {"path": "README.md"}
    assert sess["status"] == "idle"


def test_post_message_tool_user_agent_missing_declared_tool_sets_error_turn(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    class _Tool:
        name = "hdf5_list_datasets"

    class _Executor:
        def to_dspy_tools(self) -> list[Any]:
            return [_Tool()]

    agent = FakeClioAgent(answer="should not run")
    agent.tool_executor = _Executor()  # type: ignore[attr-defined]
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        created = c.post(
            "/v1/agents",
            json={
                "id": "tool_reviewer",
                "title": "Tool Reviewer",
                "system_prompt": "Read files before answering.",
                "tools": ["fs_read_file"],
            },
        )
        assert created.status_code == 201
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "agent": {"id": "tool_reviewer"}},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert agent.calls == []
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "not_implemented"
    assert assistant["error_info"]["details"]["agent_id"] == "tool_reviewer"
    assert assistant["error_info"]["details"]["reason"] == "custom_agent_tools_unavailable"
    assert assistant["error_info"]["details"]["unsupported_tools"] == ["fs_read_file"]
    assert [part["type"] for part in assistant["parts"]] == ["routing_decision"]
    assert assistant["parts"][0]["selected_agent"] == "tool_reviewer"
    assert sess["status"] == "error"


def test_post_message_model_override_returns_structured_501(
    client: TestClient,
) -> None:
    sid = _create_session(client)

    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "model": {"provider_id": "openai", "model_id": "gpt-4o-mini"},
        },
    )

    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["error"] == "not_implemented"
    assert body["error"]["details"]["source"] == "per_message"
    assert body["error"]["details"]["model"] == {
        "provider_id": "openai",
        "model_id": "gpt-4o-mini",
        "variant": "",
    }
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert msgs == []


def test_post_message_session_model_mismatch_returns_structured_501(
    client: TestClient,
    fake_agent: FakeClioAgent,
) -> None:
    sid = client.post(
        "/v1/sessions",
        json={
            "title": "t",
            "model": {"provider_id": "openai", "model_id": "gpt-4o-mini"},
        },
    ).json()["id"]

    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hi"}]},
    )

    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["error"] == "not_implemented"
    assert body["error"]["details"]["source"] == "session"
    assert body["error"]["details"]["model"] == {
        "provider_id": "openai",
        "model_id": "gpt-4o-mini",
        "variant": "",
    }
    assert body["error"]["details"]["recovery_actions"] == [
        "put_global_lm_provider",
        "clear_session_model",
        "retry",
        "exit",
    ]
    assert fake_agent.calls == []
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert msgs == []


def test_post_message_session_model_matching_global_config_runs(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    fake_agent = FakeClioAgent(answer="model matched")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    app.state.lm_config = {
        "provider": "lm_studio",
        "model": "qwopus3.5-9b-v3",
    }
    client = TestClient(app)
    sid = client.post(
        "/v1/sessions",
        json={
            "title": "t",
            "model": {
                "provider_id": "lm_studio",
                "model_id": "qwopus3.5-9b-v3",
            },
        },
    ).json()["id"]

    assistant = complete_turn(client, sid, "hi")

    assert assistant["parts"][1]["text"] == "model matched"
    assert fake_agent.calls == [("hi", sid)]


def test_post_message_model_matching_global_config_runs(
    tmp_path: Path,
) -> None:
    fake_agent = FakeClioAgent(answer="message model matched")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    app.state.lm_config = {
        "provider": "lm_studio",
        "model": "qwopus3.5-9b-v3",
    }
    client = TestClient(app)
    sid = _create_session(client)

    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "model": {
                "provider_id": "lm_studio",
                "model_id": "qwopus3.5-9b-v3",
            },
        },
    )

    assert resp.status_code == 200
    user_id = resp.json()["message_id"]
    assistant = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        for i, msg in enumerate(messages):
            if msg.get("id") == user_id and i > 0:
                assistant = messages[i - 1]
                break
        if assistant is not None:
            break
        time.sleep(0.05)

    assert assistant is not None
    assert assistant["parts"][1]["text"] == "message model matched"
    assert fake_agent.calls == [("hi", sid)]


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
