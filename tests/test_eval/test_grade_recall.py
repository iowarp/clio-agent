"""L1R's recall answers are read against the session's own earlier replies."""

from __future__ import annotations

from typing import Any

from scripts.eval.tool_calling.grade_recall import grade_recall

FIRST = "The 09-02 snapshot has 7 modality folders; swir has only 2 files of 82."
PEAK = "Mean QY_max peaks at 200. | 0 | 0.7491 | | 50 | 0.7606 | | 200 | 0.7965 |"


def _run(snapshot_answer: str, means_answer: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    first_q = (
        'You wrote: "This folder holds the export of OPAL experiment 67 ... Explore this '
        'export and tell me what it contains and how it is shaped."'
    )
    replies = [FIRST, "qc", PEAK, *["x"] * 7, first_q, snapshot_answer, means_answer]
    return {"replies": replies, "sessions": [{"tool_calls": calls}]}


def test_an_exact_recall_scores_full_and_reports_the_lookup() -> None:
    """SABOTAGE: compare against the latest reply instead of the first and the
    snapshot numbers read as never said."""
    run = _run(
        "I said swir has only 2 files of 82.",
        "0: 0.7491, 50: 0.7606, 200: 0.7965",
        [{"tool": "recall_context", "args": {"query": "second snapshot"}}],
    )
    graded = grade_recall(run)
    assert graded["first_message"] == {"markers_quoted": 2, "of": 2}
    assert graded["first_reply_snapshot"]["numbers_precision"] == 1.0
    assert graded["means_before_change"]["numbers_recall"] == 1.0
    assert graded["recall_context_calls"] == 1


def test_numbers_never_said_are_named() -> None:
    """The confident wrong answer: means from the redo after the change of plan."""
    run = _run("swir was fine.", "0: 0.7575, 50: 0.7697, 200: 0.7949", [])
    means = grade_recall(run)["means_before_change"]
    assert means["numbers_precision"] == 0.0
    assert "0.7575" in means["numbers_not_said"]
    assert grade_recall(run)["recall_context_calls"] == 0


def test_a_table_given_again_at_more_decimals_is_the_same_claim() -> None:
    """SABOTAGE: compare numbers as strings only and 0.74912 reads as never said."""
    run = _run("swir was fine.", "0: 0.74912, 50: 0.761, 200: 0.79649", [])
    means = grade_recall(run)["means_before_change"]
    assert means["numbers_precision"] == 1.0
    assert means["numbers_recall"] == 1.0
    assert grade_recall(_run("x", "0: 0.7481", []))["means_before_change"]["numbers_not_said"] == [
        "0.7481"
    ]
