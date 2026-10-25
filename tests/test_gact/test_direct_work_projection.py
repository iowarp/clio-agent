"""Direct Work listings must preserve assignment lineage and deeper access."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.task_types import TaskOwner, TaskSnapshot
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_history import TaskHistory
from tests.test_gact.task_records import record_assignment_fixture


def _session(client: TestClient, title: str) -> str:
    return str(client.post("/v1/sessions", json={"title": title}).json()["id"])


def _leaf(app: Any, assignment: TaskSnapshot) -> TaskSnapshot:
    """Record a passive terminal MCP observation; start no backend or model."""
    identity = "task_" + uuid4().hex
    task = TaskSnapshot(
        handle=identity,
        kind="MCP",
        tool="fetch",
        label="Read a source",
        owner=TaskOwner(
            human_id=assignment.owner.human_id,
            session_id=assignment.owner.session_id,
            assignment_id=assignment.handle,
            parent_handle=assignment.handle,
        ),
        invocation_id=identity,
        description="Retained source read",
        description_missing=False,
        status="completed",
        raw_status="completed",
        settlement="external_acknowledged",
        created_at=assignment.created_at,
        updated_at=assignment.updated_at,
        terminal_at=assignment.updated_at,
    )
    app.state.task_history.record_snapshot(task, observation_id=uuid4().hex)
    recorded = app.state.task_history.task_snapshot(identity)
    assert recorded is not None
    return recorded


def test_parent_work_keeps_flow_but_excludes_its_agents_and_tools(
    world: tuple[Any, TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each selected agent shows its next lineage edge, without changing tree rights."""
    app, client, sid = world
    root = app.state.task_history.latest_assignment(sid)
    assert root is not None
    flow_sid = _session(client, "Research flow")
    flow = record_assignment_fixture(app, flow_sid, parent_sid=sid)
    researcher_sid = _session(client, "Researcher")
    researcher = record_assignment_fixture(app, researcher_sid, parent_sid=flow_sid)
    source = _leaf(app, researcher)
    direct_source = _leaf(app, root)
    unrelated_sid = _session(client, "Unrelated")
    unrelated = record_assignment_fixture(app, unrelated_sid)
    _leaf(app, unrelated)

    def read(owner: str, **params: Any) -> dict[str, Any]:
        response = client.get(
            f"/v1/sessions/{owner}/async-processes", params={"include_descendants": False, **params}
        )
        assert response.status_code == 200, response.text
        return response.json()

    original_read = app.state.task_history.task_snapshot

    def own_only(handle: str) -> TaskSnapshot | None:
        assert handle not in {researcher.handle, source.handle, unrelated.handle}
        return original_read(handle)

    with monkeypatch.context() as display:
        display.setattr(app.state.task_history, "task_snapshot", own_only)
        own = read(sid)
    assert {task["handle"] for task in own["processes"]} == {flow.handle, direct_source.handle}
    assert own["total"] == 2
    assert [task["handle"] for task in read(flow_sid)["processes"]] == [researcher.handle]
    assert [task["handle"] for task in read(researcher_sid)["processes"]] == [source.handle]
    assert {task["handle"] for task in read(sid, active_only=True)["processes"]} == {flow.handle}
    full = {task["handle"] for task in read(sid, include_descendants=True)["processes"]}
    assert {
        root.handle,
        flow.handle,
        researcher.handle,
        source.handle,
        direct_source.handle,
    } <= full
    access = TaskAccess(app.state.task_history, TaskCaller(sid, app.state.human_principal))
    assert access.require(source.handle, "read") == source
    legacy = client.get(f"/v1/sessions/{sid}/async-processes").json()
    assert {task["handle"] for task in legacy["processes"]} == full


