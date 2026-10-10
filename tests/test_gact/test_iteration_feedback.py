"""Completion and feedback belong to iteration boundaries, not ordinary updates."""

from __future__ import annotations

from typing import Any

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.messaging import reserved_metadata_keys
from clio_agent.gact.protocol.v3.message import message_to_v3
from tests._scripted_engine import Reply, calls, scripted_lm, wire
from tests.test_gact.test_context_projection import _plane


@pytest.mark.parametrize("status", [None, "running", "pending", "completed"])
def test_an_update_timestamp_is_not_a_completion_signal(status: str | None) -> None:
    record: dict[str, Any] = {
        "id": "answer",
        "role": "assistant",
        "session_id": "s",
        "created_at": "2026-10-10T01:12:00Z",
        "updated_at": "2026-10-10T01:13:00Z",
        "metadata": {"status": status},
        "parts": [],
    }
    assert "completed_at" not in message_to_v3(record)
    record["stop_reason"] = "end_turn"
    assert message_to_v3(record)["completed_at"] == record["updated_at"]


def test_public_feedback_receipt_exposes_only_its_recorded_boundary() -> None:
    receipt = {"assistant_message_id": "assistant", "after_part_id": "call-part"}
    row = message_to_v3(
        {
            "id": "feedback",
            "session_id": "s",
            "role": "user",
            "parts": [],
            "metadata": {"mid_turn_steer": True, "steer_delivery": receipt, "private": "secret"},
        }
    )
    assert row["metadata"] == {"steer_delivery": receipt}
    assert reserved_metadata_keys({"steer_delivery": receipt}) == ["steer_delivery"]


@pytest.mark.parametrize("finish", ["direct_response", "submit"])
def test_arrivals_during_the_final_model_call_are_considered_before_completion(
    tmp_path: Any,
    finish: str,
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arrivals = [[], [("steer", "use the new scope"), ("task_results", "child done")]]

    def drain() -> list[tuple[str, str]]:
        return arrivals.pop(0) if arrivals else []

    first = (
        Reply(text="old answer")
        if finish == "direct_response"
        else calls(("submit", {"answer": "old answer"}))
    )
    lm, engine = scripted_lm([first, Reply(text="updated answer")])
    with _plane(arc, pending_loop_inbox_drain=drain), dspy.context(lm=lm):
        prediction = ClioReAct("question -> answer", tools=[])(question="initial scope")
    assert len(engine.requests) == 2
    assert prediction.answer == "updated answer"
    assert wire(engine.requests[1])[-2:] == [
        ("user", [("text", "use the new scope")]),
        ("user", [("text", "[clio: task_results]\nchild done")]),
    ]


def test_an_explicit_iteration_cap_leaves_late_feedback_pending(tmp_path: Any) -> None:
    """A cap must not consume feedback that this forward cannot process."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    drains: list[bool] = []

    def drain() -> list[tuple[str, str]]:
        drains.append(True)
        return [] if len(drains) == 1 else [("steer", "late feedback")]

    lm, engine = scripted_lm([Reply(text="answer")])
    with _plane(arc, pending_loop_inbox_drain=drain), dspy.context(lm=lm):
        ClioReAct("question -> answer", tools=[], max_iters=1)(question="scope")
    assert len(engine.requests) == 1
    assert len(drains) == 1
