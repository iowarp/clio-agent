"""Wait receipts distinguish pending work from collected task results."""

from typing import Any

import pytest

from clio_agent.gact.agents.tool_instrumentation import instrument_tools
from clio_agent.gact.task_tools import build_task_tools, task_presentation


def test_wait_defaults_are_explicit_in_the_agent_visible_declaration() -> None:
    """Default null remains public after native schema and observer wrapping."""
    tool = next(tool for tool in build_task_tools() if tool.name == "wait_tasks")
    for candidate in (tool, instrument_tools([tool])[0]):
        declaration = candidate.format_as_litellm_function_call()["function"]
        parameters = declaration["parameters"]
        assert parameters["required"] == ["tasks"]
        assert parameters["properties"]["timeout_s"]["default"] is None
        assert parameters["properties"]["return_when"]["default"] == "all"
        assert "Default: null (no deadline)" in parameters["properties"]["timeout_s"]["description"]
        assert "timeout_s=null (no deadline)" in declaration["description"]


@pytest.mark.parametrize("timeout", [0, 2.5, None])
def test_wait_receipt_keeps_pending_work_visible(timeout: float | None) -> None:
    """An immediate poll or expired wait must not look like completed task work."""
    receipt: dict[str, Any] = {
        "results": [],
        "pending": ["task_child"],
        "errors": [],
        "timed_out": True,
    }
    shown = task_presentation({"tasks": "task_child", "timeout_s": timeout}, receipt, None)
    assert shown["action"] == ("Check all tasks" if timeout == 0 else "Wait for all tasks")
    assert shown["summary"] == "1 pending, 0 results collected"
    assert shown["subject"] == "wait-targets"
    assert shown["blocks"][0]["text"] == "task_child"
    assert shown["blocks"][2]["result_kind"] == "snapshot"
    assert shown["blocks"][2]["status"] == "pending"
    assert "Pending when this call returned" in shown["blocks"][2]["text"]


def test_mixed_wait_receipt_counts_only_collected_results() -> None:
    """A collected child and a pending child remain separate outcomes."""
    shown = task_presentation({}, {"results": [{"handle": "ready"}], "pending": ["pending"]}, None)
    assert shown["summary"] == "1 pending, 1 result collected"


def test_completed_wait_has_no_pending_snapshot() -> None:
    """A fully collected wait does not retain a running indicator."""
    shown = task_presentation({}, {"results": [{"handle": "ready"}], "pending": []}, None)
    assert shown["summary"] == "1 result collected"
    assert not any(block.get("status") == "pending" for block in shown["blocks"])
    assert shown["blocks"][2]["label"] == "ready: Result collected"


def test_any_wait_shows_selection_policy_and_each_result() -> None:
    """Any can return collected evidence while other selected tasks remain pending."""
    shown = task_presentation(
        {"tasks": ["worker_a", "worker_b"], "return_when": "any"},
        {
            "pending": ["worker_b"],
            "results": [
                {
                    "handle": "worker_a",
                    "kind": "Subagent",
                    "status": "completed",
                    "result": {"output": "Audit incomplete; loader failed."},
                }
            ],
        },
        None,
    )
    assert shown["action"] == "Wait for any task"
    assert shown["blocks"][0]["text"] == "worker_a, worker_b"
    assert "Pending tasks continue running" in shown["blocks"][1]["text"]
    assert "No deadline" not in shown["blocks"][1]["text"]
    assert shown["blocks"][2]["label"] == "worker_a: Turn ended"
    assert shown["blocks"][2]["text"] == "Audit incomplete; loader failed."
    assert shown["blocks"][3]["status"] == "pending"


def test_wait_surfaces_failed_result_and_collection_error() -> None:
    """A returned receipt must not hide task failure behind a successful call badge."""
    shown = task_presentation(
        {"tasks": ["failed", "missing"]},
        {
            "pending": [],
            "results": [
                {"handle": "failed", "status": "failed", "result": {"output": "Bad input"}}
            ],
            "errors": [{"handle": "missing", "error": "Unknown task handle"}],
        },
        None,
    )
    assert shown["status"] == "degraded"
    assert shown["summary"] == "1 result collected, 1 unsuccessful, 1 error"
    assert shown["blocks"][2]["text"] == "Bad input"
    assert shown["blocks"][3]["text"] == "Unknown task handle"
    assert shown["blocks"][3]["severity"] == "error"
