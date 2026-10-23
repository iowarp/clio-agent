"""Each model call's streamed text is its own transcript part.

The ledger keeps one part open per ``(agent, field)`` until another part lands. Two
model calls of one agent with nothing appended between them -- a tool-free reply,
then the next loop step because a message joined the running turn -- used to stream
into ONE text part, so the UI showed "...with no error.I'll query the task tree..."
(live: ``sess_9f8733f4bdb9`` / ``msg_asst_4b3927586801``). The boundary is the model
call itself (``call_lm`` -> ``note_lm_call_start`` -> ``begin_model_call``), never the
text.

This drives the REAL ClioReAct loop inside a real gact turn (``POST /messages``) over a
scripted engine; the follow-up is a real mid-turn ``POST`` that joins the turn.

SABOTAGE: drop the ``note_lm_call_start()`` call in ``clio_react_stream_call.call_lm``
-> one text part holding both replies (this test turns red).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.app import build_app
from tests._harness import install_scripted_module
from tests._scripted_engine import Reply, scripted_lm
from tests.turn_signals import post_turn_and_wait

SCOPE = "standard"
FIRST = "The notice reports the workflow as completed with no error."
SECOND = "I'll query the task tree for the workflow and its steps."


class _HostAgent:
    """The app's host agent: unused, the turn runs the scripted ClioReAct module."""

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover - unused
        raise AssertionError("the turn runs the scripted ClioReAct module")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_HostAgent(), arc=arc)
    with TestClient(app) as test_client:
        yield test_client


def _install_loop(monkeypatch: pytest.MonkeyPatch, client: TestClient, sid: str) -> Any:
    """Run a real ClioReAct turn; a follow-up joins it while the first call is out."""

    lm, engine = scripted_lm([Reply(text=FIRST), Reply(text=SECOND)])
    original = clio_react._call_lm

    def call(model: Any, request: Any) -> Any:
        response = original(model, request)
        if len(engine.requests) == 1:
            # The person sends a follow-up while the turn is still running: it joins
            # the turn, so the loop calls the model again instead of ending.
            ack = client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "Use query_tasks."}]},
            )
            assert ack.status_code == 202, ack.text
        return response

    monkeypatch.setattr(clio_react, "_call_lm", call)

    def run(question: str, session_id: str, **_kwargs: Any) -> Any:
        tokens = [ctx.set_react_scope(SCOPE), ctx.set_react_session(session_id)]
        try:
            ctx.set_primary_agent_id(SCOPE)
            with dspy.context(lm=lm):
                return ClioReAct("question -> answer", tools=[])(question=question)
        finally:
            for token in reversed(tokens):
                ctx.reset(token)

    install_scripted_module(monkeypatch, run, commit_input=False)
    return engine


def test_two_tool_free_model_calls_stream_two_text_parts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client.app
    sid = str(client.post("/v1/sessions", json={"title": "parts"}).json()["id"])
    engine = _install_loop(monkeypatch, client, sid)
    bus = app.state.bus
    cursor = bus.latest_event_id(sid)

    status = post_turn_and_wait(
        client, sid, {"parts": [{"type": "text", "text": "Is the workflow done?"}]}
    )
    assert status == "idle", status
    assert len(engine.requests) == 2, "the joined message must drive a second model call"

    # Live: what the SSE stream carried for this turn's text parts.
    events = bus.session_events_since(sid, cursor=cursor + 1)
    added = [
        e.payload["part"]["id"]
        for e in events
        if e.type == "message.part.added" and e.payload["part"]["type"] == "text"
    ]
    completed = {
        e.payload["part_id"]: e.payload["final_text"]
        for e in events
        if e.type == "message.part.completed"
    }
    assert [completed[part_id] for part_id in added] == [FIRST, SECOND]

    # Reload: the persisted assistant message agrees with the live parts.
    app.state.messages.clear()
    rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    [assistant] = [m for m in rows if m["role"] == "assistant"]
    texts = [p for p in assistant["parts"] if p["type"] == "text"]
    assert [p["id"] for p in texts] == added
    assert [p["text"] for p in texts] == [FIRST, SECOND]
    # The tool-free final call's part is the answer; the earlier call's stays its prose.
    assert [p["metadata"]["signature_field_name"] for p in texts] == ["next_thought", "answer"]
