"""Flows read exact authorized tool bodies without copying them into checkpoints."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from clio_agent.arc.task_types import CoordinationGrant, TaskOwner, TaskSnapshot
from clio_agent.flows import tool_evidence
from clio_agent.flows.context import FlowContext
from clio_agent.flows.outcomes import CallRefused
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskAccessDenied, TaskCaller
from tests.test_gact.task_records import record_assignment_fixture
from tests.test_gact.test_permission_evidence import ReceiptFlow
from tests.test_gact.test_permission_evidence import world as world

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _tool(app: Any, owner: TaskSnapshot, value: Any) -> TaskSnapshot:
    handle = "task_" + uuid4().hex
    task = TaskSnapshot(
        handle=handle,
        kind="MCP",
        label="Read evidence",
        tool="web_fetch",
        owner=TaskOwner(
            human_id=owner.owner.human_id,
            session_id=owner.owner.session_id,
            assignment_id=owner.handle,
            parent_handle=owner.handle,
        ),
        invocation_id=handle,
        created_at=owner.created_at,
        updated_at=owner.updated_at,
        terminal_at=owner.updated_at,
        status="completed",
        raw_status="completed",
        settlement="external_acknowledged",
        result=app.state.task_history.write_content(
            owner.owner.session_id,
            value,
        ),
    )
    app.state.task_history.record_snapshot(task, observation_id=handle)
    return task


def _context(app: Any, sid: str, root: TaskSnapshot) -> FlowContext:
    return FlowContext(
        app=app,
        session_id=sid,
        module=SimpleNamespace(resolution=None),
        flow=ReceiptFlow(),
        root_handle=root.handle,
        run_invocation="tool-evidence",
        task="Read exact results",
    )


def test_child_tool_results_are_complete_pinned_and_isolated_from_siblings(
    request: pytest.FixtureRequest,
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    child_sid = client.post("/v1/sessions", json={"title": "Critic"}).json()["id"]
    child = record_assignment_fixture(app, child_sid, parent_sid=sid)
    value = {"content": "A full source passage. " * 5000}
    task = _tool(app, child, value)
    _tool(app, root, {"not": "this critic's tool"})
    first = _context(app, sid, root)
    rows = asyncio.run(first.tool_results(child.handle))
    assert [row["task"]["handle"] for row in rows] == [task.handle]
    assert rows[0]["value"] == value
    assert rows[0]["result_reference"] == task.result.model_dump(mode="json")
    checkpoint = first.journal.content["steps"]
    assert "A full source passage" not in str(checkpoint)
    _tool(app, child, {"new": "later result"})
    replay = _context(app, sid, root)
    assert asyncio.run(replay.tool_results(child.handle)) == rows
    assert len(asyncio.run(replay.tool_results(child.handle))) == 2


def test_tool_evidence_cannot_be_read_from_an_unrelated_assignment(
    request: pytest.FixtureRequest,
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    other_sid = client.post("/v1/sessions", json={"title": "Unrelated"}).json()["id"]
    other = record_assignment_fixture(app, other_sid)
    _tool(app, other, {"private": "other assignment"})
    with pytest.raises(CallRefused, match="assignment subtree"):
        asyncio.run(_context(app, sid, root).tool_results(other.handle))


def test_tool_evidence_rechecks_authorization_after_content_read(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _client, sid, root = request.getfixturevalue("world")
    tool = _tool(app, root, {"source": "exact body"})
    original_read = app.state.task_history.read_content
    original_require = TaskAccess.require
    revoked = False

    def read(reference: Any) -> Any:
        nonlocal revoked
        value = original_read(reference)
        if reference == tool.result:
            revoked = True
        return value

    def require(access: Any, handle: str, right: str) -> Any:
        if revoked and handle == tool.handle:
            raise TaskAccessDenied("Read authority revoked")
        return original_require(access, handle, right)

    monkeypatch.setattr(app.state.task_history, "read_content", read)
    monkeypatch.setattr(TaskAccess, "require", require)
    with pytest.raises(TaskAccessDenied, match="revoked"):
        asyncio.run(_context(app, sid, root).tool_results(root.handle))


def test_a_real_coordination_revocation_is_not_hidden_by_a_cached_access_view(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    reader = client.post("/v1/sessions", json={"title": "Authorized reader"}).json()["id"]
    history = app.state.task_history
    grant = CoordinationGrant(
        id="grant_" + uuid4().hex,
        granting_human=app.state.human_principal,
        recipient_session=reader,
        target_session=sid,
        target_scope="tree",
        rights=("read",),
        created_at=now(),
    )
    history.record_grant(grant, observation_id=uuid4().hex)
    task = _tool(app, root, {"private": "only before revocation"})
    original = history.read_content

    def read(reference: Any) -> Any:
        value = original(reference)
        if reference == task.result:
            history.record_grant(
                grant.model_copy(update={"revoked_at": now()}), observation_id=uuid4().hex
            )
        return value

    monkeypatch.setattr(history, "read_content", read)
    context = SimpleNamespace(
        app=app, root_handle=root.handle, caller=TaskCaller(reader, app.state.human_principal)
    )
    with pytest.raises(TaskAccessDenied):
        tool_evidence._read(context, root.handle, [task.model_dump(mode="json")])
