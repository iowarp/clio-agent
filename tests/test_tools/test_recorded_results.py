"""CLIO's records of a large tool result are its head and a file, never tens of MB inline.

Found live (GG-F3): a NASA Earthdata granule search, a normal model-visible tool, wrote a
33.9 MB segment into a 128 MB clio-core store; the turn stalled until it was cancelled.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import mcp_types
import pytest
from fastmcp.client.client import CallToolResult

from clio_agent.tools import recorded_results, view_only_results
from clio_agent.tools.tool_observation import notify_tool_observer

GRANULES = {"items": [{"id": f"G{n}", "polygon": "47.1 -122.4 " * 40} for n in range(4000)]}


@pytest.fixture
def spill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "tool-output" / "sess_a"
    monkeypatch.setattr(
        "clio_agent.tools.servers.shell_spill_store.spill_directory",
        lambda _root, session_id=None: folder,
    )
    monkeypatch.setenv("CLIO_TOOL_RESULT_RECORD_BYTES", "65536")
    return folder


def _mcp(structured: object, text: str) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


def test_a_small_result_is_recorded_whole(spill: Path) -> None:
    small = _mcp({"items": [1, 2]}, "two items")
    assert recorded_results.observed(small) is small
    assert recorded_results.observed("short text") == "short text"
    assert recorded_results.observed(None) is None
    assert not spill.exists()


def test_a_large_result_is_recorded_as_its_head_and_a_file(spill: Path) -> None:
    """SABOTAGE: return ``result`` past the bound and the record is ~2 MB inline."""
    whole = _mcp(GRANULES, json.dumps(GRANULES))
    record = recorded_results.observed(whole)
    stored = record["stored_result"]
    assert stored["reason"] == recorded_results.TOOL_RESULT_STORED_IN_FILE
    body = json.dumps(whole, sort_keys=True, separators=(",", ":")).encode()
    assert stored["bytes"] == len(body) and stored["sha256"] == hashlib.sha256(body).hexdigest()
    assert Path(stored["file"]).read_bytes() == body  # the whole result, once
    assert record["structuredContent"] is None and record["isError"] is False
    assert len(json.dumps(record)) < 20_000
    assert record["content"][0]["text"].startswith("[clio: tool_result_stored_in_file]")
    assert '{"items"' in record["content"][0]["text"]  # the head follows the note
    again = recorded_results.observed(whole)  # the same result: the same file, not a second
    assert again["stored_result"]["file"] == stored["file"]
    assert len(list(spill.iterdir())) == 1


def test_a_large_native_text_is_recorded_as_its_head_and_a_file(spill: Path) -> None:
    text = "row\n" * 50_000
    record = recorded_results.observed(text)
    assert isinstance(record, str) and record.startswith("[clio: tool_result_stored_in_file]")
    assert len(record) < 20_000
    assert (spill / next(iter(p.name for p in spill.iterdir()))).read_text() == text


def test_a_file_that_cannot_be_written_is_typed_and_still_not_inline(
    spill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spill.parent.mkdir(parents=True)
    spill.write_text("a file where the folder should be")
    record = recorded_results.observed(_mcp(GRANULES, "granules"))
    assert record["stored_result"]["reason"] == recorded_results.TOOL_RESULT_NOT_STORED
    assert "file" not in record["stored_result"] and record["stored_result"]["error"]
    assert len(json.dumps(record)) < 20_000


def test_the_ledger_copy_of_a_large_reply_keeps_its_type(spill: Path) -> None:
    text = json.dumps(GRANULES)
    reply = CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        structured_content=GRANULES,
        meta=None,
        data=GRANULES,
        is_error=False,
    )
    stored = recorded_results.ledger_reply(reply)
    assert isinstance(stored, CallToolResult) and stored.data is None
    record = stored.structured_content["stored_result"]
    assert record["reason"] == recorded_results.TOOL_RESULT_STORED_IN_FILE
    assert Path(record["file"]).exists() and text not in str(stored)
    assert reply.structured_content is GRANULES  # the caller's reply is untouched
    small = CallToolResult(content=[], structured_content={}, meta=None, data=None, is_error=False)
    assert recorded_results.ledger_reply(small) is small


def test_a_view_only_call_records_no_content_however_large(spill: Path) -> None:
    definition = {"_meta": {"ui": {"visibility": ["app"]}}}
    with view_only_results.scope("pdf_read_pdf_bytes", definition):
        record = recorded_results.observed(_mcp(GRANULES, "chunk"))
    assert "view_only_result" in record["structuredContent"]
    assert not spill.exists()  # nothing of a view-only result is kept, not even a file


def test_the_observer_receives_the_bounded_record(spill: Path) -> None:
    seen: list[object] = []
    whole = _mcp(GRANULES, "granules")
    notify_tool_observer(
        lambda *a: seen.append(a[4]), "earthdata_search", {}, "completed", None, whole
    )
    assert seen[0]["stored_result"]["reason"] == recorded_results.TOOL_RESULT_STORED_IN_FILE
    assert whole["structuredContent"] is GRANULES  # the caller's copy is whole
