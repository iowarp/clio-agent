"""Missing terminal records are uncertainty, never an indefinitely running command."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.protocol.v3.message import transcript_entities


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
