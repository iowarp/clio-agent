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
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class FakePrediction:
    answer: str
    selected_expert: str = ""
    routing_rationale: str = ""
    route_source: str = ""
    route_reason: str = ""
    error_info: dict[str, Any] | None = None


class FakeClioAgent:
    """Minimal stand-in for ClioAgent. Records invocations so tests
    can assert on them without a real LM."""

    def __init__(
        self,
        answer: str = "hello from fake",
        selected_expert: str = "code_expert",
        routing_rationale: str = "matched coding keywords",
        route_source: str = "dspy",
        route_reason: str = "planner selected code expert",
        error_info: dict[str, Any] | None = None,
        raise_on_forward: bool = False,
    ) -> None:
        self.answer = answer
        self.selected_expert = selected_expert
        self.routing_rationale = routing_rationale
        self.route_source = route_source
        self.route_reason = route_reason
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
            route_source=self.route_source,
            route_reason=self.route_reason,
            error_info=self.error_info,
        )


class SlowClioAgent(FakeClioAgent):
    def __init__(self, delay_s: float) -> None:
        super().__init__(answer="too late")
        self.delay_s = delay_s

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        time.sleep(self.delay_s)
        return FakePrediction(answer=self.answer)


@pytest.fixture()
def fake_agent() -> FakeClioAgent:
    return FakeClioAgent()


@pytest.fixture()
def client(tmp_path: Path, fake_agent: FakeClioAgent) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent))


def _create_session(client: TestClient, title: str = "t") -> str:
    return client.post("/v1/sessions", json={"title": title}).json()["id"]


def test_post_message_happy_path(client: TestClient, fake_agent: FakeClioAgent) -> None:
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
    assert rd["metadata"]["route_source"] == "dspy"
    assert rd["metadata"]["route_reason"] == "planner selected code expert"
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


def test_messages_persist_across_backend_restart(tmp_path: Path) -> None:
    from .conftest import complete_turn

    sessions_path = tmp_path / "sessions.json"
    first_agent = FakeClioAgent(answer="persisted reply")
    with TestClient(build_app(sessions_path=sessions_path, agent=first_agent)) as client:
        sid = _create_session(client, title="Persistent")
        complete_turn(client, sid, "remember this")
        before = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]

    with TestClient(build_app(sessions_path=sessions_path, agent=FakeClioAgent())) as client:
        restored_session = client.get(f"/v1/sessions/{sid}").json()
        after = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]

    assert restored_session["title"] == "Persistent"
    assert restored_session["message_count"] == 2
    assert [m["role"] for m in after] == ["assistant", "user"]
    assert after == before


def test_session_delete_removes_persisted_messages(tmp_path: Path) -> None:
    from .conftest import complete_turn

    sessions_path = tmp_path / "sessions.json"
    with TestClient(build_app(sessions_path=sessions_path, agent=FakeClioAgent())) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "delete me")
        assert client.delete(f"/v1/sessions/{sid}").status_code == 204

    with TestClient(build_app(sessions_path=sessions_path, agent=FakeClioAgent())) as client:
        assert client.get(f"/v1/sessions/{sid}").status_code == 404

    assert not (tmp_path / "messages" / f"{sid}.json").exists()


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
    resp = client.post("/v1/sessions/sess_nope/messages", json={"text": "hi"})
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
        assert inner.get("error") == "agent_not_available"
        assert inner["details"]["agent_status"] == "not_configured"
        assert "No executable CLIO agent is configured" in inner.get("message", "")
        assert c.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
        assert c.get(f"/v1/sessions/{sid}").json()["status"] == "idle"


def test_post_message_while_agent_starts_returns_structured_503(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=None)

    with TestClient(app) as c:
        app.state.want_agent = True
        app.state.agent_construction_task = SimpleNamespace(done=lambda: False)
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        resp = c.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})

        assert resp.status_code == 503
        inner = resp.json()["error"]
        assert inner["error"] == "agent_not_available"
        assert inner["details"]["agent_status"] == "starting"
        assert inner["details"]["recovery_actions"] == [
            "wait_for_agent_startup",
            "retry",
            "check_health",
        ]
        assert c.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
        assert c.get(f"/v1/sessions/{sid}").json()["status"] == "idle"


