"""Canonical mixed waits disclose collection, attention and independent member errors."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.task_wait_presentation import wait_presentation


@pytest.mark.parametrize("encoded", [False, True])
def test_native_wait_keeps_subject_and_honest_mixed_collection_counts(encoded: bool) -> None:
    payload: dict[str, Any] = {
        "outcomes": [
            {"handle": "child", "kind": "Subagent", "status": "completed", "result": "Analysis"},
            {"handle": "lost", "status": "task_state_uncertain", "reason": "Backend disconnected"},
            {"handle": "denied", "error": "read_denied"},
        ],
        "unfinished": [
            {"handle": "lost", "status": "running"},
            {"handle": "working", "status": "running", "activity": "executing"},
        ],
        "timed_out": True,
    }
    original = copy.deepcopy(payload)
    result = json.dumps(payload) if encoded else payload
    view = native_presentation(
        "wait", {"tasks": ["child", "lost", "working", "denied"], "timeout_s": 1}, result, None
    )
    assert view["subject"] == "wait-targets"
    assert view["status"] == "degraded"
    assert view["summary"] == "1 running, 1 result collected, 1 task needs attention, 1 error"
    assert view["blocks"][1]["text"].endswith("The deadline expired; unfinished tasks continue.")
    child, lost, working, denied = view["blocks"][2:]
    assert child["label"] == "child: Assignment complete"
    assert child["result_kind"] == "completion" and child["text"] == "Analysis"
    assert lost["label"] == "lost: Needs attention" and lost["severity"] == "warning"
    assert lost["result_kind"] == "snapshot" and lost["text"] == "Backend disconnected"
    assert working["status"] == "running" and working["result_kind"] == "snapshot"
    assert working["text"] == "Still running when this call returned (activity: executing)"
    assert denied["severity"] == "error" and denied["text"] == "read_denied"
    assert payload == original


def test_legacy_retained_wait_results_count_snapshots_by_their_status() -> None:
    view = wait_presentation(
        {"tasks": ["done", "running"], "timeout_s": 0},
        {
            "results": [
                {"handle": "done", "status": "completed", "result": "Saved result"},
                {"handle": "running", "status": "running", "pending": True},
            ],
            "pending": ["running"],
            "errors": [],
        },
    )
    assert view["summary"] == "1 running, 1 result collected"
    assert "status" not in view
    assert view["blocks"][1]["text"].startswith("Immediate status check")
    assert view["blocks"][-1]["result_kind"] == "snapshot"
