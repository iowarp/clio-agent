"""CLIO's records of a tool result keep it whole in clio-core, whatever its size.

The user's rule: all data that goes to the agent is stored in clio-core. The record of a
NASA Earthdata granule search (GG-F3, tens of MB) is no longer cut to its head with the
rest in a file; a full store grows instead (``clio_core_growth.recover_capacity``). Only a
view-only call's content, which never reaches the agent, is kept as a size and hash.
"""

from __future__ import annotations

from pathlib import Path

import mcp_types
from fastmcp.client.client import CallToolResult

from clio_agent.tools import recorded_results, view_only_results
from clio_agent.tools.tool_observation import notify_tool_observer

GRANULES = {"items": [{"id": f"G{n}", "polygon": "47.1 -122.4 " * 40} for n in range(4000)]}


def _mcp(structured: object, text: str) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


def test_a_large_result_is_recorded_whole() -> None:
    """SABOTAGE: bound the record again and the granules are no longer in it."""
    whole = _mcp(GRANULES, "granules")
    assert recorded_results.observed(whole) is whole
    text = "row\n" * 500_000
    assert recorded_results.observed(text) == text
    assert recorded_results.observed(None) is None


def test_the_ledger_copy_of_a_large_reply_is_whole() -> None:
    reply = CallToolResult(
        content=[mcp_types.TextContent(type="text", text="granules")],
        structured_content=GRANULES,
        meta=None,
        data=GRANULES,
        is_error=False,
    )
    assert recorded_results.ledger_reply(reply) is reply


def test_a_view_only_call_records_no_content_however_large() -> None:
    definition = {"_meta": {"ui": {"visibility": ["app"]}}}
    with view_only_results.scope("pdf_read_pdf_bytes", definition):
        record = recorded_results.observed(_mcp(GRANULES, "chunk"))
    assert "view_only_result" in record["structuredContent"]


def test_the_observer_receives_the_whole_result() -> None:
    seen: list[object] = []
    whole = _mcp(GRANULES, "granules")
    notify_tool_observer(
        lambda *a: seen.append(a[4]), "earthdata_search", {}, "completed", None, whole
    )
    assert seen[0]["structuredContent"] is GRANULES


def test_a_spilled_shell_stream_is_read_back_whole(tmp_path: Path) -> None:
    """SABOTAGE: return ``result`` unchanged -> clio-core keeps only the head."""
    text = "line\n" * 200_000
    spill = tmp_path / "call.stdout.txt"
    spill.write_bytes(text.encode("utf-8"))
    shell = {
        "stdout": "line\n" * 10,
        "stdout_truncated": True,
        "stdout_spill": {"status": "spilled", "path": str(spill), "total_bytes": len(text)},
        "stderr": "",
        "stderr_truncated": False,
    }
    mcp = {"content": [], "structuredContent": shell, "isError": False}
    whole = recorded_results.with_whole_streams(mcp)
    assert whole["structuredContent"]["stdout"] == text
    assert whole["structuredContent"]["stdout_truncated"] is False
    assert whole["structuredContent"]["stdout_spill"]["path"] == str(spill)
    assert shell["stdout"] == "line\n" * 10  # the model's copy keeps its head
    assert recorded_results.with_whole_streams(shell)["stdout"] == text  # a native result
    gone = {**shell, "stdout_spill": {"status": "spilled", "path": str(tmp_path / "gone")}}
    assert recorded_results.with_whole_streams(gone) is gone
    assert recorded_results.with_whole_streams("text") == "text"