def test_post_message_after_agent_start_failure_returns_structured_503(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=None)

    with TestClient(app) as c:
        app.state.want_agent = True
        app.state.agent_construction_task = SimpleNamespace(done=lambda: True)
        app.state.agent_init_error = "RuntimeError('bad provider')"
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        resp = c.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})

        assert resp.status_code == 503
        inner = resp.json()["error"]
        assert inner["error"] == "agent_not_available"
        assert inner["details"]["agent_status"] == "failed"
        assert inner["details"]["agent_init_error"] == "RuntimeError('bad provider')"
        assert "startup failed" in inner["message"]
        assert c.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
        assert c.get(f"/v1/sessions/{sid}").json()["status"] == "idle"


def test_post_message_while_provider_configures_returns_structured_503(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=None)

    with TestClient(app) as c:
        app.state.lm_config_status = {
            "state": "configuring",
            "operation_id": "lmcfg_test",
            "provider": "lm_studio",
            "model": "qwopus3.5-9b-v3",
        }
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        resp = c.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})

        assert resp.status_code == 503
        inner = resp.json()["error"]
        assert inner["error"] == "provider_configuring"
        assert inner["details"]["operation_id"] == "lmcfg_test"
        assert inner["details"]["provider"] == "lm_studio"
        assert inner["details"]["model"] == "qwopus3.5-9b-v3"
        assert c.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
        assert c.get(f"/v1/sessions/{sid}").json()["status"] == "idle"


def test_post_message_agent_exception_populates_error_info(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    failing_agent = FakeClioAgent(raise_on_forward=True)
    app = build_app(sessions_path=tmp_path / "s.json", agent=failing_agent)
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
    app = build_app(sessions_path=tmp_path / "s.json", agent=failing_agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        assistant = complete_turn(c, sid, "hi")

    completed = [ev for ev in app.state.bus._history.get(sid, []) if ev.type == "message.completed"]
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
    app = build_app(sessions_path=tmp_path / "s.json", agent=failing_agent)
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "routing_mode": "experts"},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")

    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "agent_error"
    assert failing_agent._routing_mode_override == "auto"


def test_session_routing_override_does_not_mutate_agent_attr(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    class AttrObservingAgent(FakeClioAgent):
        def __init__(self) -> None:
            super().__init__(answer="ok")
            self._routing_mode_override = "auto"
            self.seen_overrides: list[str] = []

        def forward(self, question: str, session_id: str) -> Any:
            self.seen_overrides.append(self._routing_mode_override)
            return super().forward(question, session_id)

    agent = AttrObservingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "routing_mode": "experts"},
        ).json()["id"]
        complete_turn(c, sid, "hi")

    assert agent.seen_overrides == ["auto"]
    assert agent._routing_mode_override == "auto"


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
    assert assistant["error_info"]["details"]["recovery_actions"] == ["retry_with_auto_routing"]
    assert [part["type"] for part in assistant["parts"]] == ["routing_decision"]
    assert assistant["metadata"]["stream_source"] == "batch"
    assert assistant["metadata"]["stream_fallback"]["reason"] == "agent_not_streamable"
    assert assistant["metadata"]["stream_fallback"]["live_streaming"] is False

    completed = [ev for ev in app.state.bus._history.get(sid, []) if ev.type == "message.completed"]
    assert completed, "turn did not publish message.completed"
    payload = completed[-1].payload
    assert payload["message_id"] == assistant["id"]
    assert payload["stop_reason"] == "error"
    assert payload["error_info"]["error"] == "routing_error"
    assert payload["metadata"]["stream_source"] == "batch"
    assert payload["metadata"]["stream_fallback"]["reason"] == "agent_not_streamable"
    assert payload["metadata"]["stream_fallback"]["live_streaming"] is False


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

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> Any:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
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
                "default_provider": "openai",
                "default_model": "gpt-4.1",
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
    assert assistant["metadata"]["stream_source"] == "batch"
    assert assistant["metadata"]["stream_fallback"]["reason"] == (
        "dynamic_prompt_stream_unavailable"
    )
    assert assistant["metadata"]["agent_runtime"] == {
        "kind": "dynamic_agent",
        "agent_id": "reviewer",
        "source": "user",
        "title": "Reviewer",
        "execution_mode": "prompt_agent",
        "tools": [],
        "prompt": {
            "source": "agent_definition",
            "has_system_prompt": True,
        },
        "model": {
            "provider_id": "openai",
            "model_id": "gpt-4.1",
            "provider_source": "agent_default",
            "model_source": "agent_default",
            "fallback_to_global": False,
        },
    }
    assert sess["status"] == "idle"


