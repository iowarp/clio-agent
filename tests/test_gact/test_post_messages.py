"""tests for POST /v1/sessions/{sid}/messages.

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

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake (agent=None ingress paths return their
# structured 503 before any module is built, so they are unaffected).
pytestmark = pytest.mark.usefixtures("host_agent_executor")


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


class ImageAwareFakeClioAgent(FakeClioAgent):
    def __init__(self) -> None:
        super().__init__(answer="image seen")
        self.image_calls: list[list[Any]] = []

    def forward(
        self,
        question: str,
        session_id: str,
        *,
        images: list[Any] | None = None,
        **_: Any,
    ) -> Any:
        self.calls.append((question, session_id))
        self.image_calls.append(list(images or []))
        return FakePrediction(answer=self.answer)


class SlowClioAgent(FakeClioAgent):
    def __init__(self, delay_s: float) -> None:
        super().__init__(answer="too late")
        self.delay_s = delay_s

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        time.sleep(self.delay_s)
        return FakePrediction(answer=self.answer)


class ProgressingSlowClioAgent(FakeClioAgent):
    """Runs longer than the no-progress window but keeps publishing progress.

    Mirrors a real multi-phase turn (filter -> stage -> profile -> plot): each
    phase emits a bus event, so the gap between events stays under the window
    even though the total turn duration exceeds it. The no-progress watchdog
    must let this run to completion.
    """

    def __init__(self, *, steps: int, step_s: float) -> None:
        super().__init__(answer="progressing done")
        self.steps = steps
        self.step_s = step_s
        self.bus: Any | None = None

    def forward(self, question: str, session_id: str) -> Any:
        from clio_agent.gact.events import Event

        self.calls.append((question, session_id))
        for i in range(self.steps):
            time.sleep(self.step_s)
            if self.bus is not None:
                self.bus.publish(
                    Event(
                        type="semantic.event",
                        session_id=session_id,
                        payload={"summary": f"phase {i}"},
                    )
                )
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
    # routing_decision is attributed to the orchestrator (the decider), while the
    # answer text part carries the responding expert's agent_id so a client can
    # attribute every part to its source without inference.
    assert rd["agent_id"] == "main"
    assert a["parts"][1]["text"] == "hello from fake"
    assert a["parts"][1]["agent_id"] == "code_expert"

    # User message persisted under the session.
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    user_msg = next(m for m in msgs if m["role"] == "user")
    assert user_msg["parts"][0]["text"] == "refactor this function"

    # Fake agent saw the call with the right session id.
    assert fake_agent.calls == [("refactor this function", sid)]


def test_turn_id_correlates_user_and_assistant_durably(client: TestClient) -> None:
    """#711: turn_id is a durable join key in the ledger.

    The user message's turn_id equals its own id; the assistant reply's turn_id equals
    that user message id. Persisted (GET /messages) so consumers join the whole turn —
    prose + trajectory — without the message_id -> preceding-user-message heuristic.
    """

    from .conftest import complete_turn

    sid = _create_session(client)
    assistant = complete_turn(client, sid, "correlate this turn")

    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    user_msg = next(m for m in msgs if m["role"] == "user")
    asst_msg = next(m for m in msgs if m["id"] == assistant["id"])

    assert user_msg["turn_id"] == user_msg["id"]
    assert asst_msg["turn_id"] == user_msg["id"]
    assert assistant["turn_id"] == user_msg["id"]
    # Matches the semantic-event turn_id (== the user message id) so message.* and
    # semantic.event streams join on one key.
    completed = [ev for ev in app_history(client, sid) if ev.type == "message.completed"]
    assert completed and completed[-1].payload["turn_id"] == user_msg["id"]


def app_history(client: TestClient, sid: str) -> list[Any]:
    return client.app.state.bus._history.get(sid, [])  # type: ignore[attr-defined]


def test_capabilities_and_provider_catalog_report_image_part_support(client: TestClient) -> None:
    caps = client.get("/v1/capabilities").json()["capabilities"]
    assert caps["multimodal_image_parts"] is True

    providers = client.get("/v1/providers").json()["providers"]
    by_id = {row["id"]: row for row in providers}
    assert by_id["openai"]["metadata"]["supports_vision"] is True
    assert by_id["anthropic"]["metadata"]["supports_vision"] is True
    assert by_id["codex"]["metadata"]["supports_vision"] is False
    assert by_id["claude_code"]["metadata"]["supports_vision"] is False


def test_post_message_rejects_image_parts_for_text_only_provider(
    tmp_path: Path,
    fake_agent: FakeClioAgent,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    app.state.lm_config = {
        "provider": "codex",
        "model": "gpt-5.5",
        "supports_vision": False,
    }
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "vision"}).json()["id"]
        resp = c.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "parts": [
                    {"type": "text", "text": "describe this image"},
                    {
                        "type": "image",
                        "data": "iVBORw0KGgo=",
                        "media_type": "image/png",
                    },
                ]
            },
        )

        assert resp.status_code == 501, resp.text
        body = resp.json()["error"]
        assert body["error"] == "unsupported_multimodal_image"
        assert body["details"]["provider"] == "codex"
        assert body["details"]["image_part_count"] == 1
        assert c.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []

    assert fake_agent.calls == []


def test_post_message_preserves_image_parts_for_vision_capable_provider(
    tmp_path: Path,
    fake_agent: FakeClioAgent,
) -> None:
    from .conftest import complete_turn

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    app.state.lm_config = {
        "provider": "openai",
        "model": "gpt-4o",
        "supports_vision": True,
    }
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "vision"}).json()["id"]
        assistant = complete_turn(
            c,
            sid,
            "",
            json_override={
                "parts": [
                    {"type": "text", "text": "describe this image"},
                    {
                        "type": "image",
                        "data": "iVBORw0KGgo=",
                        "media_type": "image/png",
                        "metadata": {"filename": "cell.png"},
                    },
                ]
            },
        )
        messages = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]

    user_msg = next(m for m in messages if m["role"] == "user")
    assert [part["type"] for part in user_msg["parts"]] == ["text", "image"]
    image = user_msg["parts"][1]
    assert image["media_type"] == "image/png"
    assert image["data"] == "iVBORw0KGgo="
    assert image["metadata"]["filename"] == "cell.png"
    assert image["metadata"]["clio_multimodal"] == "preserved"
    assert user_msg["metadata"]["multimodal"] == {
        "image_part_count": 1,
        "transcript_preserved": True,
        "native_model_dispatch": False,
    }
    assert assistant["parts"][-1]["text"] == "hello from fake"
    assert fake_agent.calls == [("describe this image", sid)]


def test_post_message_dispatches_image_parts_to_image_aware_agent(tmp_path: Path) -> None:
    from .conftest import complete_turn

    fake_agent = ImageAwareFakeClioAgent()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    app.state.lm_config = {
        "provider": "openai",
        "model": "gpt-4o",
        "supports_vision": True,
    }
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "vision"}).json()["id"]
        assistant = complete_turn(
            c,
            sid,
            "",
            json_override={
                "parts": [
                    {"type": "text", "text": "describe this image"},
                    {
                        "type": "image",
                        "data": "iVBORw0KGgo=",
                        "media_type": "image/png",
                    },
                ]
            },
        )
        messages = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]

    user_msg = next(m for m in messages if m["role"] == "user")
    assert user_msg["metadata"]["multimodal"] == {
        "image_part_count": 1,
        "transcript_preserved": True,
        "native_model_dispatch": True,
    }
    assert assistant["parts"][-1]["text"] == "image seen"
    assert fake_agent.calls == [("describe this image", sid)]
    # #948 S4b: ``native_model_dispatch`` still reflects the HOST agent's declared
    # vision capability (``_agent_accepts_images(app.state.agent)``), but the
    # blueprint runtime executes a compiled DSPy module whose ``forward`` takes no
    # ``images`` kwarg (``BlueprintExpertModule.forward``); native images are
    # threaded to the model through the streaming/adapter layer, not handed to a
    # host-agent ``forward(images=)``. That host-forward dispatch was a
    # legacy-planner mechanism, so the host fake no longer observes the images.
    assert fake_agent.image_calls == [[]]


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


def test_post_message_disabled_blueprint_root_fails_typed(
    tmp_path: Path,
) -> None:
    """#948 S4: an active blueprint whose declared root is DISABLED by validation
    (e.g. a pre-migration pack: chain_of_thought main with children) fails the
    turn typed — never a silently substituted root, never the legacy planner."""

    from .conftest import complete_turn

    source = tmp_path / "stale-pack"
    (source / "experts").mkdir(parents=True)
    source.joinpath("AGENT.md").write_text(
        """---
