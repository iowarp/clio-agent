"""Parsing ``claude -p --output-format stream-json`` into the run shape metrics read."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.eval.tool_calling.claude_code_ref import (
    ancestor_instructions,
    parse_stream,
    rate_windows,
    seven_day_utilization,
)
from scripts.eval.tool_calling.metrics import run_metrics
from scripts.eval.tool_calling.summarize import window_share


def _se(event: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    return {"type": "stream_event", "event": event, "parent_tool_use_id": parent}


def _assistant(mid: str, content: list[dict], usage: dict, parent: str | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {"id": mid, "model": "claude-sonnet-5-5", "content": content, "usage": usage},
        "parent_tool_use_id": parent,
    }


def _result_for(tool_id: str, parent: str | None = None, error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_id, "is_error": error, "content": "x"}
    return {"type": "user", "message": {"content": [block]}, "parent_tool_use_id": parent}


STREAM: list[tuple[float, dict[str, Any]]] = [
    (100.1, {"type": "system", "subtype": "init", "session_id": "cc-1", "model": "claude-sonnet-5-5"}),
    (101.0, _se({"type": "message_start", "message": {"id": "msg_1", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 5000,
        "cache_creation_input_tokens": 300, "output_tokens": 1}}})),
    (101.4, _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let"}})),
    (102.0, _assistant("msg_1", [
        {"type": "text", "text": "Looking"},
        {"type": "tool_use", "id": "toolu_1", "name": "Grep", "input": {"pattern": "f"}},
        {"type": "tool_use", "id": "toolu_2", "name": "Task", "input": {"prompt": "dig"}},
    ], {"input_tokens": 10, "output_tokens": 40})),
    (102.1, _se({"type": "message_delta", "usage": {"output_tokens": 55}})),
    (102.2, _se({"type": "message_stop"})),
    (102.5, _result_for("toolu_1")),
    (103.0, _assistant("msg_c1", [{"type": "tool_use", "id": "toolu_3", "name": "Read",
                                   "input": {"file_path": "a.py"}}],
                       {"input_tokens": 20, "output_tokens": 10}, parent="toolu_2")),
    (103.5, _result_for("toolu_3", parent="toolu_2", error=True)),
    (104.0, _assistant("msg_c2", [{"type": "text", "text": "a.py:3"}],
                       {"input_tokens": 30, "output_tokens": 5}, parent="toolu_2")),
    (104.2, _result_for("toolu_2")),
    (104.5, _se({"type": "message_start", "message": {"id": "msg_2", "usage": {"input_tokens": 4}}})),
    (105.0, _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Found"}})),
    (105.6, _assistant("msg_2", [{"type": "text", "text": "Found it"}], {"output_tokens": 9})),
    (105.7, _se({"type": "message_stop"})),
    (105.8, {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "seven_day",
                                                            "utilization": 0.5}}),
    (105.9, {"type": "result", "subtype": "success", "is_error": False, "result": "Found it at a.py:3",
             "num_turns": 2, "duration_ms": 5800}),
]  # fmt: skip


def test_parse_root_and_subagent_sessions() -> None:
    parsed = parse_stream(STREAM, 100.0)
    root, child = parsed["sessions"]
    assert root["session_id"] == "main" and child["session_id"] == "toolu_2"
    assert child["parent_session_id"] == "main" and child["depth"] == 1
    first, second = root["lm_calls"]
    assert first["duration_ms"] == pytest.approx(2200.0)
    assert first["ttft_ms"] == pytest.approx(1400.0)
    assert first["usage"] == {
        "input_tokens": 10,
        "output_tokens": 55,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 300,
        "reasoning_tokens": 0,
    }
    assert second["started_at"] == pytest.approx(104.2)  # after the last tool result
    assert second["ttft_ms"] == pytest.approx(800.0)
    tools = {t["id"]: t for t in root["tool_calls"]}
    assert tools["toolu_1"]["duration_ms"] == pytest.approx(500.0)
    assert tools["toolu_2"]["step_id"] == "msg_1"
    assert [c["started_at"] for c in child["lm_calls"]] == pytest.approx([102.0, 103.5])
    assert child["lm_calls"][0]["ttft_ms"] is None
    assert child["tool_calls"][0]["ok"] is False
    assert parsed["outcome"]["final"] and parsed["outcome"]["final_text"] == "Found it at a.py:3"
    assert parsed["claude_result"]["num_turns"] == 2


def test_metrics_read_the_reference_run() -> None:
    run = {"source": "claude_code", "wall_s": 5.9, **parse_stream(STREAM, 100.0)}
    m = run_metrics(run)
    assert m["root"]["steps"] == 2 and m["totals"]["child_steps"] == 2
    assert m["root"]["batch"]["ratio"] == pytest.approx(1.0)
    assert m["root"]["prompt_tokens_per_step"]["cached_total"] == 5000
    assert m["completion"]["valid"]


def test_failed_or_empty_result_is_not_final() -> None:
    errored = [
        *STREAM[:-1],
        (106.0, {"type": "result", "subtype": "error_max_turns", "is_error": True}),
    ]
    assert parse_stream(errored, 100.0)["outcome"]["reason"] == "error_max_turns"
    assert parse_stream(STREAM[:5], 100.0)["outcome"]["reason"] == "no_result"


def test_rate_limits_and_window_share() -> None:
    assert rate_windows({"unifiedWindows": {"five_hour": {"utilization": 0.2}}}) == {
        "five_hour": 0.2
    }
    assert rate_windows({"rateLimitType": "seven_day", "utilization": 0.4}) == {"seven_day": 0.4}
    limits = [
        {
            "unifiedWindows": {
                "five_hour": {"utilization": 0.20},
                "seven_day": {"utilization": 0.40},
            }
        },
        {
            "unifiedWindows": {
                "five_hour": {"utilization": 0.25},
                "seven_day": {"utilization": 0.41},
            }
        },
    ]
    assert seven_day_utilization(limits) == 0.41
    five, week = window_share({"rate_limits": limits})
    assert five == pytest.approx(5.0) and week == pytest.approx(1.0)


def test_ancestor_instructions(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("x")
    work = tmp_path / "a" / "b"
    work.mkdir(parents=True)
    assert str(tmp_path / "CLAUDE.md") in ancestor_instructions(work)
