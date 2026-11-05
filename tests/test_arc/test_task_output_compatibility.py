"""Shipped getter compatibility delegates to common authorization and input commitment."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    ContextBoundary,
    CoreReference,
    TaskAlias,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.gact import context
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_declarations import build_task_compatibility_tools, build_task_tools
from clio_agent.gact.task_history import _encoded
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.tool_observation import CollectedTaskResult
from tests.test_arc.test_assignment_evidence_contracts import evidence_children
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_observation_compatibility import forbidden_cancel


@pytest.mark.asyncio
async def test_wait_and_message_aliases_preserve_canonical_receipts_and_addressing(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    app, owner, source, critic, body = await evidence_children(arc, tmp_path)
    assert source.owner.parent_handle is not None
    root = await asyncio.to_thread(owner.history.task_snapshot, source.owner.parent_handle)
    assert root is not None
    app.state.task_operations = TaskOperations(owner.history, forbidden_cancel)
    tools = {
        tool.name: tool
        for tool in build_task_compatibility_tools(["wait_agent_tasks", "message_agent"])
    }
    token, sid = context.set_app(app), context.set_session_id(root.owner.session_id)
    try:
        result = await tools["wait_agent_tasks"].func([source.handle])
        assert isinstance(result, CollectedTaskResult)
        assert "outcomes" not in result and result["results"][0]["result"] == body
        assert len(result.notification_ids) == 1
        assert (
            await asyncio.to_thread(owner.history.acknowledged_notifications, root.owner.session_id)
            == set()
        )
        message = await tools["message_agent"].func(critic.handle, "Inspect the evidence carefully")
        assert message["accepted"] is True and message["session_id"] == critic.owner.session_id
        pending = await asyncio.to_thread(owner.history.pending_mail, critic.owner.session_id)
        delivered = next(item for item in pending if item.id == message["message_id"])
        assert delivered.sender_session == root.owner.session_id and delivered.kind == "peer"
    finally:
        context.reset(sid)
        context.reset(token)
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_legacy_getter_preserves_authorized_result_and_durable_collection(
    arc: ARCMemory,
    tmp_path: Path,
    status: Literal["failed", "completed"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retained aliases resolve historical work; a read never acknowledges without input commit."""
    app, owner, root = await create_environment(arc, tmp_path)

    async def unexpected_cancel(_task: TaskSnapshot, _caller: TaskCaller) -> dict[str, Any]:
        pytest.fail("The legacy getter must never invoke cancellation")

    app.state.task_operations = TaskOperations(owner.history, unexpected_cancel)
    child_session = await asyncio.to_thread(app.state.sessions.create, workspace_id="workspace")
    unrelated = await asyncio.to_thread(app.state.sessions.create, workspace_id="workspace")
    body = {"output": "éèê 中文 🚀", "error_reason": "owner failed" if status == "failed" else ""}

    def records() -> TaskSnapshot:
        stamp = now()
        child = TaskSnapshot(
            handle="task_legacy_child",
            kind="Subagent",
            tool="conversation",
            label="Retained child",
            description="Original assignment",
            description_missing=False,
            owner=TaskOwner(
                human_id="human",
                session_id=child_session.id,
                assignment_id="task_legacy_child",
                parent_handle=root.handle,
            ),
            invocation_id="child-invocation",
            created_at=stamp,
            updated_at=stamp,
            status=status,
            terminal_at=stamp,
            settlement="confirmed",
            result=owner.history.write_content(child_session.id, body),
        )
        owner.history.record_snapshot(child, observation_id="child-result")
        owner.history.record_alias(
            TaskAlias(
                alias="old-child-id",
                handle=child.handle,
                source_identity="released-record",
            )
        )
        return child

    child = await asyncio.to_thread(records)
    assert child.result is not None
    (tool,) = build_task_compatibility_tools(["get_agent_task_output"])
    assert "get_agent_task_output" not in {tool.name for tool in build_task_tools()}
    assert build_task_compatibility_tools([]) == []
    declaration = tool.format_as_litellm_function_call()["function"]
    assert declaration["parameters"]["required"] == ["task_id"]
    assert "one release cycle" in declaration["description"]
    assert "bounds and result references" in declaration["description"]
    token, sid_token = context.set_app(app), context.set_session_id(root.owner.session_id)
    try:
        result = await tool.func(task_id="old-child-id")
        assert isinstance(result, CollectedTaskResult)
        assert result["handle"] == child.handle and result["task_id"] == "old-child-id"
        assert result["pending"] is False and result["status"] == status
        assert result["result"] == body and result["result_reference"] == child.result.model_dump(
            mode="json"
        )

        def collect() -> None:
            history, sid = owner.history, root.owner.session_id
            assert not history.acknowledged_notifications(sid)
            assert len(result.notification_ids) == 1
            assert [item.id for item in history.pending_mail(sid)] == list(result.notification_ids)
            observed = {
                "call_id": "legacy-getter",
                "text": json.dumps(dict(result), ensure_ascii=False),
                "notification_ids": list(result.notification_ids),
                "task_read_handles": list(result.read_handles),
            }
            segment = arc.append_segment(sid, "main", "observation", observed)
            observation = CoreReference(
                session_id=sid,
                scope="main",
                content_id=str(segment.id),
                sha256=hashlib.sha256(_encoded(observed)).hexdigest(),
            )
            staged = owner.mail.collection(TaskCaller(sid, "human"), result.notification_ids)
            owner.mail.commit(
                TaskCaller(sid, "human"),
                staged,
                assignment_id=root.handle,
                iteration_id="legacy-getter",
                boundary=ContextBoundary(session_id=sid, scope="main", logical_time=0),
                tool_observation=observation,
            )
            assert history.pending_mail(sid) == ()
            assert history.acknowledged_notifications(sid) == set(result.notification_ids)

        await asyncio.to_thread(collect)
        reread = await tool.func(task_id=child.handle)
        assert reread["result"] == body and reread.notification_ids == ()
        monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "8")
        monkeypatch.setenv("CLIO_AGENT_TASK_OUTPUT_DIGEST_CHARS", "8")
        bounded = await tool.func(task_id=child.handle)
        assert bounded["result"]["truncated"] is True and len(bounded["result"]["preview"]) == 8
        assert bounded["result_reference"] == reread["result_reference"]
        assert bounded.notification_ids == ()
        context.set_session_id(unrelated.id)
        with pytest.raises(ValueError, match="unavailable"):
            await tool.func(task_id="old-child-id")
    finally:
        context.reset(sid_token)
        context.reset(token)
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_legacy_getter_reports_pending_and_refuses_unknown_without_collecting(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """The compatibility name cannot turn a pending task or invalid handle into completion."""
    app, owner, root = await create_environment(arc, tmp_path)

    async def unexpected_cancel(_task: TaskSnapshot, _caller: TaskCaller) -> dict[str, Any]:
        pytest.fail("Result lookup must not cancel work")

    app.state.task_operations = TaskOperations(owner.history, unexpected_cancel)
    (tool,) = build_task_compatibility_tools(["get_agent_task_output"])
    token, sid_token = context.set_app(app), context.set_session_id(root.owner.session_id)
    try:
        pending = await tool.func(task_id=root.handle)
        assert isinstance(pending, CollectedTaskResult)
        assert pending["pending"] is True and pending.notification_ids == ()
        assert "result" not in pending
        with pytest.raises(ValueError, match="unavailable"):
            await tool.func(task_id="task_unknown")
        assert await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id) == ()
    finally:
        context.reset(sid_token)
        context.reset(token)
        await owner.recovery.drain()
