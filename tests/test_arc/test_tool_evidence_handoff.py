"""A critic's actual terminal tool result can be quoted to a later critic."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import CoordinationGrant, TaskOwner, TaskSnapshot
from clio_agent.flows.context import FlowContext
from clio_agent.flows.outcomes import CallRefused
from clio_agent.gact.assignment_evidence import select_assignment_evidence
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.sdk import Flow
from tests.test_arc.test_assignment_evidence_contracts import commit_delegation, evidence_children
from tests.test_arc.test_task_observation_compatibility import forbidden_cancel


class EvidenceFlow(Flow):
    """Exercise production child admission without running model inference."""

    id = "evidence-handoff"

    async def forward(self, task: Any, ctx: FlowContext) -> Any:
        return None


def _context(app: Any, root: TaskSnapshot) -> FlowContext:
    return FlowContext(
        app=app,
        session_id=root.owner.session_id,
        module=SimpleNamespace(resolution=None),
        flow=EvidenceFlow(),
        root_handle=root.handle,
        run_invocation="audit-handoff",
        task="Audit retrieved primary evidence",
    )


def _fetch(history: Any, source: TaskSnapshot, body: Any) -> TaskSnapshot:
    task = TaskSnapshot(
        handle="task_fetch",
        kind="MCP",
        tool="web_fetch",
        description="Fetched source document",
        description_missing=False,
        label="Read primary document",
        invocation_id="original-fetch",
        created_at=source.created_at,
        updated_at=source.updated_at,
        terminal_at=source.terminal_at,
        status="completed",
        raw_status="completed",
        settlement="external_acknowledged",
        owner=TaskOwner(
            human_id="human",
            session_id=source.owner.session_id,
            assignment_id=source.handle,
            parent_handle=source.handle,
        ),
        result=history.write_content(source.owner.session_id, body),
    )
    history.record_snapshot(task, observation_id="fetch-result")
    return task


@pytest.mark.asyncio
async def test_finished_critic_tool_handoff_uses_real_delegation_commit_and_exact_read(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, source, _critic, _body = await evidence_children(arc, tmp_path)
    history = owner.history
    root = history.task_snapshot(source.owner.parent_handle)
    assert root is not None
    body = {"source_url": "https://primary.example/doc", "content": "exact retrieved text " * 5000}
    fetch = await asyncio.to_thread(_fetch, history, source, body)
    handle = await _context(app, root).start(
        "self",
        "Critique the original research",
        evidence=[fetch.handle],
        key="repair-fetch",
    )
    assert (
        await _context(app, root).start(
            "self",
            "Critique the original research",
            evidence=[fetch.handle],
            key="repair-fetch",
        )
        == handle
    )
    critic = history.task_snapshot(handle)
    assert critic is not None
    reader = TaskCaller(critic.owner.session_id, "human")
    operations = TaskOperations(history, forbidden_cancel)
    with pytest.raises(ValueError, match="before delegated input commitment"):
        await asyncio.to_thread(operations.result, reader, fetch.handle)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "31")
    cursor = await asyncio.to_thread(commit_delegation, owner, critic)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "12000")
    metadata = await asyncio.to_thread(
        operations.result,
        reader,
        fetch.handle,
        cursor,
        select=None,
    )
    assert metadata["result_kind"] == "shared_evidence"
    assert metadata["result_reference"] == fetch.result.model_dump(mode="json")
    fragments = [metadata["result"]["preview"]]
    while metadata["next_cursor"]:
        metadata = await asyncio.to_thread(
            operations.result,
            reader,
            fetch.handle,
            metadata["next_cursor"],
        )
        fragments.append(metadata["result"]["preview"])
    assert json.loads("".join(fragments)) == body
    # A selected fresh read uses the same exact delegated source, without paging unrelated text.
    metadata = await asyncio.to_thread(
        operations.result,
        reader,
        fetch.handle,
        select=["/source_url"],
    )
    assert metadata["result"] == {"/source_url": body["source_url"]}
    for right in ("read", "observe", "message", "control"):
        with pytest.raises(ValueError, match="unavailable"):
            await asyncio.to_thread(TaskAccess(history, reader).require, fetch.handle, right)
    assert await asyncio.to_thread(history.read_content, fetch.result) == body
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_unrelated_granted_or_unfinished_results_cannot_be_reshared(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, source, _critic, _body = await evidence_children(arc, tmp_path)
    history = owner.history
    root = history.task_snapshot(source.owner.parent_handle)
    assert root is not None
    caller = TaskCaller(root.owner.session_id, "human")
    fetch = await asyncio.to_thread(_fetch, history, source, {"content": "private source"})
    peer_sid = (await asyncio.to_thread(app.state.sessions.create, workspace_id="workspace")).id
    await asyncio.to_thread(
        history.record_grant,
        CoordinationGrant(
            id="peer-read",
            granting_human="human",
            recipient_session=peer_sid,
            target_session=source.owner.session_id,
            target_scope="tree",
            rights=("read",),
            created_at=now(),
        ),
        observation_id="peer-grant",
    )
    peer = TaskCaller(peer_sid, "human")
    assert (
        await asyncio.to_thread(TaskAccess(history, peer).require, fetch.handle, "read")
    ).handle == fetch.handle
    with pytest.raises(ValueError, match="session's tree"):
        await asyncio.to_thread(select_assignment_evidence, history, peer, [fetch.handle])
    pending = fetch.model_copy(
        update={
            "handle": "task_pending_fetch",
            "terminal_at": None,
            "result": None,
            "status": "active",
            "raw_status": "working",
            "settlement": "unknown",
        }
    )
    await asyncio.to_thread(history.record_snapshot, pending, observation_id="pending-fetch")
    with pytest.raises(ValueError, match="finished task"):
        await asyncio.to_thread(select_assignment_evidence, history, caller, [pending.handle])
    with pytest.raises(CallRefused, match="finished task"):
        await _context(app, root).start("self", "No unfinished evidence", evidence=[pending.handle])
    await owner.recovery.drain()
