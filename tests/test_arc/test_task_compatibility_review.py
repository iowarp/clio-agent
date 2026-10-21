"""Regression checks for the independently reviewed public compatibility boundaries."""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import AssignmentDetails, TaskSnapshot
from clio_agent.gact import context
from clio_agent.gact.agents import clio_react_record
from clio_agent.gact.assignment_evidence import select_assignment_evidence
from clio_agent.gact.builtin_boundary import BuiltinBoundary
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_declarations import build_spawn_runtime_tools
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_assignment_evidence_contracts import evidence_children
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_observation_compatibility import (
    append_output,
    forbidden_cancel,
    running_shell,
)


@pytest.mark.asyncio
async def test_first_assignment_pins_evidence_before_an_interrupted_acceptance(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, owner, source, _critic, _body = await evidence_children(arc, tmp_path)
    history = owner.history
    parent_handle = source.owner.parent_handle
    assert parent_handle is not None
    parent = await asyncio.to_thread(history.task_snapshot, parent_handle)
    assert parent is not None
    caller = TaskCaller(parent.owner.session_id, "human")
    pins = await asyncio.to_thread(select_assignment_evidence, history, caller, [source.handle])
    original = history.record_snapshot
    recorded: list[str] = []

    def interrupt(snapshot: TaskSnapshot, *, observation_id: str) -> None:
        original(snapshot, observation_id=observation_id)
        if snapshot.description == "Resilient assignment" and not recorded:
            recorded.append(snapshot.handle)
            assert snapshot.details is not None
            details = AssignmentDetails.model_validate(history.read_content(snapshot.details))
            assert details.input_evidence == pins and details.evidence_assignment == snapshot.handle
            raise RuntimeError("Lost reply after the first confirmed assignment observation")

    monkeypatch.setattr(history, "record_snapshot", interrupt)
    before = len(app.state.sessions.list())
    arguments: dict[str, Any] = {
        "launch_context": {"input_task_refs": [pin.model_dump(mode="json") for pin in pins]}
    }
    with pytest.raises(RuntimeError, match="Lost reply"):
        await owner.spawn(caller, "Resilient assignment", "critic", "resilient", **arguments)
    accepted = await owner.spawn(caller, "Resilient assignment", "critic", "resilient", **arguments)
    assert accepted["accepted"] is True and accepted["handle"] == recorded[0]
    assert len(app.state.sessions.list()) == before + 1
    pending = await asyncio.to_thread(history.pending_mail, accepted["session_id"])
    assert [item.id for item in pending] == ["assignment:" + accepted["handle"]]
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_new_earlier_output_cannot_starve_an_omitted_observation_member(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    _app, owner, root = await create_environment(arc, tmp_path)
    first = await asyncio.to_thread(running_shell, owner.history, root, "first")
    second = await asyncio.to_thread(running_shell, owner.history, root, "second")
    first = await asyncio.to_thread(append_output, owner.history, first, "first output")
    second = await asyncio.to_thread(append_output, owner.history, second, "READY")
    operations, caller = (
        TaskOperations(owner.history, forbidden_cancel),
        TaskCaller(root.owner.session_id, "human"),
    )
    page = await operations.observe(caller, [first.handle, second.handle], row_limit=1)
    assert page["tasks"][0]["handle"] == first.handle and page["has_more"] is True
    first = await asyncio.to_thread(append_output, owner.history, first, "new first output")
    next_page = await operations.observe(
        caller, [first.handle, second.handle], page["cursor"], row_limit=1
    )
    assert next_page["tasks"][0]["handle"] == second.handle
    assert next_page["tasks"][0]["output"][0]["output"] == "READY"
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_pattern_matches_logical_output_across_a_remaining_page_budget(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, owner, root = await create_environment(arc, tmp_path)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "7")
    first = await asyncio.to_thread(running_shell, owner.history, root, "first")
    second = await asyncio.to_thread(running_shell, owner.history, root, "second")
    first = await asyncio.to_thread(append_output, owner.history, first, "12345")
    second = await asyncio.to_thread(append_output, owner.history, second, "prefix READY suffix")
    operations, caller = (
        TaskOperations(owner.history, forbidden_cancel),
        TaskCaller(root.owner.session_id, "human"),
    )
    result = await operations.observe(
        caller, [first.handle, second.handle], pattern="READY", timeout_s=1
    )
    assert result["timed_out"] is False
    chunk = next(row for row in result["tasks"] if row["handle"] == second.handle)["output"][0]
    assert chunk["pattern_match"] == {"start": 7, "end": 12, "excerpt_truncated": True}
    assert chunk["output"] == "RE"
    assert (
        sum(len(chunk["output"]) for row in result["tasks"] for chunk in row.get("output", [])) <= 7
    )
    assert "search_start_cursor" in result
    resumed = await operations.observe(
        caller,
        [first.handle, second.handle],
        result["cursor"],
        pattern="READY",
        timeout_s=0,
    )
    assert resumed["timed_out"] is True
    assert not any(
        "pattern_match" in chunk for row in resumed["tasks"] for chunk in row.get("output", [])
    )
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_truncated_match_continuation_keeps_budget_and_does_not_repeat_the_match(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, owner, root = await create_environment(arc, tmp_path)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "7")
    task = await asyncio.to_thread(running_shell, owner.history, root, "shell")
    task = await asyncio.to_thread(append_output, owner.history, task, "BBB")
    task = await asyncio.to_thread(append_output, owner.history, task, "A" * 9)
    operations, caller = (
        TaskOperations(owner.history, forbidden_cancel),
        TaskCaller(root.owner.session_id, "human"),
    )
    first = await operations.observe(caller, task.handle, pattern="A+", timeout_s=0)
    assert first["tasks"][0]["output"][0]["output"] == "A" * 7
    assert first["tasks"][0]["output"][0]["pattern_match"]["excerpt_truncated"] is True
    second = await operations.observe(
        caller, task.handle, first["cursor"], pattern="A+", timeout_s=0
    )
    assert second["timed_out"] is True
    assert [chunk["output"] for row in second["tasks"] for chunk in row.get("output", [])] == [
        "BBB"
    ]
    for page in (first, second):
        assert (
            sum(len(chunk["output"]) for row in page["tasks"] for chunk in row.get("output", []))
            <= 7
        )
    original = await operations.observe(caller, task.handle)
    assert original["tasks"][0]["output"][0]["output"] == "A" * 7
    await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_s", [0.0, 0.03])
async def test_expired_pattern_wait_retains_its_search_recovery_metadata(
    arc: ARCMemory,
    tmp_path: Path,
    timeout_s: float,
) -> None:
    _app, owner, root = await create_environment(arc, tmp_path)
    try:
        task = await asyncio.to_thread(running_shell, owner.history, root, "shell")
        task = await asyncio.to_thread(append_output, owner.history, task, "no match")
        operations = TaskOperations(owner.history, forbidden_cancel)
        caller = TaskCaller(root.owner.session_id, "human")
        result = await operations.observe(caller, task.handle, pattern="READY", timeout_s=timeout_s)
        assert result["timed_out"] is True
        # If the initial Core read exhausts the wait, its available observations
        # are returned. Otherwise the timeout snapshot may have no new rows.
        assert all(row["handle"] == task.handle for row in result["tasks"])
        assert not any(
            "pattern_match" in chunk for row in result["tasks"] for chunk in row.get("output", [])
        )
        if timeout_s == 0:
            assert result["tasks"][0]["output"][0]["output"] == "no match"
        assert result["search_start_cursor"] is None and result["scanned_pages"] == 0
        recovered = await operations.observe(
            caller, task.handle, cursor=result["search_start_cursor"]
        )
        assert recovered["tasks"][0]["output"][0]["output"] == "no match"
        pending = await asyncio.to_thread(owner.history.task_snapshot, task.handle)
        assert pending is not None and pending.terminal_at is None
        assert not owner.history.acknowledged_notifications(root.owner.session_id)
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_public_fastmcp_fanout_keeps_failed_positions(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    app.state.mcp_app_loop = asyncio.get_running_loop()
    app.state.workspaces = SimpleNamespace(
        get=lambda _wid: SimpleNamespace(root_path=str(tmp_path))
    )
    tool = next(
        tool
        for tool in build_spawn_runtime_tools(None, SimpleNamespace(fanout={}))
        if tool.name == "spawn_agents_parallel"
    )
    token, sid = context.set_app(app), context.set_session_id(root.owner.session_id)
    try:
        async with BuiltinBoundary(app, lambda _context: None) as boundary:
            result = await boundary.call(
                tool,
                {
                    "spawns": [
                        {"agent": "writer", "task": "Write original"},
                        {
                            "agent": "writer",
                            "task": "Invalid evidence",
                            "input_task_ids": ["missing"],
                        },
                        {"task": "No target agent"},
                    ]
                },
                contextvars.copy_context(),
            )
        assert result["spawned"] == result["outcomes"] and len(result["spawned"]) == 3
        assert result["spawned"][0]["accepted"] is True
        child = app.state.sessions.get(result["spawned"][0]["session_id"])
        assert child is not None and child.agent["id"] == "writer"
        assert result["spawned"][1]["index"] == 1 and "error" in result["spawned"][1]
        assert result["spawned"][2]["index"] == 2
        assert "agent must name" in result["spawned"][2]["error"]
    finally:
        context.reset(sid)
        context.reset(token)
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_transcript_injection_matches_the_committed_rendered_delegation(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, owner, _source, critic, _body = await evidence_children(arc, tmp_path)
    emitted: list[str] = []
    monkeypatch.setattr(
        clio_react_record, "emit_injection", lambda _source, text, **_kwargs: emitted.append(text)
    )

    def commit() -> None:
        token = context.set_app(app)
        try:
            context.set_turn_identity(
                app=app,
                session_id=critic.owner.session_id,
                turn_id="critic-turn",
                trace_id="critic-trace",
                assignment_handle=critic.handle,
                input_notification_id="assignment:" + critic.handle,
            )
            recorder = clio_react_record.StepRecorder(
                arc, critic.owner.session_id, "main", expert_id="critic"
            )
            recorder.commit_input(0)
            committed = owner.history.latest_input(critic.handle)
            assert committed is not None and len(committed.rendered_content) == 1
            rendered = owner.history.read_content(committed.rendered_content[0])
            assert isinstance(rendered, dict)
            assert emitted == [rendered["text"]]
            assert emitted[0].startswith("Critique the original research")
            assert "get_task_result" in emitted[0] and "cursor" in emitted[0]
        finally:
            context.reset(token)

    await asyncio.to_thread(commit)
    await owner.recovery.drain()
