"""Both lanes of tool-result truth: structured payload AND content blocks.

``structured_tool_result_error`` used to be fed ``outcome.model_text`` -- the
verbatim text of the result's content blocks -- and moved to
``outcome.raw_result`` so the structured lane and ``isError`` are read at their
source. That swap dropped the content-block lane entirely: a non-FastMCP server
that answers with its error envelope ONLY inside a text block (no
``structuredContent``, ``isError=false``) had its failures recorded as
successes on the transcript, in the ledger row, and in the semantic event.
"""

from __future__ import annotations

import json
from typing import Any

from clio_agent.tools.result_errors import structured_tool_result_error


class _TextBlock:
    """Minimal stand-in for an MCP ``TextContent`` block."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _ImageBlock:
    """Minimal stand-in for an MCP ``ImageContent`` block (never an error lane)."""

    def __init__(self, data: str) -> None:
        self.type = "image"
        self.data = data
        self.mimeType = "image/png"


class _CallToolResult:
    """fastmcp's result shape: ``data`` is None when structuredContent is absent."""

    def __init__(self, content: list[Any], *, is_error: bool = False) -> None:
        self.content = content
        self.structured_content = None
        self.data = None
        self.is_error = is_error


def test_content_block_only_error_envelope_is_a_failure() -> None:
    result = _CallToolResult(
        [_TextBlock(json.dumps({"error": {"code": "quota_exceeded", "message": "daily cap"}}))]
    )

    assert structured_tool_result_error(result) == "quota_exceeded: daily cap"


def test_content_block_error_prefixed_text_is_a_failure() -> None:
    result = _CallToolResult([_TextBlock("Error: upstream station catalog is offline")])

    assert structured_tool_result_error(result) == "Error: upstream station catalog is offline"


def test_content_block_status_and_ok_envelopes_are_failures() -> None:
    status = _CallToolResult([_TextBlock('{"status": "failed", "message": "no rows"}')])
    assert structured_tool_result_error(status) == "status=failed: no rows"

    not_ok = _CallToolResult([_TextBlock('{"ok": false, "error": "bad request"}')])
    assert structured_tool_result_error(not_ok) == "bad request"


def test_mapping_shaped_result_carries_the_content_lane_too() -> None:
    """The raw result also arrives as a plain mapping on some paths."""

    result = {"content": [{"type": "text", "text": '{"error": "boom"}'}], "isError": False}

    assert structured_tool_result_error(result) == "boom"


def test_successful_content_only_result_is_not_misclassified() -> None:
    ok_text = _CallToolResult([_TextBlock(json.dumps({"rows": 12, "status": "success"}))])
    assert structured_tool_result_error(ok_text) is None

    prose = _CallToolResult([_TextBlock("complete: 12 rows written")])
    assert structured_tool_result_error(prose) is None

    # A declared-but-empty error key is not a failure.
    empty = _CallToolResult([_TextBlock('{"error": null, "rows": 3}')])
    assert structured_tool_result_error(empty) is None


def test_non_text_and_malformed_blocks_never_raise() -> None:
    assert structured_tool_result_error(_CallToolResult([_ImageBlock("QUJD")])) is None
    assert structured_tool_result_error(_CallToolResult([])) is None
    assert structured_tool_result_error({"content": "not-a-list"}) is None
    assert structured_tool_result_error({"content": [None, 7, {"type": "text"}]}) is None
    assert structured_tool_result_error("plain string result") is None


def test_structured_lane_still_wins_and_is_error_still_counts() -> None:
    structured = _CallToolResult([_TextBlock("all good")])
    structured.structured_content = {"error": {"code": "denied"}}
    assert structured_tool_result_error(structured) == "denied"

    assert (
        structured_tool_result_error(_CallToolResult([], is_error=True)) == "tool_result_is_error"
    )
