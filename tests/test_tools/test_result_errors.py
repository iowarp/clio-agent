"""A call failed when its transport says so, never because of its words or keys.

The verdict is an MCP result's ``isError``, a native tool's raise, or a native
tool's own :class:`ToolFailure`. An ``error`` key, ``status: failed``, ``ok: false``
or a leading ``Error:`` is the tool's report, read by the model like any result.
"""

from __future__ import annotations

import json
from typing import Any

from clio_agent.tools.result_errors import (
    FailedObservation,
    ToolFailure,
    declared_failure,
    structured_tool_result_error,
)


class _TextBlock:
    """Minimal stand-in for an MCP ``TextContent`` block."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _CallToolResult:
    """fastmcp's result shape: ``data`` is None when structuredContent is absent."""

    def __init__(self, content: list[Any], *, is_error: bool = False) -> None:
        self.content = content
        self.structured_content: dict[str, Any] | None = None
        self.data = None
        self.is_error = is_error


def test_words_and_keys_never_decide_failure() -> None:
    envelope = json.dumps({"error": {"code": "quota_exceeded", "message": "daily cap"}})
    assert structured_tool_result_error(_CallToolResult([_TextBlock(envelope)])) is None
    assert structured_tool_result_error(_CallToolResult([_TextBlock("Error: offline")])) is None
    assert structured_tool_result_error({"status": "failed", "message": "no rows"}) is None
    assert structured_tool_result_error({"ok": False, "error": "bad request"}) is None
    structured = _CallToolResult([_TextBlock("all good")])
    structured.structured_content = {"error": {"code": "denied"}}
    assert structured_tool_result_error(structured) is None
    assert structured_tool_result_error("Error: plain string result") is None


def test_is_error_decides_and_reports_the_results_own_text() -> None:
    envelope = '{"code":"invalid_argument","message":"steps must be within 0..9","details":{}}'
    result = {"content": [{"type": "text", "text": envelope}], "isError": True}
    assert structured_tool_result_error(result) == "invalid_argument: steps must be within 0..9"
    plain = _CallToolResult([_TextBlock("upstream timed out")], is_error=True)
    assert structured_tool_result_error(plain) == "upstream timed out"
    assert structured_tool_result_error({"content": [], "isError": True}) == "tool_result_is_error"


def test_a_native_tools_declared_failure_is_the_verdict() -> None:
    failure = ToolFailure({"ok": False, "reason": "stream_not_found", "detail": "No stream 'x'."})
    assert declared_failure(failure)
    assert json.loads(structured_tool_result_error(failure) or "") == dict(failure)
    assert not declared_failure({"ok": False, "reason": "stream_not_found"})


def test_failed_observation_text_carries_the_mcp_verdict() -> None:
    text = FailedObservation("upstream timed out")
    assert declared_failure(text)
    assert structured_tool_result_error(text) == "upstream timed out"
    assert not declared_failure("upstream timed out")
