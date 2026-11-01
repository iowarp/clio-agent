"""Collecting an export into the run shape, and the per-run metrics over it."""

from __future__ import annotations

import json

import pytest

from scripts.eval.tool_calling import collect
from scripts.eval.tool_calling.metrics import run_metrics, tool_matches
from scripts.eval.tool_calling.session_run import slice_stream_audit
from tests.test_eval import _runs


def _run(**export_args: str) -> dict:
    norm = collect.normalize_export(_runs.export(**export_args))
    return {
        "config": "base",
        "task": "P1",
        "run_index": 1,
        "session_id": "sess_root",
        "wall_s": 7.2,
        "sessions": norm["sessions"],
        "outcome": norm["outcome"],
        "stream_audit_enabled": True,
        "stream_audit": [
            {"stage": "provider.stateful", "reason": "first_call", "conversation": "sess_root::main"},
            {"stage": "provider.stateful", "conversation": "sess_root::main"},
            {"stage": "provider.stateful", "reason": "prefix_mismatch", "conversation": "sess_root::main"},
            {"stage": "provider.stateful", "reason": "first_call", "conversation": "sess_child::x"},
        ],
    }  # fmt: skip


def test_normalize_export_pairs_tool_calls_and_unwraps_args() -> None:
    norm = collect.normalize_export(_runs.export())
    root, child = norm["sessions"]
    assert root["session_id"] == "sess_root" and root["attribution"] == "root"
    assert child["parent_session_id"] == "sess_root" and child["depth"] == 1
    calls = {c["id"]: c for c in root["tool_calls"]}
    assert calls["call_a"]["args"] == {"filepath": "a.py"}
    assert calls["call_a"]["duration_ms"] == pytest.approx(500.0)
    assert calls["call_b"]["step_id"] == "s1" and calls["call_b"]["ok"] is True
    assert [c["step_id"] for c in root["lm_calls"]] == ["s1", "s2", "s3"]
    assert root["lm_calls"][0]["started_at"] == pytest.approx(_runs.T0)
    assert child["lm_calls"][0]["duration_ms"] is None
    assert root["events"]["compactions"] == 1
    assert root["steps"][0]["tools"] == ["fs_read_file", "fs_read_file"]


def test_derives_start_from_duration_when_started_at_is_missing() -> None:
    event = _runs.lm_event("s", start=1.0, end=3.0, usage={})
    del event["payload"]["started_at"]
    row = collect.lm_call_row(event)
    assert row["started_at"] == pytest.approx(_runs.T0 + 1.0)
    assert row["duration_ms"] == pytest.approx(2000.0)


def test_root_metrics() -> None:
    m = run_metrics(_run())
    root = m["root"]
    assert root["steps"] == 3
    assert root["calls_per_step"] == {"mean": 1.0, "max": 2}
    assert root["prompt_tokens_per_step"]["uncached_mean"] == 500.0
    assert root["prompt_tokens_per_step"]["cached_mean"] == 21000.0
    assert root["output_tokens"] == 180
    assert root["ttft_ms"] == {"median": 350.0, "n": 2}
    assert root["decode_ms"]["median"] == 1400.0
    assert root["batch"] == {"steps": 1, "wall_ms": 1000.0, "slowest_ms": 1000.0, "ratio": 1.0}
    # Step 1: 3.5 s window - 2.0 s model - 1.0 s tools; step 2: 2.5 - 1.5 - 0.2; step 3: 0.
    assert root["step_overhead_ms"]["total"] == pytest.approx(1300.0, abs=0.2)
    assert root["step_overhead_ms"]["n"] == 3
    assert root["tool_counts"]["search_tools"] == 1


def test_totals_children_resets_and_completion() -> None:
    m = run_metrics(_run())
    assert m["totals"]["steps"] == 5 and m["totals"]["child_steps"] == 2
    assert m["children"][0]["steps"] == 2
    assert m["totals"]["tool_counts"]["write_todos"] == 1
    assert m["totals"]["events"]["compactions"] == 1
    assert m["prefix_resets"] == {"first_call": 2, "prefix_mismatch": 1}
    assert m["completion"] == {"valid": True, "reason": "final_answer"}


def test_input_including_cache_is_subtracted() -> None:
    run = _run()
    run["sessions"][0]["lm_calls"][0]["usage"] = {"input_tokens": 21000, "cache_read_tokens": 20000}
    m = run_metrics(run, input_includes_cache=True)
    assert m["root"]["prompt_tokens_per_step"]["uncached_total"] == 1000 + 0 + 0


@pytest.mark.parametrize(
    ("text", "stop", "reason"),
    [
        ("", "end_turn", "empty_reply"),
        ("partial", "error", "error"),
        ("partial", "cancelled", "cancelled"),
        ("ok", "end_turn", "final_answer"),
    ],
)
def test_completion_guard(text: str, stop: str, reason: str) -> None:
    m = run_metrics(_run(final_text=text, stop_reason=stop))
    assert m["completion"] == {"valid": reason == "final_answer", "reason": reason}


def test_completion_guard_without_reply_or_on_timeout() -> None:
    assert collect.outcome([])["reason"] == "no_reply"
    messages = _runs.export()["messages"]
    assert collect.outcome(messages, timed_out=True)["final"] is False


def test_tool_name_matching() -> None:
    assert tool_matches("search_tools", "search_tools")
    assert tool_matches("core_write_todos", "write_todos")
    assert not tool_matches("research_tools_x", "search_tools")


def test_stream_audit_slice_attributes_rows_by_session(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    rows = [
        {"stage": "provider.stateful", "ts": 100.0, "conversation": "sess_root::main", "reason": "first_call"},
        {"stage": "provider.stateful", "ts": 100.0, "conversation": "sess_other::main", "reason": "first_call"},
        {"stage": "provider.batch_response", "ts": 100.0, "conversation": "sess_root::main"},
        {"stage": "provider.stateful", "ts": 500.0, "conversation": "sess_root::main", "reason": "x"},
    ]  # fmt: skip
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    got = slice_stream_audit(path, 90.0, 200.0, {"sess_root"})
    assert got == rows[:1]
