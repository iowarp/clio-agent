"""Task settlement cannot invent success for a missing or failed flow checkpoint."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.task_flow_summary import task_flow_summary


class History:
    """A tiny retained-content fixture; no execution or prose matching."""

    def __init__(self, status: str | None, *, predecessor: str = "") -> None:
        self.status = status
        self.predecessor = predecessor
        self.result: Any = {"message": "rejected"}

    def workflow_checkpoint(self, handle: str) -> Any:
        return SimpleNamespace(content="run") if self.status is not None else None

    def read_content(self, reference: Any) -> Any:
        return (
            {"flow": "deep-research", "status": self.status, "result": self.result}
            if reference == "run"
            else {"predecessor": self.predecessor}
        )

    def task_snapshot(self, handle: str) -> Any:
        return SimpleNamespace(details="assignment")


@pytest.mark.parametrize("status", ["flow_error", "flow_stall", "completed"])
def test_flow_summary_keeps_its_actual_checkpoint_status(status: str) -> None:
    summary = task_flow_summary(
        History(status), {"handle": "t", "status": "completed"}, "deep-research"
    )
    assert summary["status"] == status


def test_completed_followup_is_not_a_new_run_and_restart_is_not_guessed_complete() -> None:
    history = History(None, predecessor="old")
    task = {"handle": "t", "status": "completed", "invocation_id": "mail:later"}
    assert task_flow_summary(history, task, "deep-research") == {
        "id": "deep-research",
        "status": "not_run",
        "previous_handle": "old",
    }
    assert (
        task_flow_summary(history, {**task, "invocation_id": "restart:old"}, "deep-research")[
            "status"
        ]
        == "unknown"
    )
    assert task_flow_summary(History(None), task, "deep-research")["status"] == "unknown"


def test_string_failure_is_normalized_for_the_public_flow_summary() -> None:
    history = History("flow_error")
    history.result = "recorded failure"
    summary = task_flow_summary(history, {"handle": "t", "status": "failed"}, "deep-research")
    assert summary["outcome"] == {"reason": "flow_error", "message": "recorded failure"}
