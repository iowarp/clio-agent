"""The semantic record keeps what a run's agent saw and did; the runner answers plans."""

from __future__ import annotations

from typing import Any

from scripts.eval.tool_calling import semantic
from scripts.eval.tool_calling.session_run import REVISE_NOTE, _answer_plan


def _event(kind: str, at: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"event_type": kind, "occurred_at": at, "payload": payload}


def _lm(at: str, messages: list[dict[str, Any]], started: str) -> dict[str, Any]:
    return _event("lm.call", at, {
        "messages": messages, "started_at": started, "timestamp": at,
        "usage": {"input_tokens": 5, "cache_read_tokens": 100, "output_tokens": 40},
        "prompt_shape": {"system_chars": 900, "tools": [["fs_glob", 300], ["shell_bash", 700]]},
    })  # fmt: skip


def _export() -> dict[str, Any]:
    root_events = [
        _event("turn.started", "2026-11-04T00:00:00+00:00", {"text": "Explore the data."}),
        _lm("2026-11-04T00:00:02+00:00", [
            {"role": "user", "parts": [{"type": "text", "text": "[clio: session_facts]\nroot"}]},
            {"role": "user", "parts": [{"type": "text", "text": "Explore the data."}]},
        ], "2026-11-04T00:00:01+00:00"),
        _event("react.step.completed", "2026-11-04T00:00:03+00:00", {
            "step_index": 0, "thought": "Listing it.", "tool_calls": [
                {"name": "fs_glob", "args": {"pattern": "*"}, "observation": "a.csv\nb.csv",
                 "is_error": False}]}),
        _lm("2026-11-04T00:00:05+00:00", [
            {"role": "tool", "parts": [{"type": "tool_result", "name": "fs_glob",
                                        "content": [{"text": "a.csv\nb.csv"}]}]},
        ], "2026-11-04T00:00:04+00:00"),
        _event("react.step.completed", "2026-11-04T00:00:06+00:00", {
            "step_index": 1, "thought": "Two files.", "tool_calls": []}),
    ]  # fmt: skip
    child = {
        "session": {"id": "sess_child", "agent": {"id": "explore"}},
        "relationship": {"session_id": "sess_child", "depth": 1, "attribution": "agent_task"},
        "semantic_events": [
            _event("turn.started", "2026-11-04T00:00:01+00:00", {"text": "Survey b.csv"}),
        ],
        "messages": [
            {"role": "assistant", "parts": [{"type": "text", "text": "b.csv has 3 cols"}]}
        ],
        "loaded_skills": [{"name": "csv-reading"}],
    }
    return {
        "session": {"id": "sess_root", "agent": {"id": "standard"}},
        "semantic_events": root_events,
        "messages": [],
        "children": [child],
    }


def test_the_record_classifies_what_each_call_received_and_pairs_steps() -> None:
    """SABOTAGE: drop the ``[clio: ...]`` match and the injection reads as the person's
    text; pair by index without time and a checkpoint call shifts every step."""
    record = semantic.semantic_record(
        _export(),
        work={"todos": [{"status": "completed", "content": "List files"}]},
        plan_text="1. List files",
        exact_prompt={"system": "S" * 10, "tools": [{"name": "fs_glob", "description": "d",
                                                    "parameters": {"type": "object"}}]},
    )  # fmt: skip
    root, child = record["sessions"]
    first = root["model_calls"][0]["incoming"]
    assert [p["kind"] for p in first] == ["injection", "user_text"]
    assert first[0]["source"] == "session_facts"
    second = root["model_calls"][1]
    assert second["incoming"][0] == {
        "kind": "tool_result", "name": "fs_glob", "is_error": False, "chars": 11,
        "text": "a.csv\nb.csv",
    }  # fmt: skip
    assert second["tool_chars"] == 1000 and second["prompt_tokens"] == 105
    assert child["skills"] == ["csv-reading"] and child["final_text"] == "b.csv has 3 cols"
    assert record["exact_prompt"]["tool_chars"] == {"fs_glob": 1 + len('{"type": "object"}')}
    report = semantic.reading_report(record, {"task": "X1", "run_index": 1, "config": "t"})
    assert "- in: injection `session_facts`" in report
    assert '- calls `fs_glob` {"pattern": "*"} -> 11 ch' in report
    assert "- says: Two files." in report
    assert "[completed] List files" in report and "Task: Survey b.csv" in report