id: stale-pack
version: 0.1.0
title: Stale Pack
root_expert: root
---
Pre-migration pack.
""",
        encoding="utf-8",
    )
    # Activation-time validation rejects a broken pack outright, so reproduce the
    # STALE-INSTALL shape observed live: the pack is VALID when activated, then
    # the on-disk root loses its react declaration (an old install re-validated
    # under the new S4 hierarchy rules) — runtime resolution re-validates rows
    # each turn and disables the root.
    source.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Valid Root
tier: 1
module:
  kind: react
---
Coordinate work.
""",
        encoding="utf-8",
    )
    source.joinpath("experts", "leaf.md").write_text(
        """---
id: leaf
title: Enabled Leaf
parent_id: root
tier: 2
module:
  kind: react
---
Do leaf work.
""",
        encoding="utf-8",
    )

    agent = FakeClioAgent(answer="legacy planner must not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        assert (
            c.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(source)},
            ).status_code
            == 200
        )
        # The stale-install mutation: the declared root is no longer react while
        # it still has a declared child — disabled at the next runtime resolve.
        source.joinpath("experts", "root.md").write_text(
            """---
id: root
title: Stale Root
tier: 1
---
Coordinate work.
""",
            encoding="utf-8",
        )
        assistant = complete_turn(c, sid, "hello")
        sess = c.get(f"/v1/sessions/{sid}").json()

    # The legacy ClioAgent planner NEVER ran (the observed live fall-through).
    assert agent.calls == []
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "blueprint_root_disabled"
    details = assistant["error_info"]["details"]
    assert details["root_id"] == "root"
    assert any("module.kind: react" in err for err in details["validation_errors"])
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
        "module": {},
        "tools": [],
        "structured_outputs": {},
        "fanout": {},
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


