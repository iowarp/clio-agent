"""Child activity and provenance projection contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.provenance.child_projection import (
    child_session_lineage,
    project_child_execution,
)
from clio_agent.gact.provenance.normalization import normalize_semantic_events
from clio_agent.gact.routes.async_processes import project_session_async_processes
from clio_agent.gact.routes.provenance import _child_session_ids


@dataclass
class _Task:
    task_id: str
    parent_session_id: str
    child_session_id: str
    agent_ref: dict[str, str]
    depth: int
    status: str = "completed"
    run_index: int = 0
    run_label: str = ""
    created_at: str = "2026-09-02T12:00:00+00:00"
    updated_at: str = "2026-09-02T12:01:00+00:00"
    parent_turn_id: str = ""
    child_turn_id: str = ""
    fanout_bound: int = 0
    spawn_group_id: str = ""
    group_size: int = 0
    handle_id: str = ""
    live_state: str = "completed"
    host: str = "local"
    placement: str = "local"
    detached: bool = False
    dismissed_at: str = ""
    queued_reason: str = ""
    error_reason: str = ""
    result: dict[str, Any] | None = None
    artifact_ref: str = ""
    notify_pending: bool = False
    consumed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class _Registry:
    def __init__(self, tasks: list[_Task]) -> None:
        self.tasks = tasks

    def for_parent(self, session_id: str) -> list[_Task]:
        return [task for task in self.tasks if task.parent_session_id == session_id]

    def snapshot(self) -> list[_Task]:
        return list(self.tasks)


class _Sessions:
    """The session store's read surface the projection actually uses.

    ``list`` is here because the descendant walk reads BOTH substrates: the
    registry cannot see ``fork`` (no task delegated it) and the store cannot say
    which task owns ``child``.
    """

    def __init__(self) -> None:
        self.rows = {
            "root": SimpleNamespace(id="root", title="Investigation", parent_session_id=""),
            "child": SimpleNamespace(id="child", title="Evidence child", parent_session_id=""),
            "leaf": SimpleNamespace(id="leaf", title="Evidence leaf", parent_session_id=""),
            "fork": SimpleNamespace(id="fork", title="User fork", parent_session_id="root"),
        }

    def get(self, session_id: str) -> Any:
        return self.rows.get(session_id)

    def list(self, workspace_id: str | None = None) -> list[Any]:
        del workspace_id
        return list(self.rows.values())


def _app() -> Any:
    tasks = [
        _Task("task_child", "root", "child", {"expert_id": "researcher"}, 1),
        _Task("task_leaf", "child", "leaf", {"expert_id": "critic"}, 2),
    ]
    return SimpleNamespace(
        state=SimpleNamespace(agent_task_registry=_Registry(tasks), sessions=_Sessions())
    )


def test_child_lineage_preserves_nested_task_identity() -> None:
    rows, truncated = child_session_lineage(_app(), "root")

    by_id = {row["session_id"]: row for row in rows}
    assert set(by_id) == {"root", "child", "leaf", "fork"}
    assert truncated is False
    assert by_id["leaf"]["task_path"] == ["task_child", "task_leaf"]
    assert by_id["leaf"]["depth"] == 2
    assert by_id["leaf"]["agent_id"] == "critic"
    assert by_id["leaf"]["attribution"] == "agent_task"
    # A user fork is real topology with no task: it carries neither a task id
    # nor an inherited path, and says which substrate found it.
    assert by_id["fork"]["attribution"] == "session_fork"
    assert by_id["fork"]["task_id"] == ""
    assert by_id["fork"]["task_path"] == []


def test_projection_adds_typed_delegation_and_artifact_edges() -> None:
    normalized = normalize_semantic_events(
        [
            {
                "event_id": "artifact_event",
                "event_type": "artifact.created",
                "session_id": "leaf",
                "status": "completed",
                "summary": "Produced review",
                "actor": {"agent_id": "critic"},
                "subject": {"artifact_id": "artifact_review"},
                "payload": {"artifact_id": "artifact_review", "sha256": "abc"},
                "occurred_at": "2026-09-02T12:01:00+00:00",
            }
        ],
        provider="native",
        session_id="root",
    )

    result = project_child_execution(_app(), "root", normalized)

    span = result["spans"][0]
    assert span["root_session_id"] == "root"
    assert span["owner_session_id"] == "leaf"
    assert span["task_id"] == "task_leaf"
    assert span["task_path"] == ["task_child", "task_leaf"]
    edges = {(edge["source"], edge["target"], edge["kind"]) for edge in result["edges"]}
    assert ("session:root", "task:task_child", "delegated") in edges
    assert ("task:task_child", "session:child", "executes_in") in edges
    assert ("task:task_leaf", "artifact:artifact_review", "generated") in edges


def test_projection_without_children_does_not_leak_delegation_topology() -> None:
    normalized = normalize_semantic_events(
        [
            {
                "event_id": "root_turn",
                "event_type": "turn.completed",
                "session_id": "root",
                "status": "completed",
                "summary": "Root turn complete",
            }
        ],
        provider="native",
        session_id="root",
    )

    result = project_child_execution(_app(), "root", normalized, include_children=False)

    assert [row["session_id"] for row in result["session_lineage"]] == ["root"]
    assert result["lineage_truncated"] is False
    assert not [node for node in result["nodes"] if node["id"].startswith("task:")]
    assert not [edge for edge in result["edges"] if edge["kind"] == "delegated"]


def test_child_provenance_scope_covers_forks_as_well_as_delegated_children() -> None:
    """Both substrates, one walk — a fork's events are readable, not invisible."""

    assert set(_child_session_ids(_app(), "root")) == {"child", "leaf", "fork"}


def test_normalization_preserves_interaction_correlation_without_prompt_content() -> None:
    result = normalize_semantic_events(
        [
            {
                "event_id": "permission_resolved",
                "event_type": "permission.resolved",
                "session_id": "child",
                "status": "completed",
                "summary": "Permission resolved",
                "payload": {
                    "interaction_id": "interaction_1",
                    "permission_id": "permission_1",
                    "invocation_id": "invoke_1",
                    "tool_name": "workspace_write",
                    "prompt": "private prompt text",
                },
                "occurred_at": "2026-09-02T12:01:00+00:00",
            }
        ],
        provider="native",
        session_id="root",
    )

    span = result["spans"][0]
    assert span["kind"] == "interaction"
    assert span["invocation_id"] == "invoke_1"
    assert span["tool_name"] == "workspace_write"
    assert span["attributes"]["interaction_id"] == "interaction_1"
    assert "prompt" not in span["attributes"]


def test_native_user_question_events_are_interactions() -> None:
    result = normalize_semantic_events(
        [
            {
                "event_id": "question_created",
                "event_type": "user_question.created",
                "session_id": "child",
                "status": "pending",
                "summary": "Response needed",
            }
        ],
        provider="native",
        session_id="root",
    )

    assert result["spans"][0]["kind"] == "interaction"


def test_async_process_projection_includes_nested_children(
    monkeypatch: Any,
) -> None:
    empty_store = SimpleNamespace(list=lambda: [])
    monkeypatch.setattr(
        "clio_agent.gact.routes.async_processes.app_task_store", lambda _app: empty_store
    )

    rows = project_session_async_processes(_app(), "root")

    assert [row["id"] for row in rows] == ["task_child", "task_leaf"]
    assert rows[1]["owner_session_id"] == "leaf"
    assert rows[1]["task_path"] == ["task_child", "task_leaf"]
