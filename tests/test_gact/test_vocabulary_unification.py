"""Regression: the retired ``turn.*`` transcript twins never reach the wire (#767 PR5).

PR5 stops the server from publishing the normalized transcript twins that
exploration confirmed had zero consumers — ``turn.text.delta`` /
``turn.trace.delta`` (streamed text/thinking), ``turn.action.added`` (tool
calls + delegation), and ``call.result.delta`` (tool results). ``message.part.*``
(plus ``message.created`` / ``message.completed``) is now the SOLE transcript
wire vocabulary.

Both scenarios below drive a full turn through the TestClient and assert none of
the retired twins appear on the session bus while the surviving ``message.part.*``
vocabulary still does. The suite-wide ``_live_equals_reload_property`` autouse
fixture in ``conftest.py`` folds that same ``message.part.*`` stream back into the
persisted assistant message for EVERY turn here, so these tests also assert the
live == reload parity still holds after the twins are gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .test_turn_transcript_equivalence import (
    _build,
    _complete_turn,
    _PlainAgent,
    _Pred,
    _ToolCallingAgent,
)

# The four normalized transcript twins retired in #767 PR5. None may ride the bus.
RETIRED_TWINS = {
    "turn.text.delta",
    "turn.trace.delta",
    "turn.action.added",
    "call.result.delta",
}


def _bus_event_types(app: Any, sid: str) -> list[str]:
    return [e.type for e in app.state.bus._history.get(sid, [])]


def test_streaming_turn_emits_message_parts_and_no_transcript_twins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A streamed provider-thinking + reasoning + answer turn: message.part.*
    only, no turn.text.delta / turn.trace.delta twins."""

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("Weighing the options...", None, "provider_thinking:anthropic")
        await emit_chunk("I should answer directly. ", None, "reasoning")
        await emit_chunk("The answer ", None, "answer")
        await emit_chunk("is 42.", None, "answer")
        return _Pred(
            answer="The answer is 42.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = _build(tmp_path, "vocab_stream", _PlainAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        _complete_turn(client, sid, "think then answer")
        types = _bus_event_types(app, sid)

        leaked = [t for t in types if t in RETIRED_TWINS]
        assert leaked == [], f"retired transcript twins leaked onto the bus: {leaked}"
        # The surviving vocabulary carried the streamed text/thinking.
        assert "message.part.added" in types
        assert "message.part.delta" in types
        assert "message.part.completed" in types

        # Reload: the persisted assistant message rebuilt from the same
        # message.part.* stream carries the answer (live == reload also enforced
        # field-for-field by the autouse conftest property).
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"][-1]
        text = "".join(p.get("text", "") for p in assistant["parts"] if p["type"] == "text")
        assert "The answer is 42." in text


def test_tool_call_turn_emits_message_parts_and_no_action_or_result_twins(
    tmp_path: Path,
) -> None:
    """A live tool-call turn: the tool_call / tool_result parts ride
    message.part.added, never turn.action.added / call.result.delta twins."""

    app = _build(tmp_path, "vocab_tool", _ToolCallingAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _complete_turn(client, sid, "call a tool please")
        types = _bus_event_types(app, sid)

        # The observer really fired (else the twin-absence assertion is vacuous).
        assert "tool.call.started" in types
        assert "tool.call.completed" in types

        leaked = [t for t in types if t in RETIRED_TWINS]
        assert leaked == [], f"retired transcript twins leaked onto the bus: {leaked}"
        # The tool_call / tool_result / answer parts still reach the client.
        assert "message.part.added" in types
        assert "message.part.completed" in types

        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"][-1]
        part_types = {p["type"] for p in assistant["parts"]}
        assert {"tool_call", "tool_result"} <= part_types
