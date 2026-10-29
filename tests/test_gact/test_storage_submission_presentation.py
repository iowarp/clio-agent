"""Source-task acceptance is not an unavailable inventory or a completed transfer."""

from functools import partial
from typing import Any

import pytest

from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.storage.setup_tool import setup_presentation
from clio_agent.gact.tool_result_presentation import ToolPresentation


@pytest.mark.parametrize("kind", ["Indexing", "Download"])
@pytest.mark.parametrize(
    ("status", "summary", "result_kind"),
    [
        ("running", "requested", "snapshot"),
        ("queued", "requested", "snapshot"),
        ("completed", "completed", "completion"),
        ("failed", "unsuccessful", "completion"),
    ],
)
def test_submission_has_a_truthful_typed_receipt(
    kind: str, status: str, summary: str, result_kind: str
) -> None:
    """Both native source presenters recognize the same recorded acceptance shape."""
    result: dict[str, Any] = {
        "accepted": True,
        "handle": "task_index",
        "kind": kind,
        "description": "Index the selected scientific resources",
        "status": status,
        "connection": {"source_id": "source_1", "state": "indexing"},
    }
    for presenter in (setup_presentation, partial(native_presentation, "tasks")):
        for raw, structured in ((result, None), (None, result)):
            receipt = ToolPresentation.model_validate(presenter({}, raw, structured))
            assert receipt.summary == f"{kind} {summary}"
            assert receipt.blocks[0].text == kind
            assert receipt.blocks[0].detail == "Task handle: task_index"
            assert receipt.blocks[0].result_kind == result_kind
            if status == "failed":
                assert receipt.status == "degraded"
                assert receipt.blocks[0].severity == "error"
            elif status != "completed":
                assert receipt.blocks[0].status == "pending"


@pytest.mark.parametrize("result", [{}, {"accepted": True}, {"accepted": True, "handle": ""}])
def test_missing_task_identity_does_not_claim_an_acceptance(result: dict[str, Any]) -> None:
    """An incomplete return does not become a fake task handle."""
    presentation = native_presentation("tasks", {}, result, None)
    assert presentation["summary"] == "1 task"
    assert all(block["id"] != "task-submission" for block in presentation["blocks"])


def test_spawn_and_question_receipts_keep_their_shared_presentation() -> None:
    """Storage acceptance rendering cannot remove child navigation or Question state."""
    spawn = native_presentation(
        "tasks",
        {},
        {"accepted": True, "handle": "task_child", "kind": "Subagent", "session_id": "child"},
        None,
    )
    assert any(
        block.get("target") == "session" and block["uri"] == "child" for block in spawn["blocks"]
    )
    question = native_presentation(
        "tasks",
        {},
        {
            "accepted": True,
            "handle": "task_question",
            "kind": "Question",
            "status": "input_required",
        },
        None,
    )
    assert any(block.get("label") == "Question" for block in question["blocks"])
    assert any(
        block.get("label") == "Status" and block.get("text") == "input_required"
        for block in question["blocks"]
    )
    assert all(
        block["id"] != "task-submission" for block in (*spawn["blocks"], *question["blocks"])
    )


@pytest.mark.parametrize("kind", ["Download", "Indexing"])
def test_recording_uncertainty_keeps_its_error_without_claiming_completion(kind: str) -> None:
    """A retained handle with uncertain recording remains distinguishable from normal acceptance."""
    result = {
        "accepted": True,
        "handle": "task_uncertain",
        "kind": kind,
        "acceptance": "accepted_recording_unconfirmed",
        "status": "task_state_uncertain",
        "error": "Accepted work has unconfirmed recording; inspect its handle before retrying.",
    }
    for presenter in (setup_presentation, partial(native_presentation, "tasks")):
        shown = presenter({}, result, None)
        assert shown["status"] == "failed"
        assert any(
            block.get("severity") == "error" and result["error"] in block.get("text", "")
            for block in shown["blocks"]
        )
        assert all(block.get("result_kind") != "completion" for block in shown["blocks"])
        assert "requested" not in shown["summary"]
