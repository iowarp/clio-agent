"""Structured context-reference discovery, authorization, and delivery tests."""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    Custody,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.context_references import (
    authorize_context_reference_parts,
    enrich_with_context_references,
    search_workspace_references,
)
from clio_agent.gact.loop_inbox import enqueue_user_steer
from clio_agent.gact.messaging import _user_message_parts
from clio_agent.gact.routes.references import register_reference_routes
from clio_agent.gact.types import Message, Part


class _Agent:
    def forward(self, question: str, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(answer="ok", selected_expert="", routing_rationale="")


class _Workspaces:
    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots = roots

    def get(self, workspace_id: str) -> SimpleNamespace | None:
        root = self._roots.get(workspace_id)
        if root is None:
            return None
        return SimpleNamespace(id=workspace_id, root_path=str(root))


class _Sessions:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = {row.id: row for row in rows}

    def get(self, session_id: str) -> SimpleNamespace | None:
        return self._rows.get(session_id)

    def list(self, *, workspace_id: str | None = None) -> list[SimpleNamespace]:
        rows = list(self._rows.values())
        if workspace_id:
            rows = [row for row in rows if row.workspace_id == workspace_id]
        return rows


class _Resources:
    def list(self, workspace_id: str) -> list[SimpleNamespace]:
        if workspace_id != "ws_a":
            return []
        return [
            SimpleNamespace(
                id="res_1",
                name="observations.csv",
                revision="rev_7",
                detected_mime="text/csv",
            )
        ]


def _session(
    session_id: str,
    workspace_id: str,
    *,
    title: str = "Session",
    message_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        workspace_id=workspace_id,
        title=title,
        status="idle",
        message_count=message_count,
        updated_at="2026-09-02T12:00:00+00:00",
    )


def _app(tmp_path: Path) -> SimpleNamespace:
    session_a = _session("sess_a", "ws_a", title="Shared title", message_count=12)
    session_b = _session("sess_b", "ws_a", title="Shared title")
    session_other = _session("sess_other", "ws_b", title="Private")
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_1",
            parent_session_id="sess_a",
            child_session_id="sess_b",
            agent_ref={"expert_id": "analysis"},
            run_label="Analysis run",
            status="completed",
            result={"answer_excerpt": "bounded result"},
            created_at="2026-09-02T11:00:00+00:00",
            updated_at="2026-09-02T12:00:00+00:00",
        )
    )
    message_rows = [
        Message(
            id=f"msg_{index}",
            session_id="sess_a",
            role="user" if index % 2 else "assistant",
            created_at="2026-09-02T12:00:00+00:00",
            updated_at="2026-09-02T12:00:00+00:00",
            parts=[Part(type="text", text=f"message {index} " + "x" * 800)],
        )
        for index in range(12)
    ]
    state = SimpleNamespace(
        workspaces=_Workspaces({"ws_a": tmp_path / "a", "ws_b": tmp_path / "b"}),
        sessions=_Sessions([session_a, session_b, session_other]),
        messages={"sess_a": message_rows, "sess_b": [], "sess_other": []},
        agent_task_registry=registry,
        artifact_registry=ArtifactRegistry(),
        resource_store=_Resources(),
        loop_inboxes={},
    )
    return SimpleNamespace(state=state)


def _mint_artifact(app: SimpleNamespace, path: Path) -> str:
    data = path.read_bytes()
    outcome = app.state.artifact_registry.mint(
        workspace_id="ws_a",
        name=path.name,
        event_id="event_1",
        kind=ArtifactKind.REPORT,
        custody=Custody.WORKSPACE_REFERENCED,
        mechanism=Mechanism.TOOL_SCHEMA,
        evidence=IdentityEvidence.hashed_at_use(
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        ),
        producer={"session_id": "sess_a", "call_id": "call_1"},
        path=str(path),
        created_at=datetime.now(timezone.utc).isoformat(),
        annotation="",
    )
    return outcome.version.artifact_id


def test_context_ref_wire_keeps_part_identity_separate() -> None:
    part = Part(
        id="part_1",
        type="context_ref",
        ref_kind="workspace_file",
        ref_id="data/input.csv",
        label="input.csv",
        revision="sha256:abc",
    )

    assert part.to_wire() == {
        "id": "part_1",
        "type": "context_ref",
        "agent_id": "",
        "ref_kind": "workspace_file",
        "ref_id": "data/input.csv",
        "label": "input.csv",
        "revision": "sha256:abc",
    }