def test_post_message_agent_override_executes_user_agent_for_one_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        calls.append((agent_def.id, question, session_id))
        return FakePrediction(
            answer="OVERRIDE_AGENT_OK",
            selected_expert=agent_def.id,
            routing_rationale="per-turn agent override",
        )

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> Any:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    agent = FakeClioAgent(answer="main agent should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        assert (
            c.post(
                "/v1/agents",
                json={
                    "id": "reviewer",
                    "title": "Reviewer",
                    "system_prompt": "Reply exactly OVERRIDE_AGENT_OK.",
                },
            ).status_code
            == 201
        )
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "parts": [{"type": "text", "text": "hi"}],
                "agent": {"id": "reviewer"},
            },
        )
        assert ack.status_code == 200, ack.text
        user_id = ack.json()["message_id"]

        deadline = time.monotonic() + 10
        assistant = None
        user_msg = None
        while time.monotonic() < deadline:
            msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            user_msg = next((m for m in msgs if m["id"] == user_id), None)
            for i, m in enumerate(msgs):
                if m["id"] == user_id and i > 0 and msgs[i - 1]["role"] == "assistant":
                    assistant = msgs[i - 1]
                    break
            if assistant is not None:
                break
            time.sleep(0.05)
        assert assistant is not None
        assert user_msg is not None
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert agent.calls == []
    assert calls == [("reviewer", "hi", sid)]
    assert sess["agent"]["id"] == "main"
    assert assistant["parts"][0]["selected_agent"] == "reviewer"
    assert assistant["parts"][1]["text"] == "OVERRIDE_AGENT_OK"
    assert assistant["metadata"]["agent_override"] == {
        "requested_agent_id": "reviewer",
        "session_agent_id": "main",
        "effective_agent_id": "reviewer",
        "scope": "turn",
    }
    assert user_msg["metadata"]["agent_override"] == {
        "requested_agent_id": "reviewer",
        "session_agent_id": "main",
        "scope": "turn",
    }


def test_post_message_agent_id_override_reports_structured_error_without_mutating_session(
    client: TestClient,
    fake_agent: FakeClioAgent,
) -> None:
    from .conftest import complete_turn

    sid = _create_session(client)
    assistant = complete_turn(
        client,
        sid,
        "hi",
        json_override={"agent_id": "missing_agent"},
    )
    sess = client.get(f"/v1/sessions/{sid}").json()

    assert fake_agent.calls == []
    assert sess["agent"]["id"] == "main"
    assert assistant["stop_reason"] == "error"
    assert assistant["parts"][0]["selected_agent"] == "missing_agent"
    assert assistant["error_info"]["error"] == "not_implemented"
    assert assistant["error_info"]["details"]["agent_id"] == "missing_agent"
    assert assistant["metadata"]["agent_override"] == {
        "requested_agent_id": "missing_agent",
        "session_agent_id": "main",
        "effective_agent_id": "missing_agent",
        "scope": "turn",
    }


def test_post_message_does_not_keyword_route_to_user_agent_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    def fail_prompt_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("keyword user-agent routing should be opt-in")

    monkeypatch.delenv("CLIO_ENABLE_KEYWORD_USER_AGENT_ROUTING", raising=False)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fail_prompt_agent)

    agent = FakeClioAgent(answer="MAIN_OK", selected_expert="")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        assert (
            c.post(
                "/v1/agents",
                json={
                    "id": "reviewer",
                    "title": "Reviewer",
                    "system_prompt": "Review code carefully.",
                    "keywords": ["code review", "reviewer"],
                },
            ).status_code
            == 201
        )
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        assistant = complete_turn(c, sid, "please do a code review of this patch")

    assert agent.calls == [("please do a code review of this patch", sid)]
    assert [part["type"] for part in assistant["parts"]] == ["text"]
    assert assistant["parts"][0]["text"] == "MAIN_OK"


