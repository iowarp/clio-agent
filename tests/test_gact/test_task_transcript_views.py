"""Public task reads over real retained transcripts, grants and artifact custody."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.task_types import CoordinationGrant, MailAdmission, MailItem, TaskSnapshot
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.records import ArtifactKind
from clio_agent.gact.native_work import now
from clio_agent.gact.summarization_record import summarization_part
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.types import Message, Part
from tests.test_gact.task_records import (
    active_turn_fixture,
    record_assignment_fixture,
    record_delegation_fixture,
)


@dataclass
class TranscriptWorld:
    """Actual application and recorded independent session identities."""

    app: Any
    client: TestClient
    reader: str
    target: str
    task: TaskSnapshot
    root: Path

    @property
    def caller(self) -> TaskCaller:
        """An agent identity, without the operator HTTP privilege."""
        return TaskCaller(self.reader, self.app.state.human_principal)

    def grant(
        self, rights: tuple[Any, ...] = ("read", "observe", "message"), *, exact: bool = False
    ) -> CoordinationGrant:
        """Record the same trusted relationship the authenticated human creates."""
        grant = CoordinationGrant(
            id="grant_" + uuid4().hex,
            granting_human=self.app.state.human_principal,
            recipient_session=self.reader,
            target_session=self.target,
            rights=rights,
            created_at=now(),
            target_handle=self.task.handle if exact else None,
        )
        self.app.state.task_history.record_grant(grant, observation_id=uuid4().hex)
        return grant

    def observe(self, handle: str | list[str] | None = None, **kwargs: Any) -> Any:
        """Use the production async operation on its application loop."""
        return asyncio.run_coroutine_threadsafe(
            self.app.state.task_operations.observe(
                self.caller, handle or self.task.handle, **kwargs
            ),
            self.app.state.conversations._loop,
        ).result(timeout=10)

    def message(self, text: str, *, identity: str = "message_original") -> Message:
        """Persist an attributed source row through the real message owner."""
        message = Message(
            id=identity,
            session_id=self.target,
            role="assistant",
            created_at=now(),
            updated_at=now(),
            parts=[Part(id="part_" + identity, type="text", text=text)],
        )
        existing = list(self.app.state.messages.get(self.target, ()))
        self.app.state.messages[self.target] = [*existing, message]
        return message


@pytest.fixture
def transcript_world(tmp_path: Path) -> Iterator[TranscriptWorld]:
    """Use real Core-backed task/message owners and no model double."""
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    with TestClient(app) as client:
        root = tmp_path / "workspace"
        root.mkdir()
        response = client.post(
            "/v1/workspaces", json={"name": "Task transcript tests", "root_path": str(root)}
        )
        assert response.status_code in {200, 201}, response.text
        workspace = response.json()["id"]
        reader = app.state.sessions.create(title="Reader", workspace_id=workspace).id
        target = app.state.sessions.create(title="Independent analyst", workspace_id=workspace).id
        record_assignment_fixture(app, reader)
        task = record_assignment_fixture(app, target, description="Analyze transport measurements")
        yield TranscriptWorld(app, client, reader, target, task, root)


def test_query_discovers_transcript_text_after_authorization(
    transcript_world: TranscriptWorld,
) -> None:
    """A result comes from real older text; a description-only match cannot substitute."""
    world = transcript_world
    world.message("The approved calibration is 17.25 seconds.")
    assert not world.app.state.task_operations.query(
        world.caller, scope="authorized", query="calibration"
    )["tasks"]
    world.grant()
    result = world.app.state.task_operations.query(
        world.caller, scope="authorized", query="calibration"
    )
    assert [row["handle"] for row in result["tasks"]] == [world.task.handle]
    assert result["total"] is None


def test_observe_only_does_not_disclose_or_search_transcript(
    transcript_world: TranscriptWorld,
) -> None:
    """Safe status access conveys no text-search oracle or source body rights."""
    world = transcript_world
    world.message("Private calibration 17.25")
    world.grant(("observe",))
    found = world.app.state.task_operations.query(
        world.caller, scope="authorized", query="calibration"
    )
    assert not found["tasks"]
    peek = world.observe(view="transcript")
    assert peek["tasks"][0].get("error")
    assert "17.25" not in json.dumps(peek)


def test_transcript_pages_reconstruct_large_typed_messages(
    transcript_world: TranscriptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every original Unicode character and typed part survives bounded paging."""
    world = transcript_world
    world.grant()
    source = world.message("α calibration " * 900)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "700")
    cursor, fragments = None, []
    for _ in range(100):
        result = world.observe(view="transcript", cursor=cursor, limit=1)
        row = result["tasks"][0]
        assert "error" not in row, row
        fragment = row["items"][0]
        assert fragment["encoding"] == "json"
        assert len(fragment["content"]) <= 700
        fragments.append(fragment["content"])
        cursor = result["cursor"]
        if not result["has_more"]:
            break
    else:
        pytest.fail("Transcript continuation did not finish")
    assert json.loads("".join(fragments)) == source.to_wire()
    assert result.notification_ids == () and result.collect is False
    assert world.task.handle in result.read_handles


