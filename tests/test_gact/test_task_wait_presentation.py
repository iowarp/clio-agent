"""Wait receipts show unfinished work in its recorded state, apart from collected results."""

import json
from typing import Any

import pytest

from clio_agent.gact.agents.tool_instrumentation import instrument_tools
from clio_agent.gact.task_declarations import build_task_tools
from clio_agent.gact.task_wait_presentation import wait_presentation


@pytest.mark.usefixtures("builtin_tool_context")
def test_wait_defaults_are_explicit_in_the_agent_visible_declaration() -> None:
    """Default null remains public after native schema and observer wrapping."""
    tool = next(tool for tool in build_task_tools() if tool.name == "wait_tasks")
    for candidate in (tool, instrument_tools([tool])[0]):
        declaration = candidate.format_as_litellm_function_call()["function"]
        parameters = declaration["parameters"]
        assert parameters["required"] == ["tasks"]
        assert parameters["properties"]["timeout_s"]["default"] is None
        assert parameters["properties"]["return_when"]["default"] == "all"
        assert parameters["properties"]["interrupt_on_mail"]["default"] is True
        assert parameters["properties"]["timeout_s"]["anyOf"] == [
            {"type": "number", "minimum": 0},
            {"type": "null"},
        ]
        description = declaration["description"]
        assert "unbounded" in description and "zero snapshots" in description
        assert "positive expiry or Stop ends only this waiter" in description


def test_mail_interrupt_receipt_explains_unfinished_work_without_claiming_completion() -> None:
    presentation = wait_presentation(
        {"tasks": ["task_child"], "return_when": "all", "timeout_s": None},
        {
            "outcomes": [],
            "unfinished": [{"handle": "task_child", "status": "running"}],
            "timed_out": False,
            "interrupted": "mail_received",
        },
    )
    assert "New input received" in presentation["summary"]
    assert presentation["status"] == "interrupted"
    assert "Interrupted by mail" in presentation["diagnostic"]
    assert "1 running" in presentation["summary"]
    assert any(
        "New input is available" in block.get("text", "") for block in presentation["blocks"]
    )


def test_stop_interrupt_is_marked_without_making_unfinished_tasks_unsuccessful() -> None:
    presentation = wait_presentation(
        {"tasks": ["task_child"], "return_when": "all"},
        {
            "outcomes": [],
            "unfinished": [{"handle": "task_child", "status": "running"}],
            "interrupted": "stopped",
        },
    )
    assert presentation["status"] == "interrupted"
    assert "stop request" in presentation["diagnostic"]
    assert "1 running" in presentation["summary"]
    assert not any(block.get("severity") == "error" for block in presentation["blocks"])


def test_wait_interruption_keeps_independent_child_failure_detail() -> None:
    presentation = wait_presentation(
        {"tasks": ["failed", "waiting"]},
        {
            "outcomes": [{"handle": "failed", "status": "failed", "result": "Bad input"}],
            "unfinished": [{"handle": "waiting", "status": "queued"}],
            "interrupted": "mail_received",
        },
    )
    assert presentation["status"] == "interrupted"
    assert "1 unsuccessful" in presentation["summary"]
    failed = next(block for block in presentation["blocks"] if block.get("severity") == "error")
    assert failed["text"] == "Bad input"


@pytest.mark.parametrize("timeout", [0, 2.5, None])
def test_wait_receipt_shows_unfinished_work_in_its_recorded_state(timeout: float | None) -> None:
    """An immediate poll or expired wait shows a running task as running, not pending."""
    receipt: dict[str, Any] = {
        "outcomes": [],
        "unfinished": [{"handle": "task_child", "status": "running", "activity": "submitting"}],
        "timed_out": True,
    }
    shown = wait_presentation({"tasks": "task_child", "timeout_s": timeout}, receipt)
    assert shown["action"] == ("Check all tasks" if timeout == 0 else "Wait for all tasks")
    assert shown["summary"] == "1 running, 0 results collected"
    assert shown["subject"] == "wait-targets"
    assert shown["blocks"][0]["text"] == "task_child"
    assert shown["blocks"][2]["label"] == "task_child: Running"
    assert shown["blocks"][2]["result_kind"] == "snapshot"
    assert shown["blocks"][2]["status"] == "running"
    assert shown["blocks"][2]["text"] == (
        "Still running when this call returned (activity: submitting)"
    )
    assert "pending" not in json.dumps(shown).lower()