def test_post_message_can_opt_in_to_keyword_user_agent_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        calls.append((agent_def.id, question, session_id))
        return FakePrediction(
            answer="USER_AGENT_ROUTED",
            selected_expert=agent_def.id,
            routing_rationale="matched registered user-agent keyword",
            route_source="user_agent_keyword",
        )

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> Any:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)
    monkeypatch.setenv("CLIO_ENABLE_KEYWORD_USER_AGENT_ROUTING", "1")

    agent = FakeClioAgent(answer="main should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        assert (
            c.post(
                "/v1/agents",
                json={
                    "id": "reviewer",
                    "title": "Reviewer",
                    "system_prompt": "Review code carefully.",
                    "keywords": ["code review", "reviewer"],
                },
            ).status_code
            == 201
        )
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        assistant = complete_turn(c, sid, "please do a code review of this patch")

    assert agent.calls == []
    assert calls == [("reviewer", "please do a code review of this patch", sid)]
    assert assistant["parts"][0]["selected_agent"] == "reviewer"
    assert assistant["parts"][0]["metadata"]["route_source"] == "user_agent_keyword"
    assert assistant["parts"][1]["text"] == "USER_AGENT_ROUTED"


def test_post_message_keyword_routing_chat_mode_uses_main_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    def fail_prompt_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("chat routing mode should not auto-route to user agent")

    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fail_prompt_agent)

    agent = FakeClioAgent(answer="MAIN_OK", selected_expert="")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        assert (
            c.post(
                "/v1/agents",
                json={
                    "id": "reviewer",
                    "title": "Reviewer",
                    "system_prompt": "Review code carefully.",
                    "keywords": ["code review"],
                },
            ).status_code
            == 201
        )
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "routing_mode": "chat"},
        ).json()["id"]
        assistant = complete_turn(c, sid, "please do a code review of this patch")

    assert agent.calls == [("please do a code review of this patch", sid)]
    assert [part["type"] for part in assistant["parts"]] == ["text"]
    assert assistant["parts"][0]["text"] == "MAIN_OK"


def test_post_message_prompt_user_agent_streams_live_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    def fail_prompt_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("streamed prompt user agent should not use sync runner")

    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> Any:
        del app, enriched_text, sid
        assert kwargs["agent_override"] is not None
        await emit_chunk("USER_")
        await emit_chunk("AGENT_LIVE_OK")
        return FakePrediction(
            answer="USER_AGENT_LIVE_OK",
            selected_expert="reviewer",
            routing_rationale="selected registered user agent",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fail_prompt_agent)

    agent = FakeClioAgent(answer="should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        created = c.post(
            "/v1/agents",
            json={
                "id": "reviewer",
                "title": "Reviewer",
                "system_prompt": "Reply exactly USER_AGENT_LIVE_OK.",
            },
        )
        assert created.status_code == 201
        sid = c.post(
            "/v1/sessions",
            json={"title": "x", "agent": {"id": "reviewer"}},
        ).json()["id"]
        assistant = complete_turn(c, sid, "hi")

    history = app.state.bus._history.get(sid, [])
    deltas = [ev for ev in history if ev.type == "message.part.delta"]
    completed = [ev for ev in history if ev.type == "message.completed"]

    assert agent.calls == []
    assert assistant["parts"][1]["text"] == "USER_AGENT_LIVE_OK"
    assert assistant["metadata"]["stream_source"] == "live"
    assert [d.payload["delta"]["text_append"] for d in deltas] == [
        "USER_",
        "AGENT_LIVE_OK",
    ]
    assert all(d.payload["stream_source"] == "live" for d in deltas)
    assert completed[-1].payload["metadata"]["stream_source"] == "live"


def test_post_message_tool_user_agent_executes_registered_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []
    tool_module = object()

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

    def fake_tool_module(base_agent: Any, agent_def: Any) -> object:
        assert agent_def.id == "tool_reviewer"
        return tool_module

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> Any:
        del enriched_text, emit_chunk
        assert kwargs["agent_override"] is tool_module
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_tool_stream_unavailable")
        return None

    monkeypatch.setattr("clio_agent.gact.app._build_tool_user_agent_module", fake_tool_module)
    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
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
    assert assistant["metadata"]["stream_source"] == "batch"
    assert assistant["metadata"]["stream_fallback"]["reason"] == ("dynamic_tool_stream_unavailable")
    assert assistant["metadata"]["tools_called"][0]["name"] == "fs_read_file"
    assert assistant["metadata"]["tools_called"][0]["args"] == {"path": "README.md"}
    assert assistant["metadata"]["agent_runtime"] == {
        "kind": "dynamic_agent",
        "agent_id": "tool_reviewer",
        "source": "user",
        "title": "Tool Reviewer",
        "execution_mode": "tool_agent",
        "tools": ["fs_read_file"],
        "prompt": {
            "source": "agent_definition",
            "has_system_prompt": True,
        },
        "model": {
            "provider_id": "",
            "model_id": "",
            "provider_source": "global_active",
            "model_source": "global_active",
            "fallback_to_global": True,
        },
    }
    assert sess["status"] == "idle"


