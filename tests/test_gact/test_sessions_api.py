"""CLIO-BBBBBBBBBB8: integration tests for /v1/sessions CRUD.

Uses FastAPI's TestClient against build_app() with a per-test
sessions_path so tests don't share state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.schema import Conversation as ARCConversation
from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part, Tokens


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # These are session-CRUD tests that never drive a turn (so no semantic events
    # are emitted) and one asserts the arc-absent policy (``arc_wired is False``).
    # Pin ``arc=None`` so the suite-wide default-ARC test fixture leaves it unwired.
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", arc=None))


def _seed_text_message(client: TestClient, sid: str, text: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    message = Message(
        id="msg_seed",
        session_id=sid,
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="part_seed", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )
    client.app.state.messages[sid] = [message]
    client.app.state.message_store.replace_session(sid, [message])


def _seed_text_messages(client: TestClient, sid: str, messages: list[tuple[str, str]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    seeded: list[Message] = []
    for index, (role, text) in enumerate(messages):
        seeded.append(
            Message(
                id=f"msg_seed_{index}",
                session_id=sid,
                role=role,
                created_at=now,
                updated_at=now,
                parts=[Part(id=f"part_seed_{index}", type="text", text=text)],
                tokens=Tokens(),
                stop_reason="end_turn",
            )
        )
    client.app.state.messages[sid] = seeded
    client.app.state.message_store.replace_session(sid, seeded)


def test_messages_includes_inflight_live_assistant_projection(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "live reload"}).json()["id"]
    _seed_text_message(client, sid, "start long turn")
    client.app.state.live_assistant_message_ids[sid] = "msg_live_asst"
    client.app.state.live_assistant_parts[sid] = [
        Part(id="part_live", type="text", text="live assistant evidence", agent_id="main")
    ]

    resp = client.get(f"/v1/sessions/{sid}/messages")

    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert [m["role"] for m in messages] == ["assistant", "user"]
    assert messages[0]["id"] == "msg_live_asst"
    assert messages[0]["parts"][0]["text"] == "live assistant evidence"
    assert messages[0]["metadata"] == {"live": True, "status": "running"}


def test_session_context_policy_reports_current_compartment_semantics(
    client: TestClient,
) -> None:
    sid = client.post(
        "/v1/sessions",
        json={"title": "memory scope", "mode": "plan", "routing_mode": "chat"},
    ).json()["id"]

    resp = client.get(f"/v1/sessions/{sid}/context/policy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["memory_scope"] == "session"
    assert body["writable_scope"] == "session"
    assert body["cross_session_read_available"] is True
    assert body["cross_session_read_endpoint"] == (
        f"/v1/sessions/{sid}/memory/tools/search-sessions"
    )
    assert body["requires_user_consent"] is True
    assert body["metadata"]["source"] == "clio_backend_default"
    assert body["metadata"]["session_mode"] == "plan"
    assert body["metadata"]["routing_mode"] == "chat"
    assert body["metadata"]["arc_wired"] is False
    assert body["metadata"]["cross_session_default"] == "deny_without_user_intent"
    assert any("Other-workspace memory is denied" in note for note in body["notes"])


def test_session_context_policy_unknown_session_404s(client: TestClient) -> None:
    resp = client.get("/v1/sessions/sess_missing/context/policy")

    assert resp.status_code == 404
    detail = resp.json()["error"]
    assert detail["error"] == "internal_error"
    assert detail["details"]["session_id"] == "sess_missing"


class CompactArc:
    """Minimal ARC fake for compact-memory tests."""

    def __init__(self) -> None:
        self.conversation: ARCConversation | None = None
        self.stored: list[ARCConversation] = []

    def get_conversation(self, session_id: str) -> ARCConversation | None:
        if self.conversation and self.conversation.session_id == session_id:
            return self.conversation
        return None

    def store_conversation(self, conversation: ARCConversation) -> None:
        self.conversation = conversation
        self.stored.append(conversation)


class RetryCompactAgent:
    """Fake compact agent that succeeds only when the endpoint uses retry wrapping."""

    def __init__(self) -> None:
        self.chat_calls = 0
        self.retry_labels: list[str] = []
        self.arc = CompactArc()

    def _run_chat_agent(self, question: str, session_id: str) -> str:
        self.chat_calls += 1
        if self.chat_calls == 1:
            raise RuntimeError("Tokens/minute limit exceeded")
        assert "important experiment details" in question
        assert "evidence-preserving compact memory" in question
        assert "Do not invent dataset names" in question
        assert session_id == ""
        return "Recovered compact summary."

    def _call_with_transient_provider_retries(
        self,
        label: str,
        call: Callable[[], Any],
    ) -> Any:
        self.retry_labels.append(label)
        try:
            return call()
        except RuntimeError as exc:
            if "tokens/minute" not in str(exc).lower():
                raise
            return call()

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover
        raise AssertionError("compact tests should not call forward()")


class ExhaustedCompactAgent(RetryCompactAgent):
    """Fake compact agent whose retry wrapper exhausts and re-raises."""

    def _run_chat_agent(self, question: str, session_id: str) -> str:
        self.chat_calls += 1
        raise RuntimeError("Tokens/minute limit exceeded")


class CapturingCompactAgent(RetryCompactAgent):
    """Fake compact agent that records the prompt sent to the summarizer."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def _run_chat_agent(self, question: str, session_id: str) -> str:
        self.chat_calls += 1
        self.prompts.append(question)
        assert session_id == ""
        return "captured exact identifiers"

    def _call_with_transient_provider_retries(
        self,
        label: str,
        call: Callable[[], Any],
    ) -> Any:
        self.retry_labels.append(label)
        return call()