class _Questions:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.answers: list[tuple[str, str, str]] = []

    def pending_questions(self, sid: str) -> list[dict[str, Any]]:
        return list(self.rows)

    def answer_question(self, sid: str, qid: str, value: str, answer: str = "") -> None:
        self.answers.append((qid, value, answer))


def test_a_plan_is_rejected_once_with_a_note_then_approved() -> None:
    """SABOTAGE: approve on the first answer under ``reject_once`` and the revision path
    is never exercised."""
    client = _Questions([
        {"id": "q_ask", "source": "ask_user", "prompt": "Which file?"},
        {"id": "q_plan", "source": "plan_exit", "prompt": "Plan: list files"},
    ])  # fmt: skip
    answers: list[dict[str, Any]] = []
    assert _answer_plan(client, "s", "reject_once", answers, 3.0)  # type: ignore[arg-type]
    assert _answer_plan(client, "s", "reject_once", answers, 9.0)  # type: ignore[arg-type]
    assert client.answers == [("q_plan", "reject", REVISE_NOTE), ("q_plan", "auto", "")]
    assert [a["choice"] for a in answers] == ["reject", "auto"]


def test_only_a_plan_question_is_answered() -> None:
    client = _Questions([{"id": "q_ask", "source": "ask_user", "prompt": "Which file?"}])
    assert not _answer_plan(client, "s", "approve", [], 1.0)  # type: ignore[arg-type]
    assert client.answers == []


def test_a_call_without_a_step_does_not_shift_the_steps() -> None:
    """A compaction's checkpoint call sits between two steps. SABOTAGE: pair by index and
    the checkpoint takes the second step."""
    calls = [{"started_at": 1.0}, {"started_at": 3.0}, {"started_at": 5.0}]
    steps = [{"at": 2.0, "index": 0}, {"at": 6.0, "index": 1}]
    paired = semantic._pair(calls, steps)
    assert [None if s is None else s["index"] for s in paired] == [0, None, 1]


def test_a_row_counts_unreadable_blocks_apart_from_tool_errors() -> None:
    """SABOTAGE: count an unreadable block as a tool error and the two columns merge."""
    step = {"calls": [
        {"name": "clio_invalid_tool_calls", "is_error": True, "output_chars": 120},
        {"name": "shell_bash", "is_error": True, "output_chars": 400},
        {"name": "fs_glob", "is_error": False, "output_chars": 10_000},
    ]}  # fmt: skip
    record = {
        "sessions": [{"model_calls": [{"prompt_tokens": 20_000, "incoming": []},
                                      {"prompt_tokens": 50_000, "incoming": []}],
                      "steps": [step], "skills": []}],
        "work": {"plan": "", "todos": [{"status": "pending"}], "todo_history": [{}]},
    }  # fmt: skip
    row = semantic.semantic_row(record)
    assert (row["invalid_blocks"], row["tool_errors"]) == (1, 1)
    assert row["output_max_k"] == 10.0 and row["prompt_growth"] == 2.5
    assert (row["todo_writes"], row["todos_open"]) == (2.0, 1.0)


def test_only_run_results_are_read_as_runs() -> None:
    from pathlib import Path

    from scripts.eval.tool_calling.paths import is_run_file

    names = ["run-1.json", "run-12.json", "run-1.semantic.json", "run-1.export.json"]
    assert [is_run_file(Path(n)) for n in names] == [True, True, False, False]
