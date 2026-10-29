"""Task cards consume current public operation results and declared tool metadata."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    CoordinationGrant,
    TaskOwner,
    TaskProgress,
    TaskSnapshot,
)
from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.agents.tool_instrumentation import (
    PRESENTER_ATTR,
    START_PRESENTER_ATTR,
    TITLE_ATTR,
)
from clio_agent.gact.flow_tools import build_run_flow_tool, build_run_workflow_tool
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_declarations import build_task_compatibility_tools, build_task_tools
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.task_wait_presentation import wait_start_presentation

STAMP = "2026-10-17T12:00:00Z"


async def unexpected_cancel(task: TaskSnapshot, caller: TaskCaller) -> dict[str, Any]:
    raise AssertionError("Presentation must not cancel or drive work")


def _record_tasks(history: TaskHistory) -> None:
    for handle, session, parent in (
        ("task_parent", "parent", None),
        ("task_peer", "peer", None),
        ("task_child", "child", "task_parent"),
    ):
        history.record_snapshot(
            TaskSnapshot(
                handle=handle,
                kind="Subagent" if parent else "Session",
                owner=TaskOwner(
                    human_id="human",
                    session_id=session,
                    assignment_id=handle,
                    parent_handle=parent,
                ),
                label="Researcher" if parent else session,
                description="Inspect the evidence",
                description_missing=False,
                invocation_id=handle,
                tool="conversation",
                created_at=STAMP,
                updated_at=STAMP,
                raw_status="active",
                status="active",
                activity="busy" if parent else "idle",
            ),
            observation_id="record:" + handle,
        )
    reference = history.write_output(
        "task_child",
        {
            "chunk_id": "progress-one",
            "output": {"text": "Reading evidence", "tools": []},
            "previous": None,
        },
    )
    child = history.task_snapshot("task_child")
    assert child is not None
    history.record_snapshot(
        child.model_copy(update={"progress": TaskProgress(output=reference)}),
        observation_id="progress:task_child",
    )
    history.record_grant(
        CoordinationGrant(
            id="observe-only",
            granting_human="human",
            recipient_session="peer",
            target_session="child",
            rights=("observe",),
            created_at=STAMP,
        ),
        observation_id="grant:observe-only",
    )


@pytest_asyncio.fixture
async def recorded_tasks(arc: ARCMemory) -> AsyncIterator[tuple[TaskHistory, TaskOperations]]:
    history = TaskHistory(arc)
    try:
        await history.io.run(_record_tasks, history)
        yield history, TaskOperations(history, unexpected_cancel)
    finally:
        await history.io.close()


def test_task_tool_declarations_select_their_shared_presenters() -> None:
    aliases = ["wait_agent_tasks", "get_agent_task_output", "message_agent"]
    tools = build_task_tools() + build_task_compatibility_tools(aliases)
    expected = {
        "wait_tasks": "wait",
        "wait_agent_tasks": "wait",
        "get_task_result": "task_output",
        "get_agent_task_output": "task_output",
        "send_agent_message": "message",
        "message_agent": "message",
        "query_tasks": "tasks",
        "observe_tasks": "tasks",
        "cancel_tasks": "tasks",
    }
    selected = {tool.name: tool for tool in tools if tool.name in expected}
    assert set(selected) == set(expected)
    for name, presentation in expected.items():
        assert getattr(selected[name].func, PRESENTER_ATTR) == presentation
        start = getattr(selected[name].func, START_PRESENTER_ATTR)
        assert start is (wait_start_presentation if presentation == "wait" else None)


def test_task_controls_keep_the_declared_operation_title() -> None:
    aliases = ["wait_agent_tasks", "observe_agent_tasks", "get_agent_task_output", "message_agent"]
    tools = build_task_tools() + build_task_compatibility_tools(aliases)
    expected = {
        "query_tasks": "Query tasks",
        "observe_tasks": "Observe tasks",
        "wait_tasks": "Wait tasks",
        "cancel_tasks": "Cancel tasks",
        "get_task_result": "Get task result",
        "send_agent_message": "Send agent message",
        "pause_agent": "Pause agent",
        "resume_agent": "Resume agent",
        "restart_agent": "Restart agent",
        "wait_agent_tasks": "Wait agent tasks",
        "observe_agent_tasks": "Observe agent tasks",
        "get_agent_task_output": "Get agent task output",
        "message_agent": "Message agent",
    }
    selected = {tool.name: tool for tool in tools if tool.name in expected}

    assert {name: getattr(tool.func, TITLE_ATTR) for name, tool in selected.items()} == expected
    for name in expected:
        declaration = getattr(selected[name].func, PRESENTER_ATTR)
        if declaration != "wait":
            view = native_presentation(declaration, {}, {"tasks": []}, None)
            assert "action" not in view


def test_restart_receipt_says_requested_and_preserves_task_handle() -> None:
    handle = "task_original_handle"
    view = native_presentation(
        "tasks",
        {},
        {"handle": handle, "pending": True, "restart_requested": True},
        None,
    )

    assert view["summary"] == "Restart requested; waiting for task restart"
    assert view["blocks"] == [
        {
            "id": "task-restart",
            "type": "text",
            "label": "Task restart",
            "text": "Restart requested; waiting for task restart",
            "status": "pending",
            "result_kind": "snapshot",
            "detail": f"Task handle: {handle}",
        }
    ]


def test_restart_without_explicit_pending_receipt_uses_ordinary_task_presentation() -> None:
    view = native_presentation(
        "tasks",
        {"_operation": "restart"},
        {"handle": "task_original_handle", "pending": True},
        None,
    )

    assert view["summary"] == "1 task"
    assert "status" not in view


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", [None, "evidence"])
async def test_real_observation_chunks_are_visible_without_changing_the_read(
    recorded_tasks: tuple[TaskHistory, TaskOperations], pattern: str | None
) -> None:
    _, operations = recorded_tasks
    report = await operations.observe(
        TaskCaller("parent", "human"), "task_child", pattern=pattern, timeout_s=0
    )
    before = json.dumps(report, sort_keys=True)
    view = native_presentation("tasks", {"tasks": "task_child", "pattern": pattern}, report, None)
    links = [block for block in view["blocks"] if block["type"] == "link"]
    assert links and links[0]["uri"] == "child" and links[0]["label"] == "Researcher"
    progress = [block for block in view["blocks"] if block.get("label") == "New output"]
    assert len(progress) == 1 and "evidence" in progress[0]["text"]
    assert "chunk_id" not in progress[0]["text"]
    assert "tools" not in progress[0]["text"]
    assert report["cursor"] and json.dumps(report, sort_keys=True) == before


@pytest.mark.asyncio
async def test_observe_only_grants_do_not_become_readable_through_presentation(
    recorded_tasks: tuple[TaskHistory, TaskOperations],
) -> None:
    _, operations = recorded_tasks
    report = await operations.observe(TaskCaller("peer", "human"), "task_child", timeout_s=0)
    before = json.dumps(report, sort_keys=True)
    view = native_presentation("tasks", {"tasks": "task_child"}, report, None)
    shown = json.dumps(view)
    assert "Reading evidence" not in shown and "Inspect the evidence" not in shown
    assert "Researcher" not in shown
    assert all(block["type"] != "link" for block in view["blocks"])
    assert json.dumps(report, sort_keys=True) == before


@pytest.mark.asyncio
async def test_query_references_and_explicit_results_have_distinct_presentation(
    recorded_tasks: tuple[TaskHistory, TaskOperations],
) -> None:
    history, operations = recorded_tasks
    child = await history.io.run(history.task_snapshot, "task_child")
    assert child is not None
    result = await history.io.run(history.write_content, "child", "Retained final answer")
    await history.io.run(
        history.record_snapshot,
        child.model_copy(
            update={
                "status": "completed",
                "activity": "",
                "terminal_at": STAMP,
                "result": result,
            }
        ),
        observation_id="terminal:task_child",
    )
    caller = TaskCaller("parent", "human")
    query = await history.io.run(operations.query, caller, handle="task_child")
    query_before = json.dumps(query, sort_keys=True)
    snapshot = native_presentation("tasks", {}, query, None)
    assert "Retained final answer" not in json.dumps(snapshot)
    assert any("get_task_result" in block.get("text", "") for block in snapshot["blocks"])
    assert json.dumps(query, sort_keys=True) == query_before
    collected = await history.io.run(operations.result, caller, "task_child")
    before = json.dumps(collected, sort_keys=True)
    view = native_presentation("task_output", {}, collected, None)
    assert "Retained final answer" in json.dumps(view)
    assert json.dumps(collected, sort_keys=True) == before


def test_message_receipt_contains_sent_text_and_the_actual_recipient() -> None:
    report = {"accepted": True, "session_id": "child", "message_id": "mail-one"}
    before = dict(report)
    view = native_presentation(
        "message", {"target": "child", "message": "Review this evidence"}, report, None
    )
    assert "action" not in view
    assert sum(block.get("text") == "Review this evidence" for block in view["blocks"]) == 1
    assert any(block.get("uri") == "child" for block in view["blocks"])
    assert report == before


def test_task_cards_use_authorized_task_identity_once_and_keep_handles_secondary() -> None:
    report = {
        "tasks": [
            {
                "handle": "task_opaque_123",
                "kind": "Subagent",
                "label": "Evidence review",
                "description": "Compare the returned source passages",
                "status": "running",
                "activity": "busy",
                "owner": {"session_id": "session_authorized"},
            }
        ]
    }
    before = json.dumps(report, sort_keys=True)

    view = native_presentation("tasks", {}, report, None)

    assert "action" not in view  # keep the declared Query/Observe/Cancel title
    assert view["subject"] == "task-0-identity"
    identity, assignment, status = view["blocks"]
    assert identity["type"] == "link"
    assert identity["label"] == "Evidence review"
    assert identity["uri"] == "session_authorized"
    assert identity["detail"] == "task_opaque_123"
    assert assignment["label"] == "Assignment"
    assert assignment["text"] == "Compare the returned source passages"
    assert status["label"] == "Status"
    assert status["text"] == "busy"
    assert "task_opaque_123" not in assignment["label"] + status["label"]
    assert json.dumps(report, sort_keys=True) == before


def test_task_result_keeps_multiple_identities_separate_without_header_subject() -> None:
    report = {
        "tasks": [
            {
                "handle": "task_one",
                "kind": "Subagent",
                "label": "Source review",
                "description": "Read the manual",
                "status": "completed",
                "owner": {"session_id": "session_one"},
                "result": "The source defines a chunked layout.",
            },
            {
                "handle": "task_two",
                "kind": "Subagent",
                "label": "Version check",
                "description": "Check the version note",
                "status": "failed",
                "owner": {"session_id": "session_two"},
                "result": "Access denied",
                "error": "Access denied",
            },
        ]
    }

    view = native_presentation("task_output", {}, report, None)

    assert "subject" not in view
    assert [block["id"] for block in view["blocks"] if block["id"].endswith("-identity")] == [
        "task-0-identity",
        "task-1-identity",
    ]
    assert [block["label"] for block in view["blocks"] if block["id"].endswith("-assignment")] == [
        "Assignment",
        "Assignment",
    ]
    assert [block["label"] for block in view["blocks"] if block["id"].endswith("-status")] == [
        "Status",
        "Status",
    ]
    assert "The source defines a chunked layout." in json.dumps(view)
    assert "Access denied" in json.dumps(view)
    assert [block["severity"] for block in view["blocks"] if block["id"] == "task-1-status"] == [
        "error"
    ]


def test_accepted_background_submission_uses_assignment_as_subject() -> None:
    report = {
        "accepted": True,
        "handle": "task_download_456",
        "kind": "Download",
        "description": "Download archive manifest",
    }
    view = native_presentation("tasks", {}, report, None)

    assert view["subject"] == "task-submission"
    assert "action" not in view
    assert view["summary"] == "Download requested"
    assert view["blocks"] == [
        {
            "id": "task-submission",
            "type": "text",
            "label": "Download archive manifest",
            "text": "Download",
            "detail": "Task handle: task_download_456",
            "status": "pending",
            "result_kind": "snapshot",
        }
    ]


def test_accepted_flow_uses_the_explicit_flow_id_for_its_session_subject() -> None:
    report = {
        "accepted": True,
        "handle": "task_flow_789",
        "kind": "Subagent",
        "session_id": "session_flow",
        "flow": "deep-researcher",
    }

    view = native_presentation("tasks", {"flow": "ignored-argument"}, report, None)

    assert view["summary"] == "Flow accepted"
    assert view["subject"] == "task-0-identity"
    identity = view["blocks"][0]
    assert identity["type"] == "link"
    assert identity["target"] == "session"
    assert identity["uri"] == "session_flow"
    assert identity["label"] == "Deep researcher"
    assert identity["detail"] == "task_flow_789"


def test_flow_start_tools_keep_their_declared_titles() -> None:
    tools = [build_run_flow_tool(), build_run_workflow_tool()]

    assert {tool.name: getattr(tool.func, TITLE_ATTR) for tool in tools} == {
        "run_flow": "Run flow",
        "run_workflow": "Run workflow",
    }
    assert "action" not in native_presentation("tasks", {}, {"tasks": []}, None)