def test_task_transcript_route_and_native_operation_share_content(
    transcript_world: TranscriptWorld,
) -> None:
    """The operator route and authorized agent read use one source/paging contract."""
    world = transcript_world
    world.grant()
    message = world.message("Original evidence")
    direct = world.observe(view="transcript")
    route = world.client.post(
        f"/v1/sessions/{world.reader}/tasks/observe",
        json={"tasks": world.task.handle, "view": "transcript"},
    )
    assert route.status_code == 200, route.text
    assert route.json()["tasks"][0]["items"] == direct["tasks"][0]["items"] == [message.to_wire()]


def test_revocation_prevents_continued_transcript_disclosure(
    transcript_world: TranscriptWorld,
) -> None:
    """An already-issued cursor grants no rights after the human revokes access."""
    world = transcript_world
    grant = world.grant()
    world.message("First source")
    world.message("Second source", identity="second")
    first = world.observe(view="transcript", limit=1)
    assert first["has_more"]
    world.app.state.task_history.record_grant(
        grant.model_copy(update={"revoked_at": now()}), observation_id=uuid4().hex
    )
    after = world.observe(view="transcript", cursor=first["cursor"])
    assert after["tasks"][0].get("error")
    assert "Second source" not in json.dumps(after)


def test_rewritten_transcript_cursor_is_not_silently_retargeted(
    transcript_world: TranscriptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed source prefix requires an explicit fresh read."""
    world = transcript_world
    world.grant()
    world.message("Original " * 2000)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "700")
    first = world.observe(view="transcript")
    original = world.app.state.messages[world.target][0]
    world.app.state.messages[world.target] = [
        original.model_copy(update={"parts": [Part(type="text", text="Changed source")]})
    ]
    result = world.observe(view="transcript", cursor=first["cursor"])
    assert "source changed" in result["tasks"][0]["error"]


def test_summary_exposes_recorded_model_text_and_partial_coverage(
    transcript_world: TranscriptWorld,
) -> None:
    """A recorded compaction is usable with provenance, not mislabeled as full history."""
    world = transcript_world
    world.grant()
    message = world.message("Recent activity")
    part = summarization_part(
        "Calibration decision: retain the 17.25-second exposure.",
        trigger="manual",
        compaction_id="cmp_actual",
        derived_from=["step_1"],
        compacted_message_ids=["message_early"],
    )
    world.app.state.messages[world.target] = [message.model_copy(update={"parts": [part]})]
    result = world.observe(view="summary")
    row = result["tasks"][0]
    assert row["summary_available"] is True
    assert row["items"][0]["text"] == part.text
    assert row["items"][0]["covered_message_ids"] == ["message_early"]
    assert row["items"][0]["whole_session"] is False


def test_absent_summary_is_explicit(transcript_world: TranscriptWorld) -> None:
    """Recent source prose is never presented as a generated summary."""
    transcript_world.grant()
    transcript_world.message("Ordinary recent text")
    row = transcript_world.observe(view="summary")["tasks"][0]
    assert row["summary_available"] is False and row["items"] == []


def test_assignment_view_does_not_follow_a_successor(transcript_world: TranscriptWorld) -> None:
    """A failed run's handle remains pinned when the stable session gets new work."""
    world = transcript_world
    world.grant()
    original = world.message("Failure evidence to inspect before continuing")
    ended = now()
    world.app.state.task_history.record_snapshot(
        world.task.model_copy(
            update={
                "status": "failed",
                "raw_status": "failed",
                "terminal_at": ended,
                "updated_at": ended,
            }
        ),
        observation_id="actual-recorded-failure",
    )
    successor = record_assignment_fixture(
        world.app, world.target, description="Continue after inspection"
    )
    world.message("Successor evidence", identity="successor_message")
    historical = world.observe(view="transcript")
    assert historical["tasks"][0]["items"] == [original.to_wire()]
    session = world.observe(view="transcript", history="session")
    assert len(session["tasks"][0]["items"]) == 2
    assert {world.task.handle, successor.handle} <= set(session.read_handles)


def _registered_report(
    world: TranscriptWorld,
    producer: str,
    *,
    payload: bytes | None = None,
    name: str = "report.txt",
    kind: ArtifactKind = ArtifactKind.REPORT,
) -> tuple[Any, bytes]:
    """Mint and ingest real bytes through the selected artifact owner."""
    from clio_agent.gact.artifacts.records import Mechanism
    from clio_agent.gact.artifacts.registry import get_registry
    from clio_agent.gact.artifacts.storage import ingest_artifact_identity

    payload = (
        payload
        if payload is not None
        else ("Verified calibration: 17.25 seconds. αβ\n" * 120).encode()
    )
    path = world.root / name
    path.write_bytes(payload)
    ingested = ingest_artifact_identity(world.app, path, workspace_root=world.root)
    workspace = world.app.state.sessions.get(world.target).workspace_id
    outcome = get_registry(world.app).mint(
        workspace_id=workspace,
        name=name,
        event_id="event_" + uuid4().hex,
        kind=kind,
        custody=ingested.custody,
        mechanism=Mechanism.TOOL_SCHEMA,
        evidence=ingested.evidence,
        producer={"session_id": producer, "tool": "create_artifact"},
        path=str(path),
        created_at=now(),
        annotation="Original evidence",
    )
    message = world.message("", identity="report_link")
    world.app.state.messages[world.target] = [
        message.model_copy(
            update={
                "parts": [
                    Part(
                        id="registered_report",
                        type="resource_link",
                        server_id="clio-artifacts",
                        name=name,
                        metadata={"artifact_id": outcome.version.artifact_id},
                    )
                ]
            }
        )
    ]
    return outcome.version, payload


def test_linked_artifact_pages_return_original_verified_bytes(
    transcript_world: TranscriptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permitted peer gets exact UTF-8 bytes through shared artifact custody."""
    world = transcript_world
    world.grant()
    version, payload = _registered_report(world, world.target)
    listed = world.observe(view="artifacts")
    assert listed["tasks"][0]["items"][0]["artifact_id"] == version.artifact_id
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "701")
    cursor, received = None, ""
    for _ in range(30):
        result = world.observe(view="artifacts", artifact_id=version.artifact_id, cursor=cursor)
        row = result["tasks"][0]
        assert "error" not in row, row
        body = row["items"][0]
        assert body["sha256"] == hashlib.sha256(payload).hexdigest()
        assert body["encoding"] == "utf-8"
        received += body["content"]
        cursor = result["cursor"]
        if not result["has_more"]:
            break
    else:
        pytest.fail("Artifact continuation did not finish")
    assert received.encode() == payload
    assert result.collect is False


def test_forged_artifact_link_does_not_grant_another_producers_bytes(
    transcript_world: TranscriptWorld,
) -> None:
    """A source transcript cannot manufacture permission by naming another artifact ID."""
    world = transcript_world
    world.grant()
    version, _ = _registered_report(world, "ungranted_session")
    assert world.observe(view="artifacts")["tasks"][0]["items"] == [
        {"id": version.artifact_id, "artifact_id": version.artifact_id, "available": False}
    ]
    result = world.observe(view="artifacts", artifact_id=version.artifact_id)
    assert result["tasks"][0].get("error")
    assert "17.25 seconds" not in json.dumps(result)


def test_live_growth_and_sealing_do_not_rewrite_the_history_cursor(
    transcript_world: TranscriptWorld,
) -> None:
    """A busy peek remains mutable while retained history has a stable continuation."""
    world = transcript_world
    world.grant()
    retained = world.message("Previously retained evidence")
    live = Part(id="live_progress", type="text", text="First live update")
    world.app.state.live_assistant_message_ids = {world.target: "current_frame"}
    world.app.state.live_assistant_parts = {world.target: [live]}
    first = world.observe(view="transcript", limit=1)
    assert first["tasks"][0]["items"] == [retained.to_wire()]
    assert first["tasks"][0]["live_snapshot"]["parts"][0]["text"] == live.text
    grown = live.model_copy(update={"text": "First live update, now with more progress"})
    world.app.state.live_assistant_parts[world.target] = [grown]
    second = world.observe(view="transcript", cursor=first["cursor"], limit=1)
    assert second["tasks"][0]["items"] == []
    assert second["tasks"][0]["live_snapshot"]["parts"][0]["text"] == grown.text
    sealed = Message(
        id="current_frame",
        session_id=world.target,
        turn_id=world.task.invocation_id,
        role="assistant",
        created_at=now(),
        updated_at=now(),
        parts=[grown],
    )
    world.app.state.messages[world.target] = [retained, sealed]
    world.app.state.live_assistant_message_ids.clear()
    world.app.state.live_assistant_parts.clear()
    final = world.observe(view="transcript", cursor=second["cursor"], limit=1)
    assert final["tasks"][0]["items"] == [sealed.to_wire()]
    assert final["tasks"][0]["live_snapshot"] is None
    assert final.collect is False


def test_live_preview_keeps_the_existing_output_bound(
    transcript_world: TranscriptWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A growing model frame cannot bypass the configured content budget."""
    world = transcript_world
    world.grant()
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "128")
    world.app.state.live_assistant_message_ids = {world.target: "current_frame"}
    world.app.state.live_assistant_parts = {
        world.target: [Part(id="large_live_part", type="text", text="progress " * 1000)]
    }
    preview = world.observe(view="transcript")["tasks"][0]["live_snapshot"]
    assert preview["mutable"] is True and preview["truncated"] is True
    assert sum(len(part["content"]) for part in preview["parts"]) <= 128


def test_recorded_thinking_has_the_same_read_boundary_as_other_transcript_content(
    transcript_world: TranscriptWorld,
) -> None:
    """Status access cannot disclose recorded reasoning through search or a peek."""
    world = transcript_world
    world.grant(("observe",))
    message = world.message("Public-looking source")
    secret = "PRIVATE_RECORDED_REASONING_481"
    world.app.state.messages[world.target] = [
        message.model_copy(
            update={"parts": [Part(id="recorded_reasoning", type="thinking", text=secret)]}
        )
    ]
    denied = world.observe(view="transcript")
    assert denied["tasks"][0].get("error") and secret not in json.dumps(denied)
    queried = world.app.state.task_operations.query(world.caller, scope="authorized", query=secret)
    assert queried["tasks"] == []
    world.grant()
    allowed = world.observe(view="transcript")["tasks"][0]["items"][0]
    assert allowed["parts"][0]["type"] == "thinking"
    assert allowed["parts"][0]["text"] == secret


def test_multiple_live_previews_share_the_existing_output_budget(
    transcript_world: TranscriptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting more active handles cannot multiply the global content allowance."""
    world = transcript_world
    world.grant()
    peer = world.app.state.sessions.create(
        title="Second analyst",
        workspace_id=world.app.state.sessions.get(world.target).workspace_id,
    ).id
    peer_task = record_assignment_fixture(world.app, peer)
    TranscriptWorld(world.app, world.client, world.reader, peer, peer_task, world.root).grant()
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "128")
    world.app.state.live_assistant_message_ids = {
        world.target: "first_live_frame",
        peer: "second_live_frame",
    }
    world.app.state.live_assistant_parts = {
        sid: [Part(type="text", text="progress " * 1000)] for sid in (world.target, peer)
    }
    observed = world.observe(handle=[world.task.handle, peer_task.handle], view="transcript")
    assert all(not row.get("error") for row in observed["tasks"]), observed
    previews = [row["live_snapshot"] for row in observed["tasks"]]
    assert all(preview["truncated"] for preview in previews)
    assert 0 < sum(len(part["content"]) for preview in previews for part in preview["parts"]) <= 128


def test_legacy_activity_alias_does_not_reveal_unauthorized_session_existence(
    transcript_world: TranscriptWorld,
) -> None:
    """Missing and forbidden targets reach the same authorization boundary first."""
    from clio_agent.gact.routes.session_recall import _recall_session_summary

    world = transcript_world
    failures = []
    for target in (world.target, "sess_missing_ungranted"):
        with pytest.raises(ValueError) as denied:
            _recall_session_summary(world.app, target, caller_id=world.reader)
        failures.append(str(denied.value))
    assert failures[0] == failures[1]


def test_retained_exact_grant_does_not_expand_to_session_history(
    transcript_world: TranscriptWorld,
) -> None:
    """An existing exact-run grant cannot become authority over predecessor/successor history."""
    world = transcript_world
    world.grant(exact=True)
    source = world.message("Authorized finite-run evidence")
    assert world.observe(view="transcript")["tasks"][0]["items"] == [source.to_wire()]
    result = world.observe(view="transcript", history="session")
    assert result["tasks"][0].get("error")
    assert "Authorized finite-run evidence" not in json.dumps(result)


def test_artifact_corruption_uses_the_existing_integrity_failure(
    transcript_world: TranscriptWorld,
) -> None:
    """The task view cannot bypass the hash-verified HTTP/provider read ladder."""
    from clio_agent.gact.artifacts.cas import CASStore

    world = transcript_world
    world.grant()
    version, _ = _registered_report(world, world.target)
    assert world.observe(view="artifacts", artifact_id=version.artifact_id)["tasks"][0]["items"]
    CASStore(world.root).blob_path(version.sha256).write_bytes(b"Corrupt replacement")
    result = world.observe(view="artifacts", artifact_id=version.artifact_id)
    row = result["tasks"][0]
    assert row["status_code"] == 409
    assert "integrity_violation" in json.dumps(row["error"])
    assert "Corrupt replacement" not in json.dumps(row)


def test_content_view_invalid_member_does_not_hide_valid_neighbor(
    transcript_world: TranscriptWorld,
) -> None:
    """Mixed selection remains independent and never acknowledges result mail."""
    world = transcript_world
    world.grant()
    message = world.message("Neighbor evidence")
    result = world.observe(handle=["missing_handle", world.task.handle], view="transcript")
    assert result["tasks"][0].get("error")
    assert result["tasks"][1]["items"] == [message.to_wire()]
    assert result.notification_ids == () and result.collect is False


def test_context_cursor_is_bound_to_its_selected_view(transcript_world: TranscriptWorld) -> None:
    """A transcript address cannot be interpreted as a different content selection."""
    world = transcript_world
    world.grant()
    world.message("Original source")
    first = world.observe(view="transcript")
    with pytest.raises(ValueError, match="different view or caller"):
        world.observe(view="summary", cursor=first["cursor"])


def test_initiating_prompt_is_not_lost_to_snapshot_timestamp_order(
    transcript_world: TranscriptWorld,
) -> None:
    """Confirmed invocation identity wins when the prompt predates its task snapshot."""
    world = transcript_world
    world.grant()
    original = world.message("Original initiating prompt", identity=world.task.invocation_id)
    earlier = (datetime.fromisoformat(world.task.created_at) - timedelta(seconds=1)).isoformat()
    source = original.model_copy(update={"created_at": earlier, "updated_at": earlier})
    world.app.state.messages[world.target] = [source]
    assert world.observe(view="transcript")["tasks"][0]["items"] == [source.to_wire()]


def test_unread_cursor_anchor_detects_a_rewritten_next_row(
    transcript_world: TranscriptWorld,
) -> None:
    """Hitting the row limit must retain the next source's version too."""
    world = transcript_world
    world.grant()
    first = world.message("First source")
    second = world.message("Second original", identity="second_source")
    page = world.observe(view="transcript", limit=1)
    assert page["has_more"]
    world.app.state.messages[world.target] = [
        first,
        second.model_copy(update={"parts": [Part(type="text", text="Rewritten next row")]}),
    ]
    after = world.observe(view="transcript", cursor=page["cursor"])
    assert "source changed" in after["tasks"][0]["error"]


def test_tiny_text_page_advances_over_a_complete_unicode_character(
    transcript_world: TranscriptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-character budget must never leave a multi-byte source stuck at offset zero."""
    world = transcript_world
    world.grant()
    version, _ = _registered_report(world, world.target, payload="α next".encode())
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "1")
    result = world.observe(view="artifacts", artifact_id=version.artifact_id)
    body = result["tasks"][0]["items"][0]
    assert body["content"] == "α" and body["next_offset"] == 2
    assert body["has_more"]


def test_binary_artifact_pages_bound_the_encoded_output(
    transcript_world: TranscriptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Base64 expansion counts toward the configured content bound."""
    world = transcript_world
    world.grant()
    payload = bytes(range(256)) * 4
    version, _ = _registered_report(
        world, world.target, payload=payload, name="payload.bin", kind=ArtifactKind.DATASET
    )
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "64")
    result = world.observe(view="artifacts", artifact_id=version.artifact_id)
    body = result["tasks"][0]["items"][0]
    assert body["encoding"] == "base64" and len(body["content"]) <= 64
    assert base64.b64decode(body["content"]) == payload[: body["next_offset"]]
    assert body["has_more"]


def test_parent_can_inspect_and_message_a_failed_child(transcript_world: TranscriptWorld) -> None:
    """The parent uses shared task controls and the existing mailbox after child failure."""
    from clio_agent.gact.task_declarations import build_task_tools
    from clio_agent.tools.task_call_context import TASK_CALL, TaskCall

    world = transcript_world
    child = record_delegation_fixture(world.app, world.reader, handle="task_failed_worker")
    assert (
        world.app.state.task_history.latest_assignment(child.owner.session_id).handle
        == child.handle
    )
    source = Message(
        id="failed_worker_evidence",
        session_id=child.owner.session_id,
        role="assistant",
        created_at=now(),
        updated_at=now(),
        parts=[Part(type="text", text="The child wrote one report before its failure.")],
    )
    world.app.state.messages[child.owner.session_id] = [source]
    ended = now()
    world.app.state.task_history.record_snapshot(
        child.model_copy(
            update={
                "status": "failed",
                "raw_status": "failed",
                "terminal_at": ended,
                "updated_at": ended,
            }
        ),
        observation_id="failed-worker-observation",
    )
    read = world.observe(handle=child.handle, view="transcript")
    assert read["tasks"][0]["items"] == [source.to_wire()]
    send = next(tool for tool in build_task_tools() if tool.name == "send_agent_message")
    with active_turn_fixture(world.app, world.reader):
        token = TASK_CALL.set(TaskCall(invocation_id="inspect-then-message"))
        try:
            delivered = asyncio.run_coroutine_threadsafe(
                send.func(
                    target=child.handle,
                    message="Inspect your report and continue without replaying it.",
                ),
                world.app.state.conversations._loop,
            ).result(timeout=10)
        finally:
            TASK_CALL.reset(token)
    assert delivered["accepted"] is True
    assert delivered["session_id"] == child.owner.session_id
    mail = world.app.state.task_history.mail_item(child.owner.session_id, delivered["message_id"])
    assert mail.sender_session == world.reader and mail.kind == "peer"
    assert world.app.state.task_history.task_snapshot(child.handle).status == "failed"


def test_admission_identity_separates_same_instant_assignments(
    transcript_world: TranscriptWorld,
) -> None:
    """A successor at the terminal instant cannot enter a predecessor transcript."""
    world = transcript_world
    world.grant()
    history = world.app.state.task_history
    content = history.write_content(world.target, {"text": "Original admitted input"})
    history.record_mail(
        MailItem(
            id="retained-turn",
            recipient_session=world.target,
            kind="human",
            sender_human=world.app.state.human_principal,
            content=content,
            created_at=now(),
            delivery="held",
            admission=MailAdmission(
                assignment_id=world.task.handle,
                turn_id="original-turn",
                input_revision=1,
                content=content,
            ),
        )
    )
    stamp = now()
    original = Message(
        id="original-admitted",
        session_id=world.target,
        turn_id="original-turn",
        role="assistant",
        created_at=stamp,
        updated_at=stamp,
        parts=[Part(id="original-part", type="text", text="Retained original output")],
    )
    successor = original.model_copy(update={"id": "successor", "turn_id": "successor-turn"})
    world.app.state.messages[world.target] = [original, successor]
    history.record_snapshot(
        world.task.model_copy(
            update={"status": "completed", "terminal_at": stamp, "updated_at": stamp}
        ),
        observation_id="same-instant-terminal",
    )
    assert world.observe(view="transcript")["tasks"][0]["items"] == [original.to_wire()]


def test_cursor_detects_changes_before_its_anchor(transcript_world: TranscriptWorld) -> None:
    """A previously read source rewrite cannot produce a silently mixed transcript."""
    world = transcript_world
    world.grant()
    first = world.message("Original earlier source", identity="earlier")
    world.message("Later source", identity="later")
    page = world.observe(view="transcript", limit=1)
    assert page["has_more"] is True
    world.app.state.messages[world.target] = [
        row.model_copy(update={"parts": [Part(id="changed", type="text", text="Rewritten")]})
        if row.id == first.id
        else row
        for row in world.app.state.messages[world.target]
    ]
    next_page = world.observe(view="transcript", cursor=page["cursor"], limit=1)
    assert "source changed" in next_page["tasks"][0]["error"]


def test_artifact_cursor_pins_the_original_version(transcript_world: TranscriptWorld) -> None:
    """Continuation cannot adopt a replacement artifact digest from its current listing."""
    from clio_agent.gact.task_cursors import decode_cursor, encode_cursor

    world = transcript_world
    world.grant()
    version, _ = _registered_report(world, world.target, payload=b"a" * 20000)
    page = world.observe(view="artifacts", artifact_id=version.artifact_id)
    assert page["has_more"] is True
    cursor = decode_cursor(page["cursor"])
    cursor["positions"][world.task.handle]["sha256"] = "0" * 64
    changed = world.observe(
        view="artifacts", artifact_id=version.artifact_id, cursor=encode_cursor(cursor)
    )
    assert "Artifact content changed" in changed["tasks"][0]["error"]


def test_multiterm_query_does_not_match_an_unrelated_single_word(
    transcript_world: TranscriptWorld,
) -> None:
    """The task search contract requires the complete set of lexical terms."""
    world = transcript_world
    world.grant()
    world.message("Calibration exposure is 17.25 seconds.")
    assert not world.app.state.task_operations.query(
        world.caller, scope="authorized", query="calibration unrelated"
    )["tasks"]
    assert (
        world.app.state.task_operations.query(
            world.caller, scope="authorized", query="calibration exposure"
        )["tasks"][0]["handle"]
        == world.task.handle
    )


def test_missing_artifact_identity_is_reported_with_its_source_part(
    transcript_world: TranscriptWorld,
) -> None:
    """An unresolved historical link remains inspectable rather than disappearing."""
    world = transcript_world
    world.grant()
    message = world.message("", identity="legacy_report_link")
    world.app.state.messages[world.target] = [
        message.model_copy(
            update={
                "parts": [
                    Part(
                        id="legacy_report_part",
                        type="resource_link",
                        server_id="clio-artifacts",
                        name="old-report.txt",
                    )
                ]
            }
        )
    ]
    row = world.observe(view="artifacts")["tasks"][0]["items"][0]
    assert row["available"] is False and row["artifact_id"] is None
    assert row["message_id"] == message.id and row["part_id"] == "legacy_report_part"