def test_compact_retries_transient_provider_errors(tmp_path: Path) -> None:
    agent = RetryCompactAgent()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as c:
        sid = c.post("/v1/sessions", json={"title": "compact me"}).json()["id"]
        _seed_text_message(c, sid, "important experiment details and next steps")

        resp = c.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["compacted"] is True
        assert body["summary"] == "Recovered compact summary."
        assert body["event_id"].startswith("mem_")
        assert agent.retry_labels == ["compact_summary"]
        assert agent.chat_calls == 2
        assert agent.arc.conversation is not None
        arc_messages = agent.arc.conversation.messages
        assert len(arc_messages) == 1
        assert arc_messages[0].metadata["source"] == "gact_compact"
        assert arc_messages[0].metadata["memory_event_id"] == body["event_id"]
        assert "Recovered compact summary." in arc_messages[0].content
        messages = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert len(messages) == 1
        assert messages[0]["parts"][0]["metadata"]["synthetic"] == "compact_summary"
        assert messages[0]["metadata"]["memory_event_id"] == body["event_id"]
        assert "Recovered compact summary." in messages[0]["parts"][0]["text"]
        events = c.get(f"/v1/sessions/{sid}/memory/events").json()["events"]
        assert len(events) == 1
        event = events[0]
        assert event["id"] == body["event_id"]
        assert event["version"] == 1
        assert event["type"] == "compact_summary"
        assert event["summary_message_id"] == messages[0]["id"]
        assert event["archived_count"] == 1
        assert event["arc_status"] == "stored"
        assert event["metadata"]["source"] == "gact_compact"
        detail = c.get(f"/v1/sessions/{sid}/memory/events/{body['event_id']}").json()
        assert detail["event"]["id"] == body["event_id"]
        compact_events = [
            e for e in c.app.state.bus._history.get(sid, []) if e.type == "session.compacted"
        ]
        assert compact_events[-1].payload["event_id"] == body["event_id"]


def test_compact_surfaces_exhausted_transient_provider_errors(tmp_path: Path) -> None:
    agent = ExhaustedCompactAgent()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as c:
        sid = c.post("/v1/sessions", json={"title": "compact me"}).json()["id"]
        _seed_text_message(c, sid, "important experiment details and next steps")

        resp = c.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["error"] == "upstream_error"
        assert body["error"]["recoverable"] is True
        assert "compact summarisation failed" in body["error"]["message"]
        assert "Tokens/minute limit exceeded" in body["error"]["message"]
        assert agent.retry_labels == ["compact_summary"]