def test_search_aggregates_repositories_with_exact_shape_and_duplicate_labels(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    first = tmp_path / "a" / "one" / "observations.csv"
    second = tmp_path / "a" / "two" / "observations.csv"
    artifact = tmp_path / "a" / "report.md"
    for path, body in ((first, "one"), (second, "two"), (artifact, "report")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    artifact_id = _mint_artifact(app, artifact)

    results = asyncio.run(search_workspace_references(app, "ws_a"))

    assert {row["kind"] for row in results} == {
        "workspace_file",
        "resource",
        "artifact",
        "session",
        "agent_run",
    }
    assert all(
        set(row) == {"kind", "id", "label", "detail", "media_type", "revision", "navigation"}
        for row in results
    )
    duplicate_files = [
        row
        for row in results
        if row["kind"] == "workspace_file" and row["label"] == "observations.csv"
    ]
    assert {row["id"] for row in duplicate_files} == {
        "one/observations.csv",
        "two/observations.csv",
    }
    assert all(row["id"] in row["detail"] for row in duplicate_files)
    assert next(row for row in results if row["kind"] == "artifact")["id"] == artifact_id


def test_reference_route_accepts_csv_kinds_and_query(tmp_path: Path) -> None:
    owner = _app(tmp_path)
    target = tmp_path / "a" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("notes", encoding="utf-8")
    route_app = FastAPI()
    for key, value in owner.state.__dict__.items():
        setattr(route_app.state, key, value)
    register_reference_routes(route_app)

    with TestClient(route_app) as client:
        response = client.get(
            "/v1/workspaces/ws_a/references",
            params={"q": "notes", "kinds": "workspace_file,session"},
        )

    assert response.status_code == 200
    assert response.json()["references"] == [
        next(
            row
            for row in asyncio.run(
                search_workspace_references(
                    owner, "ws_a", query="notes", kinds=["workspace_file", "session"]
                )
            )
        )
    ]


def test_file_authorization_overwrites_display_fields_and_records_actual_hash(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    target = tmp_path / "a" / "data" / "truth.txt"
    target.parent.mkdir(parents=True)
    target.write_text("authoritative content", encoding="utf-8")
    session = app.state.sessions.get("sess_a")
    assert session is not None
    requested = Part(
        type="context_ref",
        ref_kind="workspace_file",
        ref_id="data/truth.txt",
        label="client lie",
        metadata={"workspace_id": "ws_b"},
    )

    resolved = asyncio.run(authorize_context_reference_parts(app, session, [requested]))[0]

    expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert resolved.label == "truth.txt"
    assert resolved.revision == f"sha256:{expected_hash}"
    metadata = resolved.metadata["context_reference"]
    assert metadata["workspace_id"] == "ws_a"
    assert metadata["delivery"]["sha256"] == expected_hash
    assert metadata["provenance"]["path"] == "data/truth.txt"


def test_file_reference_reports_typed_stale_and_inaccessible_failures(tmp_path: Path) -> None:
    app = _app(tmp_path)
    target = tmp_path / "a" / "truth.txt"
    target.parent.mkdir(parents=True)
    target.write_text("new", encoding="utf-8")
    session = app.state.sessions.get("sess_a")
    assert session is not None

    with pytest.raises(HTTPException) as stale:
        asyncio.run(
            authorize_context_reference_parts(
                app,
                session,
                [
                    Part(
                        type="context_ref",
                        ref_kind="workspace_file",
                        ref_id="truth.txt",
                        revision="sha256:old",
                    )
                ],
            )
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["error"]["error"] == "context_ref_stale"

    with pytest.raises(HTTPException) as inaccessible:
        asyncio.run(
            authorize_context_reference_parts(
                app,
                session,
                [
                    Part(
                        type="context_ref",
                        ref_kind="session",
                        ref_id="sess_other",
                        label="Private",
                    )
                ],
            )
        )
    assert inaccessible.value.status_code == 403
    assert inaccessible.value.detail["error"]["error"] == "context_ref_inaccessible"


def test_artifact_revision_is_pinned_and_verified(tmp_path: Path) -> None:
    app = _app(tmp_path)
    artifact = tmp_path / "a" / "report.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("verified", encoding="utf-8")
    artifact_id = _mint_artifact(app, artifact)
    session = app.state.sessions.get("sess_a")
    assert session is not None

    resolved = asyncio.run(
        authorize_context_reference_parts(
            app,
            session,
            [
                Part(
                    type="context_ref",
                    ref_kind="artifact",
                    ref_id=artifact_id,
                    revision="v1",
                    label="spoofed",
                )
            ],
        )
    )[0]
    assert resolved.label == "report.md"
    assert resolved.revision == "v1"
    assert resolved.metadata["context_reference"]["provenance"]["producer"]["call_id"] == "call_1"

    with pytest.raises(HTTPException) as stale:
        asyncio.run(
            authorize_context_reference_parts(
                app,
                session,
                [
                    Part(
                        type="context_ref",
                        ref_kind="artifact",
                        ref_id=artifact_id,
                        revision="v2",
                    )
                ],
            )
        )
    assert stale.value.detail["error"]["error"] == "context_ref_stale"


def test_session_and_agent_run_deliver_only_bounded_summaries(tmp_path: Path) -> None:
    app = _app(tmp_path)
    session = app.state.sessions.get("sess_a")
    assert session is not None
    parts = asyncio.run(
        authorize_context_reference_parts(
            app,
            session,
            [
                Part(type="context_ref", ref_kind="session", ref_id="sess_a"),
                Part(type="context_ref", ref_kind="agent_run", ref_id="task_1"),
            ],
        )
    )
    message = Message(
        id="msg_context",
        session_id="sess_a",
        role="user",
        created_at="2026-09-02T12:00:00+00:00",
        updated_at="2026-09-02T12:00:00+00:00",
        parts=parts,
    )

    enriched = enrich_with_context_references(app, "sess_a", "compare", message)

    session_summary = parts[0].metadata["context_reference"]["delivery"]["summary"]
    assert len(session_summary["recent"]) == 5
    assert all(len(row["excerpt"]) <= 600 for row in session_summary["recent"])
    assert "message 0" not in enriched
    assert "bounded result" in enriched
    assert "provenance" in enriched


def test_typed_parts_survive_message_builder_and_busy_queue(tmp_path: Path) -> None:
    app = _app(tmp_path)
    source = Part(
        id="part_context",
        type="context_ref",
        ref_kind="session",
        ref_id="sess_b",
        label="Shared title",
        revision="r1",
    )
    parts = _user_message_parts(request_parts=[source], user_text="")

    enqueue_user_steer(
        app,
        "sess_a",
        "",
        steer_message_id="msg_queued",
        steer_parts=parts,
        model_text="server resolved summary",
    )

    event = app.state.loop_inboxes["sess_a"].drain()[0]
    assert event.steer_parts[0].type == "context_ref"
    assert event.steer_parts[0].ref_id == "sess_b"
    assert event.model_text == "server resolved summary"


def test_normal_submission_and_retry_preserve_context_ref(
    tmp_path: Path, host_agent_executor: object
) -> None:
    del host_agent_executor
    target = tmp_path / "workspace" / "source.txt"
    target.parent.mkdir(parents=True)
    target.write_text("source context", encoding="utf-8")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    app.state.workspaces.update("ws_default", root_path=str(target.parent))

    with TestClient(app) as client:
        session_id = client.post("/v1/sessions", json={"title": "Context"}).json()["id"]
        accepted = client.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "parts": [
                    {
                        "type": "context_ref",
                        "ref_kind": "workspace_file",
                        "ref_id": "source.txt",
                        "label": "untrusted",
                    }
                ]
            },
        )
        assert accepted.status_code == 200, accepted.text
        source_message_id = accepted.json()["message_id"]

        deadline = time.monotonic() + 30
        while app.state.turn_runner.busy(session_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not app.state.turn_runner.busy(session_id)

        retry = client.post(
            f"/v1/sessions/{session_id}/messages/{source_message_id}/retry",
            json={"execute": True},
        )
        assert retry.status_code == 202, retry.text
        queued_id = retry.json()["metadata"]["queued_user_message_id"]
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        source = next(message for message in messages if message["id"] == source_message_id)
        queued = next(message for message in messages if message["id"] == queued_id)
        source_ref = next(part for part in source["parts"] if part["type"] == "context_ref")
        queued_ref = next(part for part in queued["parts"] if part["type"] == "context_ref")

        assert source_ref["label"] == "source.txt"
        assert queued_ref["ref_id"] == "source.txt"
        assert queued_ref["revision"] == source_ref["revision"]
        assert queued_ref["id"] != source_ref["id"]