def test_lifecycle_actions_follow_latest_assignment_and_activity(
    world: tuple[Any, TestClient, str],
) -> None:
    """Only a current paused assignment can resume; restart targets the current row."""
    app, _client, sid = world
    history = app.state.task_history
    first = history.latest_assignment(sid)
    assert first is not None
    first = first.model_copy(
        update={
            "supported_actions": (
                "observe",
                "wait",
                "message",
                "pause",
                "resume",
                "cancel",
                "restart",
            )
        }
    )
    history.record_snapshot(first, observation_id=uuid4().hex)
    caller = TaskCaller(sid, app.state.human_principal)
    access = TaskAccess(history, caller)
    active_actions = access.view(first)["supported_actions"]
    assert "restart" in active_actions
    assert "resume" not in active_actions

    paused = first.model_copy(
        update={"activity": "paused", "supported_actions": (*first.supported_actions, "resume")}
    )
    history.record_snapshot(paused, observation_id=uuid4().hex)
    assert "resume" in access.view(paused)["supported_actions"]

    successor = record_assignment_fixture(app, sid)
    successor = successor.model_copy(update={"supported_actions": first.supported_actions})
    history.record_snapshot(successor, observation_id=uuid4().hex)
    successor_actions = access.view(successor)["supported_actions"]
    old_actions = access.view(paused)["supported_actions"]
    assert "restart" in successor_actions and "resume" not in successor_actions
    assert "restart" not in old_actions and "resume" not in old_actions

    external = successor.model_copy(
        update={
            "handle": "task_external_capabilities",
            "kind": "MCP",
            "tool": "mcp_server",
            "invocation_id": "external-capabilities",
            "owner": successor.owner.model_copy(
                update={"assignment_id": "task_external_capabilities"}
            ),
            "supported_actions": ("observe", "resume", "restart"),
        }
    )
    history.record_snapshot(external, observation_id=uuid4().hex)
    external_actions = access.view(external)["supported_actions"]
    assert "resume" in external_actions and "restart" in external_actions


def test_direct_pages_span_assignments_and_do_not_reuse_a_tree_cursor(
    world: tuple[Any, TestClient, str],
) -> None:
    """Retained direct work is paged over the complete session, not only its latest turn."""
    app, client, sid = world
    original = app.state.task_history.latest_assignment(sid)
    assert original is not None
    first = _leaf(app, original)
    second_assignment = record_assignment_fixture(app, sid)
    second = _leaf(app, second_assignment)
    caller = TaskCaller(sid, app.state.human_principal, human_action=True)
    owner = app.state.task_operations
    page = owner.query(caller, relation="direct", limit=1)
    following = owner.query(caller, relation="direct", limit=1, cursor=page["cursor"])
    assert page["total"] == following["total"] == 2
    assert {page["tasks"][0]["handle"], following["tasks"][0]["handle"]} == {
        first.handle,
        second.handle,
    }
    with pytest.raises(ValueError, match="different query"):
        owner.query(caller, cursor=page["cursor"])
    assert owner.query(caller, relation="direct", handle=original.handle)["tasks"] == []
    assert owner.query(TaskCaller(sid, "another-human"), relation="direct")["total"] == 0
    with pytest.raises(ValueError, match="one session"):
        owner.query(caller, relation="direct", scope="authorized")


def test_direct_index_recovers_late_parent_and_survives_restore(
    world: tuple[Any, TestClient, str],
) -> None:
    """Historical child-first observations acquire their direct edge when the parent arrives."""
    app, client, sid = world
    history = app.state.task_history
    parent_handle = "task_" + uuid4().hex
    child_sid = _session(client, "Recovered child")
    child = TaskSnapshot(
        handle="task_" + uuid4().hex,
        kind="Subagent",
        tool="conversation",
        owner=TaskOwner(
            human_id=app.state.human_principal,
            session_id=child_sid,
            assignment_id="recovered",
            parent_handle=parent_handle,
        ),
        label="Recovered child",
        invocation_id="recover-child",
        description="Historical observation",
        description_missing=False,
        status="active",
        created_at="2026-10-25T05:00:00Z",
        updated_at="2026-10-25T05:00:00Z",
    )
    history.record_snapshot(child, observation_id=uuid4().hex)
    before = history.task_order("direct", sid, human_id=app.state.human_principal)
    assert child.handle not in {handle for _, handle in before}
    record_assignment_fixture(app, sid, handle=parent_handle)
    assert child.handle in {handle for _, handle in history.task_order("direct", sid)}
    restored = TaskHistory(history.arc)
    restored.restore()
    assert child.handle in {handle for _, handle in restored.task_order("direct", sid)}


def test_current_session_provenance_does_not_load_descendant_task_trees(
    world: tuple[Any, TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit session-only read must not enumerate a hidden family for attribution."""
    from clio_agent.gact.provenance import child_projection

    app, client, sid = world
    child_sid = _session(client, "A deeper agent")
    record_assignment_fixture(app, child_sid, parent_sid=sid)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Session-only provenance loaded descendants")

    with monkeypatch.context() as reads:
        reads.setattr(app.state.task_history, "tree_handles", forbidden)
        reads.setattr(child_projection, "descendant_walk", forbidden)
        result = child_projection.project_child_execution(
            app,
            sid,
            {"spans": [], "nodes": [], "edges": []},
            include_children=False,
        )
    assert [row["session_id"] for row in result["session_lineage"]] == [sid]