def test_post_message_tool_user_agent_streams_live_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    tool_module = object()

    def fake_tool_module(base_agent: Any, agent_def: Any) -> object:
        assert agent_def.id == "tool_reviewer"
        return tool_module

    def fail_tool_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("streamed tool user agent should not use sync runner")

    def fail_prompt_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tool-declaring user agent should use the tool runner")

    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> Any:
        del app, enriched_text, sid
        assert kwargs["agent_override"] is tool_module
        await emit_chunk("TOOL_")
        await emit_chunk("USER_AGENT_LIVE_OK")
        return FakePrediction(
            answer="TOOL_USER_AGENT_LIVE_OK",
            selected_expert="tool_reviewer",
            routing_rationale="selected registered tool user agent",
        )

    monkeypatch.setattr("clio_agent.gact.app._build_tool_user_agent_module", fake_tool_module)
    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    monkeypatch.setattr("clio_agent.gact.app._run_tool_user_agent", fail_tool_agent)
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

    history = app.state.bus._history.get(sid, [])
    deltas = [ev for ev in history if ev.type == "message.part.delta"]
    completed = [ev for ev in history if ev.type == "message.completed"]

    assert agent.calls == []
    assert assistant["parts"][1]["text"] == "TOOL_USER_AGENT_LIVE_OK"
    assert assistant["metadata"]["stream_source"] == "live"
    assert [d.payload["delta"]["text_append"] for d in deltas] == [
        "TOOL_",
        "USER_AGENT_LIVE_OK",
    ]
    assert all(d.payload["stream_source"] == "live" for d in deltas)
    assert completed[-1].payload["metadata"]["stream_source"] == "live"


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


def test_post_message_clears_stale_session_model_when_global_lm_active(
    client: TestClient,
    fake_agent: FakeClioAgent,
) -> None:
    """Old TUI sessions may carry stale per-session model refs.

    CLIO runs one global LM, so once that global LM is active those
    stale refs should be healed instead of blocking the next send.
    """

    from .conftest import complete_turn

    fake_agent._provider_config = SimpleNamespace(
        provider="lm_studio",
        api_base="http://127.0.0.1:1234/v1",
        model="qwopus3.5-9b-v3",
        temperature=0.0,
        max_tokens=4096,
        context_length=32768,
        thinking_budget=0,
    )
    sid = client.post(
        "/v1/sessions",
        json={
            "title": "t",
            "model": {"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
        },
    ).json()["id"]

    assistant = complete_turn(client, sid, "hi")
    refreshed = client.get(f"/v1/sessions/{sid}").json()

    assert assistant["parts"][-1]["text"] == "hello from fake"
    assert refreshed["model"] == {"provider_id": "", "model_id": "", "variant": ""}
    assert fake_agent.calls == [("hi", sid)]


def test_post_message_turn_timeout_surfaces_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider/planner hangs must settle as visible errors, not permanent running state."""

    from .conftest import complete_turn

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "0.2")
    agent = SlowClioAgent(delay_s=0.5)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    with TestClient(app) as c:
        sid = _create_session(c)
        assistant = complete_turn(c, sid, "hi", timeout=2.0)
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "provider_timeout"
    assert assistant["error_info"]["details"]["timeout_s"] == 0.2
    assert assistant["error_info"]["details"]["executor_work_may_continue"] is True
    assert sess["status"] == "error"
    assert sess["message_count"] == 2
    assert len(agent.calls) <= 1


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
    app = build_app(sessions_path=tmp_path / "s.json", agent=nochat_agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={}).json()["id"]
        a = complete_turn(c, sid, "hi")
        types = [p["type"] for p in a["parts"]]
        assert types == ["text"], f"got parts {types}, want just [text]"