def test_memory_events_unknown_session_and_event_404(tmp_path: Path) -> None:
    agent = RetryCompactAgent()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as c:
        sid = c.post("/v1/sessions", json={"title": "compact me"}).json()["id"]
        _seed_text_message(c, sid, "important experiment details and next steps")
        resp = c.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text

        missing_session = c.get("/v1/sessions/sess_missing/memory/events")
        missing_event = c.get(f"/v1/sessions/{sid}/memory/events/mem_missing")

        assert missing_session.status_code == 404
        assert missing_session.json()["error"]["error"] == "not_found"
        assert missing_event.status_code == 404
        assert missing_event.json()["error"]["details"]["event_id"] == "mem_missing"


def test_compact_prompt_preserves_late_scientific_identifiers(tmp_path: Path) -> None:
    agent = CapturingCompactAgent()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as c:
        sid = c.post("/v1/sessions", json={"title": "compact identifiers"}).json()["id"]
        long_prefix = "background filler " * 180
        _seed_text_messages(
            c,
            sid,
            [
                (
                    "assistant",
                    long_prefix
                    + "HDF5 dataset /plasma/electron_temperature shape=(128, 64) units=keV.",
                ),
                (
                    "assistant",
                    long_prefix
                    + "Parquet column anomaly_score mean=0.021 max=0.93 pressure_pa min=101325.",
                ),
                (
                    "assistant",
                    long_prefix
                    + "CSV columns event_id,status,operator_note,timestamp_utc still need semantics.",
                ),
            ],
        )

        resp = c.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "[exact retained evidence index]" in body["summary"]
        assert "/plasma/electron_temperature" in body["summary"]
        assert "anomaly_score" in body["summary"]
        assert "operator_note" in body["summary"]
        assert agent.retry_labels == ["compact_summary"]
        assert len(agent.prompts) == 1
        prompt = agent.prompts[0]
        assert "evidence-preserving compact memory" in prompt
        assert "Do not invent dataset names" in prompt
        assert "/plasma/electron_temperature" in prompt
        assert "anomaly_score" in prompt
        assert "operator_note" in prompt


