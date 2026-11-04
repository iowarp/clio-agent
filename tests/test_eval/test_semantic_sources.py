"""The Claude Code stream and stored traces read into the same semantic record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling import semantic_sources


def _row(at: float, event: dict[str, Any]) -> dict[str, Any]:
    return {"recv": at, "event": event}


def _start(at: float, parent: str | None = None) -> dict[str, Any]:
    usage = {"input_tokens": 2, "cache_read_input_tokens": 100, "output_tokens": 1}
    return _row(at, {"type": "stream_event", "parent_tool_use_id": parent,
                     "event": {"type": "message_start", "message": {"usage": usage}}})  # fmt: skip


def _delta(at: float, out: int, parent: str | None = None) -> dict[str, Any]:
    return _row(at, {"type": "stream_event", "parent_tool_use_id": parent, "event": {
        "type": "message_delta", "usage": {"output_tokens": out,
                                           "output_tokens_details": {"thinking_tokens": 7}}}})  # fmt: skip


def _assistant(at: float, block: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    return _row(at, {"type": "assistant", "parent_tool_use_id": parent,
                     "message": {"content": [block]}})  # fmt: skip


def _result(at: float, uid: str, text: str, parent: str | None = None) -> dict[str, Any]:
    return _row(at, {"type": "user", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_result", "tool_use_id": uid, "content": text, "is_error": False}]}})  # fmt: skip


def test_a_stream_reads_into_calls_steps_and_a_task_child() -> None:
    """SABOTAGE: drop the ``parent_tool_use_id`` key and the child's call joins the root."""
    rows = [
        _start(1.0), _assistant(1.1, {"type": "text", "text": "Looking."}),
        _assistant(1.2, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}),
        _assistant(1.3, {"type": "tool_use", "id": "t2", "name": "Task",
                         "input": {"prompt": "Survey b.csv"}}),
        _delta(1.4, 90),
        _result(1.5, "t1", "a.csv\nb.csv"),
        _start(1.6, "t2"), _assistant(1.7, {"type": "text", "text": "b has 3 cols"}, "t2"),
        _delta(1.8, 20, "t2"),
        _result(1.9, "t2", "b has 3 cols"),
        _start(2.0), _assistant(2.1, {"type": "text", "text": "Done."}), _delta(2.2, 30),
    ]  # fmt: skip
    record = semantic_sources.claude_code_record(rows, "Explore the data.")
    root, child = record["sessions"]
    assert [len(s["calls"]) for s in root["steps"]] == [2, 0]
    assert root["steps"][0]["calls"][0]["output"] == "a.csv\nb.csv"
    assert root["model_calls"][0]["usage"]["output_tokens"] == 90
    assert root["model_calls"][0]["usage"]["reasoning_tokens"] == 7
    assert [p["name"] for p in root["model_calls"][1]["incoming"]] == ["Bash", "Task"]
    assert child["turns"][0]["text"] == "Survey b.csv" and child["final_text"] == "b has 3 cols"
    assert root["final_text"] == "Done."


def test_a_stored_run_reads_its_sessions_traces(tmp_path: Path) -> None:
    (tmp_path / "sess_a.semantic.jsonl").write_text(json.dumps(
        {"event_type": "turn.started", "occurred_at": "2026-11-04T00:00:00+00:00",
         "payload": {"text": "go"}}) + "\n")  # fmt: skip
    export, unread = semantic_sources.trace_export(["sess_a", "sess_gone"], tmp_path)
    assert export["semantic_events"][0]["payload"]["text"] == "go"
    assert export["children"][0]["relationship"]["depth"] == 1
    assert "trace of sess_gone" in unread
