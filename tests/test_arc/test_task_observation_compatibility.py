"""Fresh public observation contracts against real Core, without retired task stores."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import TaskOwner, TaskProgress, TaskSnapshot
from clio_agent.gact import context
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_cursors import decode_cursor, encode_cursor
from clio_agent.gact.task_declarations import build_task_compatibility_tools
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from tests._result_room import pin_result_room
from tests.test_arc.test_conversation_owner_contracts import create_environment


async def forbidden_cancel(_task: TaskSnapshot, _caller: TaskCaller) -> dict[str, Any]:
    pytest.fail("Observation must never cancel work")


def running_shell(history: TaskHistory, parent: TaskSnapshot, handle: str) -> TaskSnapshot:
    """Record the same owner snapshot/output format used by native execution."""
    stamp = now()
    task = TaskSnapshot(
        handle=handle,
        kind="Shell",
        owner=TaskOwner(
            human_id=parent.owner.human_id,
            session_id=parent.owner.session_id,
            assignment_id=parent.handle,
            parent_handle=parent.handle,
        ),
        invocation_id=handle,
        tool="shell_bash",
        label="Needle only in metadata",
        description="Needle only in metadata",
        description_missing=False,
        created_at=stamp,
        updated_at=stamp,
        status="running",
    )
    history.record_snapshot(task, observation_id=handle)
    return task


def append_output(history: TaskHistory, task: TaskSnapshot, output: str) -> TaskSnapshot:
    """Publish one immutable output chunk through the production Core writer."""
    reference = history.write_output(
        task.handle,
        {
            "chunk_id": output,
            "output": output,
            "previous": task.progress.output.model_dump(mode="json")
            if task.progress.output
            else None,
        },
    )
    current = task.model_copy(
        update={"progress": TaskProgress(output=reference, message="Working")}
    )
    history.record_snapshot(current, observation_id=task.handle + ":" + output)
    return current


@pytest.mark.asyncio
async def test_output_pages_preserve_omitted_rows_bytes_and_new_output(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    history = owner.history
    pin_result_room(monkeypatch, 7)
    first = await asyncio.to_thread(running_shell, history, root, "shell_first")
    second = await asyncio.to_thread(running_shell, history, root, "shell_second")
    first = await asyncio.to_thread(append_output, history, first, "abcdefghijklmno")
    second = await asyncio.to_thread(append_output, history, second, "second output")
    operations, caller = (
        TaskOperations(history, forbidden_cancel),
        TaskCaller(root.owner.session_id, "human"),
    )
    cursor = None
    received: dict[str, str] = {first.handle: "", second.handle: ""}
    for page_number in range(12):
        page = await operations.observe(caller, list(received), cursor, row_limit=1)
        assert len(page["tasks"]) <= 1
        for row in page["tasks"]:
            for chunk in row.get("output", []):
                received[row["handle"]] += chunk["output"]
        cursor = page["cursor"]
        if page_number == 0:
            first = await asyncio.to_thread(append_output, history, first, "late")
        if not page["has_more"]:
            break
    else:
        pytest.fail("A fixed backlog plus one arrival must drain without a new producer event")
    assert received == {first.handle: "abcdefghijklmno" + "late", second.handle: "second output"}
    assert (await operations.observe(caller, list(received), cursor))["tasks"] == []
    assert await asyncio.to_thread(history.pending_mail, caller.session_id) == ()
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_pattern_drains_pages_and_does_not_match_assignment_metadata(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    _app, owner, root = await create_environment(arc, tmp_path)
    first = await asyncio.to_thread(running_shell, owner.history, root, "shell_first")
    second = await asyncio.to_thread(running_shell, owner.history, root, "shell_second")
    second = await asyncio.to_thread(append_output, owner.history, second, "actual match")
    operations, caller = (
        TaskOperations(owner.history, forbidden_cancel),
        TaskCaller(root.owner.session_id, "human"),
    )
    matched = await operations.observe(
        caller,
        [first.handle, second.handle],
        pattern="actual match",
        timeout_s=2,
        row_limit=1,
    )
    assert matched["timed_out"] is False and matched["scanned_pages"] == 1
    assert matched["tasks"][0]["handle"] == second.handle
    absent = await operations.observe(
        caller,
        first.handle,
        pattern="Needle",
        timeout_s=0,
    )
    assert absent["timed_out"] is True
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_output_cursors_cannot_read_another_tasks_content(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, owner, root = await create_environment(arc, tmp_path)
    pin_result_room(monkeypatch, 4)
    first = await asyncio.to_thread(running_shell, owner.history, root, "shell_first")
    second = await asyncio.to_thread(running_shell, owner.history, root, "shell_second")
    first = await asyncio.to_thread(append_output, owner.history, first, "first private output")
    second = await asyncio.to_thread(append_output, owner.history, second, "second private output")
    operations, caller = (
        TaskOperations(owner.history, forbidden_cancel),
        TaskCaller(root.owner.session_id, "human"),
    )
    page = await operations.observe(caller, first.handle)
    state = decode_cursor(page["cursor"])
    assert second.progress.output is not None
    state["outputs"][first.handle]["pending"]["reference"] = second.progress.output.model_dump(
        mode="json"
    )
    with pytest.raises(ValueError, match="task-local provenance"):
        await operations.observe(caller, first.handle, encode_cursor(state))
    with pytest.raises(ValueError, match="different read or history epoch") as refused:
        await operations.observe(TaskCaller("other", "human"), first.handle, page["cursor"])
    assert "first private output" not in str(refused.value)
    assert "second private output" not in str(refused.value)
    await owner.shutdown()
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_legacy_observation_preserves_surface_and_honest_cursor_migration(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    app.state.task_operations = TaskOperations(owner.history, forbidden_cancel)
    shell = await asyncio.to_thread(running_shell, owner.history, root, "shell")
    (tool,) = build_task_compatibility_tools(["observe_agent_tasks"])
    schema = tool.format_as_litellm_function_call()["function"]["parameters"]["properties"]
    assert set(schema) == {"task_ids", "cursor", "limit", "pattern", "include_state", "timeout_s"}
    token, sid = context.set_app(app), context.set_session_id(root.owner.session_id)
    try:
        with pytest.raises(ValueError, match="legacy_observation_history_unavailable"):
            await tool.func([shell.handle], cursor=42)
        first = await tool.func([shell.handle], cursor=1)
        assert "legacy_history_unavailable" in first["notice"]
        assert first["tasks"][0]["workflow_state_available"] is False
        assert (await tool.func([shell.handle], cursor=first["next_cursor"]))["tasks"] == []
    finally:
        context.reset(sid)
        context.reset(token)
        await owner.recovery.drain()
