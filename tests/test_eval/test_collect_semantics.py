"""The per-run report's context, compaction, delegation and tool-return columns.

SABOTAGE (each turns the named test red):

* ``compaction_rows``: let "before" reach into the compaction (``t <= end``) ->
  ``test_a_compaction_records_the_context_before_after_and_its_checkpoint_call``.
* ``prompt_tokens``: drop ``cache_read_tokens`` ->
  ``test_a_compaction_records_the_context_before_after_and_its_checkpoint_call``.
* ``run_semantics``: count every session as a child ->
  ``test_run_semantics_counts_children_injections_and_spill_reads``.
"""

from __future__ import annotations

from typing import Any

from scripts.eval.tool_calling.behaviour import run_semantics
from scripts.eval.tool_calling.collect import compaction_rows, normalize_session


def _call(at: str, read: int, write: int, content: str = "") -> dict[str, Any]:
    return {
        "event_type": "lm.call",
        "occurred_at": at,
        "payload": {
            "usage": {"input_tokens": 4, "cache_read_tokens": read, "cache_write_tokens": write},
            "content": content,
        },
    }


def _compaction(kind: str, at: str) -> dict[str, Any]:
    payload = {"compaction_id": "cmp_1", "trigger": "auto", "replaced_count": 7}
    return {"event_type": f"compaction.{kind}", "occurred_at": at, "payload": payload}


def test_a_compaction_records_the_context_before_after_and_its_checkpoint_call() -> None:
    events = [
        _call("2026-11-03T10:00:00Z", 60_000, 7_000),
        _compaction("started", "2026-11-03T10:00:01Z"),
        _call("2026-11-03T10:00:20Z", 61_000, 5_000, content="## Objective\n- the request"),
        _compaction("completed", "2026-11-03T10:00:21Z"),
        _call("2026-11-03T10:00:30Z", 33_000, 10_000),
    ]
    [row] = compaction_rows(events)
    assert row["before"] == 67_004 and row["after"] == 43_004
    assert row["checkpoint_cache_read"] == 61_000 and row["checkpoint_input"] == 66_004
    assert row["checkpoint"].startswith("## Objective") and row["replaced"] == 7


def test_run_semantics_counts_children_injections_and_spill_reads() -> None:
    root = normalize_session(
        {
            "session": {"id": "root"},
            "semantic_events": [_call("2026-11-03T10:00:00Z", 30_000, 3_000)],
            "messages": [
                {"parts": [{"type": "injection", "source": "task_result"}]},
                {"parts": [{"type": "injection", "source": "plan"}, {"type": "text"}]},
            ],
        }
    )
    root["tool_calls"] = [{"args": {"path": "/w/.clio-agent/tool-output/sess/out.txt"}}]
    child = {"depth": 1, "lm_calls": [{"model": "claude-haiku"}], "tool_calls": []}
    result = run_semantics({"sessions": [root, child]})
    assert result["children"] == 1 and result["child_models"] == ["claude-haiku"]
    assert result["injections"] == 2
    assert result["injections_by_source"] == {"task_result": 1, "plan": 1}
    assert result["spill_reads"] == 1
    assert result["prompt_first_k"] == 33.004
