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

from clio_agent.gact import context as gact_context
from clio_agent.gact import context_references as context_reference_module
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
from clio_agent.gact.loop_inbox import (
    drain_active_session_inbox,
    enqueue_user_steer,
)
from clio_agent.gact.messaging import _user_message_parts
from clio_agent.gact.routes.references import register_reference_routes
from clio_agent.gact.runtime.constants import _CTX_MAX_BYTES
from clio_agent.gact.runtime.globals import _ContextFileAccessError, _gact_app_context
from clio_agent.gact.types import Message, Part
from tests.test_gact.test_post_messages import SlowClioAgent


class _Agent:
    def forward(self, question: str, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(answer="ok", selected_expert="", routing_rationale="")


def test_context_reference_capability_is_explicitly_enabled(tmp_path: Path) -> None:
    """The web client can enable the picker from negotiated server truth."""

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    capability = client.get("/v1/capabilities", headers={"X-GACT-Version": "0.3"}).json()[
        "capabilities"
    ]["x_clio_context_references"]

    assert capability["enabled"] is True
    assert capability["part_type"] == "context_ref"


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
            ),
            SimpleNamespace(
                id="res_2",
                name="observations.csv",
                revision="rev_8",
                detected_mime="text/csv",
            ),
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
    registry.register(
        AgentTask(
            task_id="task_2",
            parent_session_id="sess_a",
            child_session_id="sess_b",
            agent_ref={"expert_id": "analysis"},
            run_label="Analysis run",
            status="failed",
            error_reason="failure " + "z" * 900,
            created_at="2026-09-02T11:30:00+00:00",
            updated_at="2026-09-02T12:30:00+00:00",
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


def _mint_artifact(
    app: SimpleNamespace,
    path: Path,
    *,
    workspace_id: str = "ws_a",
    event_id: str = "event_1",
) -> str:
    data = path.read_bytes()
    outcome = app.state.artifact_registry.mint(
        workspace_id=workspace_id,
        name=path.name,
        event_id=event_id,
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
    for path, body in (
        (first, "one"),
        (second, "two"),
        (artifact, "report"),
    ):
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
    assert any(row["kind"] == "artifact" and row["id"] == artifact_id for row in results)
    for kind, label in (
        ("resource", "observations.csv"),
        ("session", "Shared title"),
        ("agent_run", "Analysis run"),
    ):
        duplicates = [row for row in results if row["kind"] == kind and row["label"] == label]
        assert len(duplicates) == 2
        assert all(row["id"] in row["detail"] for row in duplicates)
    duplicate_artifacts = context_reference_module._disambiguate_duplicate_labels(
        [
            {
                "kind": "artifact",
                "id": artifact_ref,
                "label": "report.md",
                "detail": "report.md v1 (report)",
                "media_type": "text/markdown",
                "revision": "v1",
                "navigation": {},
            }
            for artifact_ref in ("artifact_a", "artifact_b")
        ]
    )
    assert all(row["id"] in row["detail"] for row in duplicate_artifacts)


def test_empty_reference_search_is_bounded_without_hiding_deep_matches(tmp_path: Path) -> None:
    app = _app(tmp_path)
    workspace = tmp_path / "a"
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(25):
        (workspace / f"file-{index:02d}.txt").write_text(str(index), encoding="utf-8")
    deep_match = workspace / "zz-deep-match.txt"
    deep_match.write_text("found", encoding="utf-8")

    initial = asyncio.run(
        search_workspace_references(app, "ws_a", kinds=["workspace_file"])
    )
    searched = asyncio.run(
        search_workspace_references(
            app,
            "ws_a",
            query="deep-match",
            kinds=["workspace_file"],
        )
    )

    assert len(initial) == 20
    assert [row["id"] for row in searched] == ["zz-deep-match.txt"]


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
    failed_run = asyncio.run(
        authorize_context_reference_parts(
            app,
            session,
            [Part(type="context_ref", ref_kind="agent_run", ref_id="task_2")],
        )
    )[0]
    failure_summary = failed_run.metadata["context_reference"]["delivery"]["summary"]
    assert len(failure_summary["error_reason"]) <= 600


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

        deliveries = source["metadata"]["context_reference_deliveries"]
        assert deliveries == [source_ref["metadata"]["context_reference"]]
        persisted = app.state.message_store.load_session(session_id)
        assert persisted is not None
        persisted_source = next(message for message in persisted if message.id == source_message_id)
        assert persisted_source.metadata["context_reference_deliveries"] == deliveries
        frame = next(
            frame
            for frame in app.state.context_frames[session_id]
            if frame["user_message_id"] == source_message_id
        )
        frame_reference = next(item for item in frame["items"] if item["kind"] == "context_ref")
        assert frame_reference["metadata"]["delivery"] == deliveries[0]["delivery"]


def test_composer_busy_context_only_steer_is_canonical_and_model_ready(
    tmp_path: Path, host_agent_executor: object
) -> None:
    del host_agent_executor
    target = tmp_path / "workspace" / "steer.txt"
    target.parent.mkdir(parents=True)
    target.write_text("authoritative steer context", encoding="utf-8")
    agent = SlowClioAgent(delay_s=1.0)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    app.state.workspaces.update("ws_default", root_path=str(target.parent))

    with TestClient(app) as client:
        session_id = client.post("/v1/sessions", json={"title": "Busy context"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{session_id}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 3
        while not app.state.turn_runner.busy(session_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert app.state.turn_runner.busy(session_id)

        steer = client.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "delivery": "steer",
                "parts": [
                    {
                        "type": "context_ref",
                        "ref_kind": "workspace_file",
                        "ref_id": "steer.txt",
                        "label": "spoofed label",
                    }
                ],
            },
        )
        assert steer.status_code == 202, steer.text

        with _gact_app_context(app):
            token = gact_context.set_session_id(session_id)
            try:
                block = drain_active_session_inbox(app)
            finally:
                gact_context.reset(token)
        assert "## Structured context references (server-resolved)" in block
        assert "authoritative steer context" in block

        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        persisted = next(row for row in messages if row["id"] == steer.json()["message_id"])
        reference = next(part for part in persisted["parts"] if part["type"] == "context_ref")
        assert reference["label"] == "steer.txt"
        assert reference["revision"].startswith("sha256:")
        assert persisted["metadata"]["context_reference_deliveries"][0]["ref_id"] == "steer.txt"


def test_queue_context_refs_authorize_edit_reorder_and_retain_stale_promotion(
    tmp_path: Path, host_agent_executor: object
) -> None:
    del host_agent_executor
    target = tmp_path / "workspace" / "queued.txt"
    target.parent.mkdir(parents=True)
    target.write_text("first revision", encoding="utf-8")
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=SlowClioAgent(delay_s=2.0),
    )
    app.state.workspaces.update("ws_default", root_path=str(target.parent))

    with TestClient(app) as client:
        session_id = client.post("/v1/sessions", json={"title": "Queued context"}).json()["id"]
        # Queue rows are intentionally a while-busy affordance. Hold the active
        # slot so this test can exercise edit, reorder, and stale promotion
        # before the idle auto-dispatch lifecycle consumes the head row.
        started = client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"parts": [{"type": "text", "text": "hold the slot"}]},
        )
        assert started.status_code == 200, started.text
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not app.state.turn_runner.busy(session_id):
            time.sleep(0.02)
        assert app.state.turn_runner.busy(session_id)

        created = client.post(
            f"/v1/sessions/{session_id}/queued-messages",
            json={
                "client_message_id": "queued_context",
                "idempotency_key": "queued_context",
                "parts": [
                    {
                        "type": "context_ref",
                        "ref_kind": "workspace_file",
                        "ref_id": "queued.txt",
                        "label": "untrusted",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        queued = created.json()
        reference = queued["parts"][0]
        assert reference["label"] == "queued.txt"
        assert reference["revision"].startswith("sha256:")

        updated = client.patch(
            f"/v1/sessions/{session_id}/queued-messages/{queued['id']}",
            json={
                "revision": queued["revision"],
                "parts": [
                    {"type": "text", "text": "Use the queued context"},
                    {**reference, "label": "spoofed again"},
                ],
            },
        )
        assert updated.status_code == 200, updated.text
        queued = updated.json()
        assert queued["parts"][1]["label"] == "queued.txt"

        second = client.post(
            f"/v1/sessions/{session_id}/queued-messages",
            json={"text": "second row", "client_message_id": "queued_second"},
        ).json()
        reordered = client.post(
            f"/v1/sessions/{session_id}/queued-messages/reorder",
            json={
                "ordered_ids": [second["id"], queued["id"]],
                "revisions": {
                    second["id"]: second["revision"],
                    queued["id"]: queued["revision"],
                },
            },
        )
        assert reordered.status_code == 200, reordered.text
        reordered_context = reordered.json()["queued_messages"][1]
        assert reordered_context["parts"] == queued["parts"]

        target.write_text("changed after queueing", encoding="utf-8")
        promoted = client.post(
            f"/v1/sessions/{session_id}/queued-messages/{queued['id']}/promote",
            json={"revision": reordered_context["revision"], "delivery": "auto"},
        )
        assert promoted.status_code == 409, promoted.text
        assert promoted.json()["error"]["error"] == "context_ref_stale"
        remaining = client.get(f"/v1/sessions/{session_id}/queued-messages").json()[
            "queued_messages"
        ]
        assert any(row["id"] == queued["id"] for row in remaining)


def test_context_only_busy_steer_idle_redrive_preserves_parts_and_model_text(
    tmp_path: Path, host_agent_executor: object
) -> None:
    del host_agent_executor
    target = tmp_path / "workspace" / "redrive.txt"
    target.parent.mkdir(parents=True)
    target.write_text("idle redrive context", encoding="utf-8")
    agent = SlowClioAgent(delay_s=0.35)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    app.state.workspaces.update("ws_default", root_path=str(target.parent))

    with TestClient(app) as client:
        session_id = client.post("/v1/sessions", json={"title": "Idle redrive"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{session_id}/messages",
                json={"parts": [{"type": "text", "text": "first"}]},
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 3
        while not app.state.turn_runner.busy(session_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert app.state.turn_runner.busy(session_id)
        steer = client.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "delivery": "steer",
                "parts": [
                    {
                        "type": "context_ref",
                        "ref_kind": "workspace_file",
                        "ref_id": "redrive.txt",
                    }
                ],
            },
        )
        assert steer.status_code == 202, steer.text

        deadline = time.monotonic() + 5
        while len(agent.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(agent.calls) >= 2
        assert "idle redrive context" in agent.calls[1][0]
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        redriven = next(row for row in messages if row["id"] == steer.json()["message_id"])
        reference = next(part for part in redriven["parts"] if part["type"] == "context_ref")
        assert reference["ref_id"] == "redrive.txt"
        assert reference["revision"].startswith("sha256:")


def test_delivery_reads_bounded_bytes_and_rejects_hash_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    target = tmp_path / "a" / "large.txt"
    artifact_path = tmp_path / "a" / "artifact-large.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"a" * (_CTX_MAX_BYTES + 1024))
    artifact_path.write_bytes(b"b" * (_CTX_MAX_BYTES + 1024))
    artifact_id = _mint_artifact(app, artifact_path, event_id="event_large")
    session = app.state.sessions.get("sess_a")
    assert session is not None
    parts = asyncio.run(
        authorize_context_reference_parts(
            app,
            session,
            [
                Part(type="context_ref", ref_kind="workspace_file", ref_id="large.txt"),
                Part(
                    type="context_ref",
                    ref_kind="artifact",
                    ref_id=artifact_id,
                    revision="v1",
                ),
            ],
        )
    )
    message = Message(
        id="msg_large",
        session_id="sess_a",
        role="user",
        created_at="2026-09-02T12:00:00+00:00",
        updated_at="2026-09-02T12:00:00+00:00",
        parts=parts,
    )

    enriched = enrich_with_context_references(app, "sess_a", "inspect", message)
    assert enriched.count("1024 more bytes truncated") == 2
    assert len(enriched) < (_CTX_MAX_BYTES * 2) + 2000

    original_read = context_reference_module._read_bounded_file

    def changed_read(path: Path) -> object:
        if path == target:
            path.write_text("changed during delivery", encoding="utf-8")
        return original_read(path)

    monkeypatch.setattr(context_reference_module, "_read_bounded_file", changed_read)
    with pytest.raises(_ContextFileAccessError) as stale:
        enrich_with_context_references(app, "sess_a", "inspect", message)
    assert stale.value.error_info.error == "context_ref_stale"


def test_context_reference_errors_cover_invalid_missing_revision_and_ownership(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    session = app.state.sessions.get("sess_a")
    assert session is not None
    artifact_path = tmp_path / "a" / "pinned.md"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("pinned", encoding="utf-8")
    artifact_id = _mint_artifact(app, artifact_path, event_id="event_pinned")
    private_file = tmp_path / "b" / "private.txt"
    private_file.parent.mkdir(parents=True, exist_ok=True)
    private_file.write_text("private", encoding="utf-8")

    cases = [
        (
            Part(type="context_ref", ref_kind="unknown", ref_id="x"),
            400,
            "context_ref_kind_invalid",
        ),
        (Part(type="context_ref", ref_kind="session", ref_id=""), 400, "context_ref_invalid"),
        (
            Part(type="context_ref", ref_kind="artifact", ref_id=artifact_id),
            400,
            "context_ref_revision_required",
        ),
        (
            Part(type="context_ref", ref_kind="workspace_file", ref_id="../b/private.txt"),
            403,
            "context_ref_inaccessible",
        ),
    ]
    for part, status_code, error in cases:
        with pytest.raises(HTTPException) as failure:
            asyncio.run(authorize_context_reference_parts(app, session, [part]))
        assert failure.value.status_code == status_code
        assert failure.value.detail["error"]["error"] == error
