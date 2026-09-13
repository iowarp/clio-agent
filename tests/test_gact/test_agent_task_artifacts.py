"""Commissioned-blueprint artifact return and parent-context tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.agent_task_artifacts import (
    artifact_context_for_task,
    artifact_context_text,
    emit_commission_parent_use,
    returned_artifact_ref,
)
from clio_agent.gact.agent_tasks import AgentTask
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    Custody,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.types import Message, Part


class _Rows:
    def __init__(self, rows: dict[str, Any]) -> None:
        self._rows = rows

    def get(self, row_id: str) -> Any:
        return self._rows.get(row_id)


def _artifact_app(tmp_path: Path) -> tuple[Any, Any, Path]:
    report = tmp_path / "report.md"
    report.write_text("# Registered report\n\nFull evidence, not an excerpt.\n", encoding="utf-8")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    registry = ArtifactRegistry()
    outcome = registry.mint(
        workspace_id="ws_test",
        name="report.md",
        event_id="event_report",
        kind=ArtifactKind.REPORT,
        custody=Custody.WORKSPACE_REFERENCED,
        mechanism=Mechanism.TOOL_SCHEMA,
        evidence=IdentityEvidence.hashed_at_use(
            sha256=digest,
            size_bytes=report.stat().st_size,
        ),
        producer={"session_id": "child_session", "tool": "create_artifact"},
        path=str(report),
        created_at="2026-09-05T00:00:00+00:00",
        annotation="",
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            artifact_registry=registry,
            workspaces=_Rows({"ws_test": SimpleNamespace(id="ws_test", root_path=str(tmp_path))}),
            semantic_event_sink=None,
        )
    )
    return app, outcome.version, report


def test_returned_artifact_ref_is_real_and_context_contains_verified_report(
    tmp_path: Path,
) -> None:
    app, version, report = _artifact_app(tmp_path)
    final = Message(
        id="message_final",
        session_id="child_session",
        role="assistant",
        created_at="2026-09-05T00:00:00+00:00",
        updated_at="2026-09-05T00:00:00+00:00",
        parts=[
            Part(
                type="resource_link",
                name="report.md",
                server_id="clio-artifacts",
                metadata={"artifact_id": version.artifact_id},
            )
        ],
    )

    artifact_ref = returned_artifact_ref(app, final)
    assert artifact_ref["artifact_id"] == version.artifact_id
    assert artifact_ref["sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert artifact_ref["metadata"]["kind"] == "report"
    assert artifact_ref["metadata"]["uri"] == "artifact://ws_test/report.md@v1"

    task = AgentTask(
        task_id="task_report",
        parent_session_id="parent_session",
        child_session_id="child_session",
        agent_ref={
            "expert_id": "main",
            "requesting_expert_id": "base",
            "blueprint_id": "deep-researcher",
        },
        status="completed",
        result={"answer_excerpt": "bounded excerpt"},
        artifact_ref=artifact_ref,
    )
    context = artifact_context_for_task(app, task)
    assert context["content_status"] == "complete"
    assert context["content"] == report.read_bytes().decode("utf-8")
    rendered = artifact_context_text(context)
    assert "<commissioned-artifact>" in rendered
    assert "Full evidence, not an excerpt." in rendered

    assert emit_commission_parent_use(app, "parent_session", task) is True
    assert app.state.artifact_registry.used_artifact_ids_for_session("parent_session") == {
        version.artifact_id
    }
    assert emit_commission_parent_use(app, "parent_session", task) is False


def test_child_without_artifact_does_not_require_an_artifact_registry() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    final = SimpleNamespace(parts=[Part(type="text", text="ordinary answer")])
    assert returned_artifact_ref(app, final) == {}
