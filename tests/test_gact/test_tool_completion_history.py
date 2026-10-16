"""Missing terminal records are uncertainty, never an indefinitely running command."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol.v3.message import transcript_entities
from clio_agent.gact.types import Message


@pytest.mark.parametrize("stop_reason", ["error", "cancelled", "interrupted", "failed", "end_turn"])
def test_ended_turn_without_result_has_unknown_execution_outcome(stop_reason: str) -> None:
    wire: dict[str, Any] = {
        "id": "message_1",
        "session_id": "session_1",
        "role": "assistant",
        "created_at": "2026-10-15T10:00:00Z",
        "updated_at": "2026-10-15T10:00:05Z",
        "stop_reason": stop_reason,
        "parts": [
            {
                "id": "part_call",
                "type": "tool_call",
                "call_id": "call_1",
                "tool_name": "shell_run",
                "input": {"command": "build report"},
                "metadata": {"started_at": "2026-10-15T10:00:01Z"},
            }
        ],
    }
    result = transcript_entities([wire], "session_1")
    tool = result["tools"][0]
    assert tool["state"] == "unknown"
    assert "Execution outcome unavailable" in tool["error"]
    assert tool["output"] is None
    assert "completed_at" not in tool
    assert tool["duration_ms"] is None


@pytest.mark.parametrize("stop_reason", [None, "pause_for_user"])
def test_live_or_paused_invocation_is_not_closed(stop_reason: str | None) -> None:
    result = transcript_entities(
        [
            {
                "id": "live",
                "session_id": "session_1",
                "role": "assistant",
                "stop_reason": stop_reason,
                "parts": [
                    {
                        "id": "call",
                        "type": "tool_call",
                        "call_id": "call_1",
                        "tool_name": "shell_run",
                    }
                ],
            }
        ],
        "session_1",
    )
    assert result["tools"][0]["state"] == "running"


def test_real_result_wins_over_turn_error() -> None:
    result = transcript_entities(
        [
            {
                "id": "message_1",
                "session_id": "session_1",
                "role": "assistant",
                "stop_reason": "error",
                "parts": [
                    {
                        "id": "call",
                        "type": "tool_call",
                        "call_id": "call_1",
                        "tool_name": "shell_run",
                    },
                    {
                        "id": "result",
                        "type": "tool_result",
                        "call_id": "call_1",
                        "tool_name": "shell_run",
                        "duration_ms": 15,
                        "structured_content": {"exit_code": 0, "stdout": "components=26"},
                    },
                ],
            }
        ],
        "session_1",
    )
    tool = result["tools"][0]
    assert tool["state"] == "succeeded"
    assert tool["output"]["stdout"] == "components=26"
    assert tool["duration_ms"] == 15
    assert "error" not in tool


@pytest.mark.parametrize(
    "status,owner,current_state",
    [
        ("idle", None, "unknown"),
        ("running", None, "unknown"),
        ("running", "finished", "unknown"),
        ("running", "pending", "running"),
        ("waiting_user", None, "running"),
        ("waiting_permission", None, "running"),
    ],
)
def test_reload_uses_live_ownership_when_storage_lost_terminal_reason(
    tmp_path: Path, status: str, owner: str | None, current_state: str
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="Incomplete history")
    messages = [
        Message(
            id=message_id,
            session_id=session.id,
            role="assistant",
            created_at="2026-10-15T10:00:00Z",
            updated_at="2026-10-15T10:00:05Z",
            parts=[
                Part(id=f"part_{message_id}", type="tool_call", call_id=message_id, tool_name="run")
            ],
        )
        for message_id in ["previous", "current"]
    ]
    app.state.messages[session.id] = messages
    app.state.sessions.update(session.id, message_count=2, status=status)
    # A stale live alias can survive failed persistence. Only the current
    # runtime task, or its actual waiting-for-user state, establishes ownership.
    app.state.live_assistant_message_ids[session.id] = "current"
    app.state.live_assistant_parts[session.id] = messages[-1].parts
    if owner is not None:
        app.state.in_flight_turns[session.id] = SimpleNamespace(done=lambda: owner == "finished")
    response = TestClient(app).get(
        f"/v1/sessions/{session.id}/messages",
        headers={"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"},
    )
    assert response.status_code == 200, response.text
    tools = {tool["id"]: tool for tool in response.json()["tools"]}
    assert tools["previous"]["state"] == "unknown"
    assert tools["current"]["state"] == current_state
    assert tools["current"]["duration_ms"] is None
    if current_state == "unknown":
        assert "Execution outcome unavailable" in tools["current"]["error"]
    else:
        assert "error" not in tools["current"]