def test_post_message_main_path_answers_without_keyword_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    def fail_prompt_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no keyword auto-routing: the main path must answer")

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
    # #767 PR3 (#731): parts persist in ARRIVAL order — the streamed answer part
    # landed live first; the routing banner is appended at finalize, after it.
    assert [part["type"] for part in assistant["parts"]] == ["text", "routing_decision"]
    assert assistant["parts"][0]["text"] == "USER_AGENT_LIVE_OK"
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
        from clio_agent.tools.execution import current_tool_runtime

        calls.append((agent_def.id, question, session_id))
        observer = current_tool_runtime().tool_observer
        assert observer is not None
        observer("fs_read_file", {"path": "README.md"}, "started", None)
        observer("fs_read_file", {"path": "README.md"}, "completed", None)
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
    # #731 / #767 PR3: the persisted message IS the ledger in ARRIVAL ORDER —
    # the observer's tool_call / tool_result parts landed live during the turn;
    # the routing banner and the batch answer are appended at finalize, after
    # them. (Before PR3 finalize hoisted its routing part above the live spine,
    # so reload order differed from stream order.)
    assert [part["type"] for part in assistant["parts"]] == [
        "tool_call",
        "tool_result",
        "routing_decision",
        "text",
    ]
    # #731: every persisted part carries a monotonic 1-based arrival-order key.
    assert [part["sequence"] for part in assistant["parts"]] == [1, 2, 3, 4]
    assert assistant["parts"][2]["selected_agent"] == "tool_reviewer"
    assert assistant["parts"][0]["tool_name"] == "fs_read_file"
    assert assistant["parts"][-1]["text"] == "TOOL_USER_AGENT_OK"
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
        "module": {},
        "tools": ["fs_read_file"],
        "structured_outputs": {},
        "fanout": {},
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
    # #767 PR3 (#731): arrival order — the streamed answer part precedes the
    # finalize-appended routing banner.
    assert [part["type"] for part in assistant["parts"]] == ["text", "routing_decision"]
    assert assistant["parts"][0]["text"] == "TOOL_USER_AGENT_LIVE_OK"
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