def test_legacy_pending_receipt_does_not_claim_a_state() -> None:
    """A receipt recorded before ``unfinished`` names no state it did not record."""
    shown = wait_presentation({"tasks": "task_child"}, {"outcomes": [], "pending": ["task_child"]})
    assert shown["summary"] == "1 unfinished, 0 results collected"
    assert shown["blocks"][2]["status"] == "unknown"
    assert shown["blocks"][2]["text"] == "Still unfinished when this call returned"


def test_mixed_wait_receipt_counts_only_collected_results() -> None:
    """A collected child and a queued child remain separate outcomes."""
    shown = wait_presentation(
        {},
        {"results": [{"handle": "ready"}], "unfinished": [{"handle": "next", "status": "queued"}]},
    )
    assert shown["summary"] == "1 queued, 1 result collected"


def test_completed_wait_has_no_unfinished_snapshot() -> None:
    """A fully collected wait does not retain a running indicator."""
    shown = wait_presentation({}, {"results": [{"handle": "ready"}], "unfinished": []})
    assert shown["summary"] == "1 result collected"
    assert not any(block.get("status") == "pending" for block in shown["blocks"])
    assert shown["blocks"][2]["label"] == "ready: Result collected"


def test_any_wait_shows_selection_policy_and_each_result() -> None:
    """Any can return collected evidence while other selected tasks keep running."""
    shown = wait_presentation(
        {"tasks": ["worker_a", "worker_b"], "return_when": "any"},
        {
            "unfinished": [{"handle": "worker_b", "status": "running"}],
            "results": [
                {
                    "handle": "worker_a",
                    "kind": "Subagent",
                    "status": "completed",
                    "result": {"output": "Audit incomplete; loader failed."},
                }
            ],
        },
    )
    assert shown["action"] == "Wait for any task"
    assert shown["blocks"][0]["text"] == "worker_a, worker_b"
    assert "Unfinished tasks continue" in shown["blocks"][1]["text"]
    assert "No deadline" not in shown["blocks"][1]["text"]
    assert shown["blocks"][2]["label"] == "worker_a: Assignment complete"
    assert shown["blocks"][2]["text"] == "Audit incomplete; loader failed."
    assert shown["blocks"][3]["status"] == "running"


def test_a_child_agents_report_is_its_answer_not_its_run() -> None:
    """A child's result is its run record; its tab shows the answer, never the JSON.

    SABOTAGE: drop the ``answer`` branch in ``_result_text`` -> the run's JSON -> red."""
    run = {
        "selected_expert": "explore",
        "route_source": "flow:react",
        "answer": "The folder holds one file, notes.txt.",
        "tools_called": [{"name": "fs_list_directory", "ok": True}],
    }
    shown = wait_presentation(
        {"tasks": ["child_a"]},
        {
            "outcomes": [
                {"handle": "child_a", "kind": "Session", "status": "completed", "result": run}
            ]
        },
    )
    report = shown["blocks"][2]
    assert report["text"] == "The folder holds one file, notes.txt."
    assert "selected_expert" not in report["text"]


def test_wait_surfaces_failed_result_and_collection_error() -> None:
    """A returned receipt must not hide task failure behind a successful call badge."""
    shown = wait_presentation(
        {"tasks": ["failed", "missing"]},
        {
            "unfinished": [],
            "results": [
                {"handle": "failed", "status": "failed", "result": {"output": "Bad input"}}
            ],
            "errors": [{"handle": "missing", "error": "Unknown task handle"}],
        },
    )
    assert shown["status"] == "degraded"
    assert shown["summary"] == "1 result collected, 1 unsuccessful, 1 error"
    assert shown["blocks"][2]["text"] == "Bad input"
    assert shown["blocks"][3]["text"] == "Unknown task handle"
    assert shown["blocks"][3]["severity"] == "error"


def test_each_child_report_names_the_child_and_its_model(monkeypatch) -> None:
    """A wait's per-child block carries the task's label and the model its session ran on,
    so the UI can show one named tab per child."""
    from types import SimpleNamespace

    from clio_agent.gact import task_wait_presentation as presentation

    task = SimpleNamespace(label="explore arc", backend=SimpleNamespace(session="sess_child"))
    child = SimpleNamespace(model={"provider_id": "claude_code", "model_id": "claude-haiku-5-5"})
    app = SimpleNamespace(
        state=SimpleNamespace(
            task_history=SimpleNamespace(task_snapshot=lambda handle: task),
            sessions=SimpleNamespace(get=lambda sid: child if sid == "sess_child" else None),
        )
    )
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    assert presentation._child("task_a") == {"title": "explore arc", "model": "claude-haiku-5-5"}
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)
    assert presentation._child("task_a") == {"title": "task_a"}
