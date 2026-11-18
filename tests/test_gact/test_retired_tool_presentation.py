"""Recorded results of a built-in tool CLIO no longer ships still render from the ledger."""

from __future__ import annotations

import json
from typing import Any

from clio_agent.gact.agents.native_tool_registry import NATIVE_TOOL_NAMES
from clio_agent.gact.protocol.v3.message import transcript_entities
from clio_agent.gact.session_export_projection import archive_transcript_views
from clio_agent.gact.tool_result_presentation import recorded_presentation

RETIRED = "retired_remote_command"


def _result_part(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"command": "hostname", "stdout": "ares.ares.local\n", "stderr": "", "exit_code": 0}
    return {
        "id": "result",
        "type": "tool_result",
        "tool_name": RETIRED,
        "call_id": "call",
        "metadata": {"result": result},
        "content": [{"type": "text", "text": json.dumps(result)}],
        "presentation": {"status": "completed", "summary": "Exit 0", "blocks": blocks},
    }


def _transcript(part: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    call = {
        "id": "call-part",
        "type": "tool_call",
        "tool_name": RETIRED,
        "call_id": "call",
        "input": {"command": "hostname"},
    }
    message = {"id": "m", "session_id": "sid", "role": "assistant", "parts": [call, part]}
    return message, {"session": {"id": "sid"}, "messages": [message]}


def test_declared_blocks_render_without_the_tool() -> None:
    assert RETIRED not in NATIVE_TOOL_NAMES
    terminal = {
        "id": "terminal",
        "type": "terminal",
        "command": "hostname",
        "text": "ares.ares.local\n",
        "exit_code": 0,
    }
    part = _result_part([terminal])
    original = json.dumps(part)
    message, transcript = _transcript(part)

    assert recorded_presentation(part) == part["presentation"]
    (tool,) = transcript_entities([message], "sid")["tools"]
    assert tool["name"] == RETIRED
    assert tool["presentation"]["blocks"][0]["text"] == "ares.ares.local\n"
    archived = archive_transcript_views(transcript)["sid"]["tools"][0]
    assert archived["presentation"]["blocks"][0]["command"] == "hostname"
    assert json.dumps(part) == original


def test_empty_recorded_blocks_keep_their_summary() -> None:
    part = _result_part([])
    message, transcript = _transcript(part)

    assert recorded_presentation(part) == part["presentation"]
    (tool,) = transcript_entities([message], "sid")["tools"]
    assert tool["presentation"]["summary"] == "Exit 0"
    assert tool["presentation"]["blocks"] == []
    archived = archive_transcript_views(transcript)["sid"]["tools"][0]
    assert archived["presentation"]["summary"] == "Exit 0"
