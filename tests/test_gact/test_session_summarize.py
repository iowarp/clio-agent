from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part, Tokens


class _SummaryAgent:
    """Minimal stand-in for the chat agent.

    /summarize calls ``agent._run_chat_agent(prompt, "")`` (the same hook
    /compact uses) and, when present, wraps it in
    ``_call_with_transient_provider_retries``. We expose only
    ``_run_chat_agent`` so the handler takes the direct-call fallback.
    """

    def __init__(self, text: str = "TLDR: inspected foo.h5.\n- 3 datasets\n- 1200 rows") -> None:
        self.text = text
        self.prompts: list[str] = []

    def _run_chat_agent(self, prompt: str, session_context: str) -> str:
        self.prompts.append(prompt)
        return self.text


class _FailingAgent:
    def _run_chat_agent(self, prompt: str, session_context: str) -> str:
        raise RuntimeError("provider exploded")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """A backend with NO agent wired (for 404 / 503 / empty paths)."""
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def _client_with_agent(tmp_path: Path, agent: object) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent))


def _create_session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "summarize"}).json()["id"]


def _seed_turn(client: TestClient, sid: str) -> list[Message]:
    now = datetime.now(timezone.utc).isoformat()
    msgs = [
        Message(
            id="msg_user_1",
            session_id=sid,
            role="user",
            created_at=now,
            updated_at=now,
            parts=[Part(id="p_u1", type="text", text="Inspect dataset foo.h5")],
        ),
        Message(
            id="msg_asst_1",
            session_id=sid,
            role="assistant",
            created_at=now,
            updated_at=now,
            parts=[Part(id="p_a1", type="text", text="foo.h5: 3 datasets, 1200 rows.")],
            tokens=Tokens(output=20),
            stop_reason="end_turn",
        ),
    ]
    client.app.state.messages[sid] = list(msgs)
    client.app.state.message_store.replace_session(sid, list(msgs))
    client.app.state.sessions.update(sid, message_count=len(msgs))
    return msgs


def test_summarize_returns_200_and_summarized_true(tmp_path: Path) -> None:
    agent = _SummaryAgent()
    client = _client_with_agent(tmp_path, agent)
    sid = _create_session(client)
    _seed_turn(client, sid)

    resp = client.post(f"/v1/sessions/{sid}/summarize", json={"auto": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == sid
    assert body["summarized"] is True
    assert body["summary"] == agent.text
    assert body["event_id"]
    assert body["summary_message_id"].startswith("msg_summary_")


def test_summarize_emits_session_summarized_event(tmp_path: Path) -> None:
    client = _client_with_agent(tmp_path, _SummaryAgent())
    sid = _create_session(client)
    _seed_turn(client, sid)

    client.post(f"/v1/sessions/{sid}/summarize", json={"auto": True})

    history = client.app.state.bus._history.get(sid, [])
    summarized = [e for e in history if e.type == "session.summarized"]
    assert len(summarized) == 1
    payload = summarized[0].payload
    # The desktop reducer reads session_id off the payload, so it must be
    # present there (not just on the SSE envelope).
    assert payload["session_id"] == sid
    assert payload["summary_message_id"].startswith("msg_summary_")
    assert payload["version"] == 1


def test_summarize_is_non_destructive(tmp_path: Path) -> None:
    client = _client_with_agent(tmp_path, _SummaryAgent())
    sid = _create_session(client)
    seeded = _seed_turn(client, sid)

    client.post(f"/v1/sessions/{sid}/summarize", json={"auto": True})

    ledger = client.app.state.messages[sid]
    # Original transcript is preserved; the summary is APPENDED, not a
    # replacement (this is the key behavioural difference from /compact).
    assert len(ledger) == len(seeded) + 1
    assert [m.id for m in ledger[: len(seeded)]] == [m.id for m in seeded]
    tail = ledger[-1]
    assert tail.metadata["synthetic"] == "session_summary"
    assert tail.parts[0].text.startswith("[session summary]")


def test_summarize_with_instructions_reaches_prompt_and_event(tmp_path: Path) -> None:
    agent = _SummaryAgent()
    client = _client_with_agent(tmp_path, agent)
    sid = _create_session(client)
    _seed_turn(client, sid)

    resp = client.post(
        f"/v1/sessions/{sid}/summarize",
        json={"auto": False, "instructions": "extract action items only"},
    )

    assert resp.status_code == 200, resp.text
    assert agent.prompts, "agent was never called"
    assert "extract action items only" in agent.prompts[0]
    events = client.app.state.memory_events[sid]
    summary_events = [e for e in events if e["type"] == "session_summary"]
    assert summary_events, "no session_summary memory event recorded"
    assert summary_events[-1]["instructions"] == "extract action items only"
    assert summary_events[-1]["auto"] is False


def test_summarize_404_unknown_session(client: TestClient) -> None:
    resp = client.post("/v1/sessions/does-not-exist/summarize", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_summarize_503_when_no_agent(client: TestClient) -> None:
    sid = _create_session(client)
    _seed_turn(client, sid)

    resp = client.post(f"/v1/sessions/{sid}/summarize", json={})

    assert resp.status_code == 503
    assert resp.json()["error"]["error"] == "agent_unavailable"


def test_summarize_empty_session_returns_summarized_false(client: TestClient) -> None:
    sid = _create_session(client)

    resp = client.post(f"/v1/sessions/{sid}/summarize", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summarized"] is False
    assert "no messages" in body["reason"]


def test_capabilities_advertise_session_summary(client: TestClient) -> None:
    caps = client.get("/v1/capabilities").json()["capabilities"]
    assert caps["session_summary"] is True
