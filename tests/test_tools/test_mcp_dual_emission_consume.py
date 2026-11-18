"""MCP result single-emission contracts for the tool-result observer seam.

Keep the common parse-and-compare twin detector, negative distinct-content
cases, and real FastMCP inline calls.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from clio_agent.tools.execution import SyncMCPToolExecutor
from clio_agent.tools.mcp_results import (
    call_tool_result_to_observer,
    content_blocks_for_wire,
)

_NESTED_STRUCTURED = {
    "execution_id": "exec_01f33476ad965a1abca1146189464282",
    "pipeline_id": "smoke-hostname-p1",
    "state": "preparing",
}


def _nested_dual_emission() -> dict[str, Any]:
    """A standard MCP dual emission nested inside another payload: a
    ``content[].text`` stringified fallback beside the SAME object under
    ``structuredContent``."""

    return {
        "content": [{"type": "text", "text": json.dumps(_NESTED_STRUCTURED)}],
        "structuredContent": _NESTED_STRUCTURED,
    }


# --------------------------------------------------------------------------- #
# call_tool_result_to_observer -- the seam every tool_result Part's           #
# content_blocks field reads from. Driven through a REAL FastMCP round trip,  #
# since FastMCP's own ToolResult auto-fill is what actually produces the text #
# twin when only structured_content is supplied.                              #
# --------------------------------------------------------------------------- #


def _structured_only_server(payload: dict[str, Any]) -> FastMCP:
    server = FastMCP("dual-emission-probe")

    @server.tool
    def structured_only() -> ToolResult:
        """Return only structured content."""

        return ToolResult(structured_content=payload)

    return server


def test_structured_content_only_result_content_blocks_carry_no_twin() -> None:
    """FAILING-FIRST: FastMCP auto-fills ``content`` from ``structured_content``
    when a tool supplies only the latter. Before the fix, that auto-filled text block -- the ENTIRE
    payload re-stringified -- rode straight through to the wire's
    content_blocks field. After the fix, content_blocks must be empty
    (None, per the "never an invented empty list" contract) and
    structured_content must remain the one true copy."""

    payload = {
        "job": {"job_id": "job-1", "state": "succeeded", "terminal": True},
        "resolved_via": "task_record",
        "result": _nested_dual_emission(),
    }
    server = _structured_only_server(payload)

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        raw_result = executor.call_tool_result("structured_only", {})

    observed = call_tool_result_to_observer(raw_result)

    assert content_blocks_for_wire(observed) is None
    assert observed["content"] == []
    assert observed["structuredContent"] == payload


def test_structured_content_with_distinct_text_block_preserves_it() -> None:
    """Negative: a tool that supplies BOTH structured_content AND a genuinely
    distinct human-facing text block (not FastMCP's auto-fill) must keep that
    block on the wire -- content_blocks exist for real additional content,
    never suppressed wholesale just because structured_content is present."""

    payload = {"value": 42}
    server = FastMCP("distinct-text-probe")

    @server.tool
    def distinct_text() -> ToolResult:
        """Return structured content plus a genuinely distinct text block."""

        return ToolResult(
            content=[TextContent(type="text", text="human-facing summary")],
            structured_content=payload,
        )

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        raw_result = executor.call_tool_result("distinct_text", {})

    observed = call_tool_result_to_observer(raw_result)

    assert content_blocks_for_wire(observed) == [{"type": "text", "text": "human-facing summary"}]
    assert observed["structuredContent"] == payload


def test_structured_content_twin_plus_distinct_block_keeps_only_distinct() -> None:
    """A tool that explicitly supplies BOTH the auto-fill-shaped twin text AND
    a genuinely distinct block: only the twin is dropped."""

    payload = {"value": 7}
    server = FastMCP("twin-plus-distinct-probe")

    @server.tool
    def twin_plus_distinct() -> ToolResult:
        """Return the standard dual-emission twin plus one distinct block."""

        return ToolResult(
            content=[
                TextContent(type="text", text=json.dumps(payload)),
                TextContent(type="text", text="a genuinely distinct note"),
            ],
            structured_content=payload,
        )

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        raw_result = executor.call_tool_result("twin_plus_distinct", {})

    observed = call_tool_result_to_observer(raw_result)

    assert content_blocks_for_wire(observed) == [
        {"type": "text", "text": "a genuinely distinct note"}
    ]