def test_post_message_progressing_turn_outlives_no_progress_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-but-progressing turn must NOT be aborted by the watchdog.

    Total duration (5 x 0.1s = ~0.5s) exceeds the 0.2s window, but the gap
    between published progress events (~0.1s) stays under it, so the turn is
    progressing and must complete successfully.
    """

    from .conftest import complete_turn

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "0.2")
    agent = ProgressingSlowClioAgent(steps=5, step_s=0.1)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    agent.bus = app.state.bus
    with TestClient(app) as c:
        sid = _create_session(c)
        assistant = complete_turn(c, sid, "hi", timeout=5.0)
        sess = c.get(f"/v1/sessions/{sid}").json()

    assert assistant.get("error_info") is None
    assert assistant["stop_reason"] != "error"
    assert assistant["parts"][-1]["text"] == "progressing done"
    assert sess["status"] != "error"
    assert len(agent.calls) == 1


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


def test_tool_call_part_carries_thought_and_invoking_expert(tmp_path: Path) -> None:
    """#732: the live tool_call part is one ordered event = the model's reasoning
    (``thought``) + the action, authored by the INVOKING expert (the active ReAct
    scope, e.g. ``geospatial``) rather than the tool's owning server (``geo``).
    The tool RESPONSE is a separate ``tool_result`` event, same ``call_id``,
    likewise authored by the invoking expert."""

    from clio_agent.gact import context as ctx
    from clio_agent.gact.app import _make_tool_observer

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="x"))
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={}).json()["id"]
        observe = _make_tool_observer(app)
        sid_tok = ctx.set_tool_session_id(sid)
        scope_tok = ctx.set_react_scope("geospatial")
        thought_tok = ctx.set_step_thought("Resolve Los Angeles to coordinates.", "raw cot")
        try:
            observe("geo_geocode", {"query": "Los Angeles"}, "started", None)
            observe("geo_geocode", {"query": "Los Angeles"}, "completed", None, {"lat": 34.0})
        finally:
            ctx.reset(thought_tok)
            ctx.reset(scope_tok)
            ctx.reset(sid_tok)

        parts = app.state.live_assistant_parts[sid]
        call = next(p for p in parts if p.type == "tool_call")
        result = next(p for p in parts if p.type == "tool_result")
        # authored by the invoking expert, NOT the tool owner
        assert call.agent_id == "geospatial"
        assert result.agent_id == "geospatial"
        assert result.call_id == call.call_id
        # the step thought rides the tool_call part (one ordered event)...
        assert call.thought == "Resolve Los Angeles to coordinates."
        # ...and survives the slim on-the-wire projection
        wire = call.to_wire()
        assert wire["thought"] == "Resolve Los Angeles to coordinates."
        assert wire["agent_id"] == "geospatial"


# NOTE (#767 PR3): the ``_dedup_cross_agent_text`` finalize scrub (mechanism 6's
# persist-time half) was deleted — finalize persists the ledger VERBATIM, so a
# post-hoc text-matching drop pass can no longer exist. The #736 symptom is now
# covered by exactly-once producer assertions in ``test_turn_transcript_pr3.py``
# (the canonical answer channel never re-emits an already-landed answer, by op
# identity) and by the suite-wide live==reload fold property in ``conftest.py``.
# The restates_part_id echo TAG (mechanism 6's replacement labeling) ships in PR4.