def test_post_v1_sessions_returns_created_session(client: TestClient) -> None:
    resp = client.post(
        "/v1/sessions",
        json={
            "workspace_id": "ws_default",
            "title": "my session",
            "agent": {"id": "code_reviewer", "mode": "review"},
            "model": {
                "provider_id": "lm_studio",
                "model_id": "qwopus3.5-9b-v3",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"].startswith("sess_")
    assert body["workspace_id"] == "ws_default"
    assert body["title"] == "my session"
    assert body["status"] == "idle"
    assert body["message_count"] == 0
    assert body["agent"] == {"id": "code_reviewer", "mode": "review"}
    assert body["model"] == {
        "provider_id": "lm_studio",
        "model_id": "qwopus3.5-9b-v3",
        "variant": "",
    }


def test_post_v1_sessions_defaults_workspace_and_title(
    client: TestClient,
) -> None:
    """Empty body is allowed — CLIO has an implicit default workspace
    and we synthesise a title from the session id."""

    resp = client.post("/v1/sessions", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == "ws_default"
    # Synthesised titles include the trailing id chars.
    assert body["id"][-6:] in body["title"]


def test_get_v1_sessions_lists_newest_first(client: TestClient) -> None:
    first = client.post("/v1/sessions", json={"title": "first"}).json()
    second = client.post("/v1/sessions", json={"title": "second"}).json()

    resp = client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    ids = [s["id"] for s in body["sessions"]]
    assert ids == [second["id"], first["id"]], f"expected newest-first; got {ids}"


def test_get_v1_sessions_filter_by_workspace(client: TestClient) -> None:
    # Create two ad-hoc workspaces (ws_default already exists so
    # POSTing to it works without a roundtrip).
    ws_a = client.post("/v1/workspaces", json={"name": "alpha"}).json()["id"]
    ws_b = client.post("/v1/workspaces", json={"name": "beta"}).json()["id"]

    client.post("/v1/sessions", json={"workspace_id": ws_a, "title": "a1"})
    client.post("/v1/sessions", json={"workspace_id": ws_b, "title": "b1"})
    client.post("/v1/sessions", json={"workspace_id": ws_a, "title": "a2"})

    resp = client.get(f"/v1/sessions?workspace_id={ws_a}")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["sessions"]]
    assert len(ids) == 2
    assert all(s["workspace_id"] == ws_a for s in resp.json()["sessions"])


def test_get_v1_sessions_defaults_to_default_workspace_scope(client: TestClient) -> None:
    ws_a = client.post("/v1/workspaces", json={"name": "alpha"}).json()["id"]
    default = client.post("/v1/sessions", json={"title": "default"}).json()
    client.post("/v1/sessions", json={"workspace_id": ws_a, "title": "a1"})

    scoped = client.get("/v1/sessions")
    assert scoped.status_code == 200
    assert [s["id"] for s in scoped.json()["sessions"]] == [default["id"]]

    all_rows = client.get("/v1/sessions", params={"include_all_workspaces": "true"})
    assert all_rows.status_code == 200
    assert {s["title"] for s in all_rows.json()["sessions"]} >= {"default", "a1"}


def test_get_v1_sessions_sid_returns_single(client: TestClient) -> None:
    created = client.post("/v1/sessions", json={"title": "x"}).json()
    resp = client.get(f"/v1/sessions/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert resp.json()["title"] == "x"


def test_get_v1_sessions_sid_denies_mismatched_workspace_scope(
    client: TestClient,
) -> None:
    ws_a = client.post("/v1/workspaces", json={"name": "alpha"}).json()["id"]
    ws_b = client.post("/v1/workspaces", json={"name": "beta"}).json()["id"]
    created = client.post(
        "/v1/sessions",
        json={"workspace_id": ws_a, "title": "alpha session"},
    ).json()

    resp = client.get(f"/v1/sessions/{created['id']}", params={"workspace_id": ws_b})

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["error"] == "permission_error"
    assert body["error"]["details"]["scope"] == "other_workspace"


def test_patch_v1_sessions_preserves_agent_and_model_refs(
    client: TestClient,
) -> None:
    created = client.post("/v1/sessions", json={"title": "x"}).json()

    resp = client.patch(
        f"/v1/sessions/{created['id']}",
        json={
            "agent": {"id": "data"},
            "model": {"provider_id": "openai", "model_id": "gpt-4o-mini"},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == {"id": "data", "mode": ""}
    assert body["model"] == {
        "provider_id": "openai",
        "model_id": "gpt-4o-mini",
        "variant": "",
    }

    fetched = client.get(f"/v1/sessions/{created['id']}").json()
    assert fetched["agent"] == body["agent"]
    assert fetched["model"] == body["model"]


def test_get_v1_sessions_sid_not_found_returns_structured_404(
    client: TestClient,
) -> None:
    resp = client.get("/v1/sessions/sess_does_not_exist")
    assert resp.status_code == 404
    body = resp.json()
    # v0.2 envelope shape — the typed taxonomy (§14).
    assert "error" in body
    inner = body["error"]
    assert isinstance(inner, dict)
    assert "message" in inner
    # Machine-readable discriminator present (either v0.1 `code` or
    # v0.2 `error` — our impl uses the latter).
    assert "error" in inner or "code" in inner


def test_delete_v1_sessions_removes_row(client: TestClient) -> None:
    created = client.post("/v1/sessions", json={"title": "gone"}).json()
    resp = client.delete(f"/v1/sessions/{created['id']}")
    assert resp.status_code == 204

    # Gone from the list.
    resp2 = client.get(f"/v1/sessions/{created['id']}")
    assert resp2.status_code == 404


def test_delete_v1_sessions_missing_is_404(client: TestClient) -> None:
    resp = client.delete("/v1/sessions/sess_does_not_exist")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body


def test_sessions_persisted_across_app_instances(tmp_path: Path) -> None:
    """Two TestClients pointing at the same sessions.json see the
    same rows — which is how the store survives
    ``clio-agent-gact`` restarts."""

    path = tmp_path / "sessions.json"

    with TestClient(build_app(sessions_path=path)) as a:
        created = a.post("/v1/sessions", json={"title": "keep"}).json()

    with TestClient(build_app(sessions_path=path)) as b:
        resp = b.get(f"/v1/sessions/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "keep"
