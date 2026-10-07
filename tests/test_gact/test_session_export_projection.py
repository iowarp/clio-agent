"""Archives share the live transcript identity without losing retained bodies."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.session_export_projection import archive_transcript_views


def test_tool_request_and_failed_result_share_one_live_entry() -> None:
    """A failed legacy record retains the command and typed failure in place."""
    transcript: dict[str, Any] = {
        "session": {"id": "s"},
        "messages": [
            {
                "id": "m",
                "session_id": "s",
                "role": "assistant",
                "parts": [
                    {
                        "id": "a",
                        "type": "tool_call",
                        "call_id": "c",
                        "tool_name": "shell_bash",
                        "input": {"command": "pwd", "cwd": "/data"},
                        "presentation": {
                            "action": "Run",
                            "blocks": [{"id": "terminal", "type": "terminal", "command": "pwd"}],
                        },
                    },
                    {
                        "id": "b",
                        "type": "tool_result",
                        "call_id": "c",
                        "is_error": True,
                        "text": "Access decisions could not be read",
                    },
                ],
            }
        ],
        "tool_records": [
            {
                "call_id": "c",
                "tool": "",
                "input": {"command": "pwd", "cwd": "/data"},
                "output": "",
                "error": "Access decisions could not be read",
            }
        ],
    }
    view = archive_transcript_views(transcript)["s"]
    assert len(view["messages"][0]["blocks"]) == 1
    tool = view["tools"][0]
    assert tool["id"] == view["messages"][0]["blocks"][0]["tool_id"] == "c"
    assert tool["input"] == {"command": "pwd", "cwd": "/data"}
    assert tool["name"] == "shell_bash"
    assert tool["presentation"]["blocks"][0]["command"] == "pwd"
    assert tool["error"] == "Access decisions could not be read"


def test_skill_body_expands_offline_at_its_own_tool_entry() -> None:
    """Native expansion uses the actual loaded body, including its unbounded tail."""
    body = "# Actual skill\n" + "instruction\n" * 1000 + "RECORDED TAIL"
    transcript: dict[str, Any] = {
        "session": {"id": "s"},
        "messages": [
            {
                "id": "m",
                "role": "assistant",
                "parts": [
                    {"id": "a", "type": "tool_call", "call_id": "c", "tool_name": "load_skill"},
                    {
                        "id": "b",
                        "type": "tool_result",
                        "call_id": "c",
                        "content": body,
                        "presentation": {
                            "blocks": [{"id": "content", "type": "markdown", "text": body}]
                        },
                    },
                ],
            }
        ],
        "tool_records": [{"call_id": "c", "output": body}],
    }
    view = archive_transcript_views(transcript)["s"]
    tool = view["tools"][0]
    assert tool["output"] == body
    assert tool["presentation"]["blocks"][0]["text"] == body
    assert "content_ref" not in tool["presentation"]["blocks"][0]
    assert view["messages"][0]["blocks"][0]["tool_id"] == tool["id"]
    assert transcript["messages"][0]["parts"][1]["content"] == body
