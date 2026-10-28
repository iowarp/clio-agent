"""Distinct retained conversion paths; migration never executes historical work."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    CoreReference,
    QuestionDefinition,
    QuestionReply,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.session_records import PageQuery
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.storage.models import TransferOperation
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.task_controls import source_operation_views
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_migration import migrate_history

STAMP = "2026-10-19T22:00:00Z"


def fixture_app(arc: ARCMemory, tmp_path: Path) -> tuple[Any, Any]:
    """Give the data-only converter real Core and the existing session owner."""
    sessions = SessionStore(path=tmp_path / "sessions.json")
    session = sessions.create(workspace_id="workspace")
    app = SimpleNamespace(
        state=SimpleNamespace(
            task_history=TaskHistory(arc), sessions=sessions, human_principal="human"
        )
    )
    return app, session


def legacy_event(arc: ARCMemory, sid: str, kind: str, body: dict[str, Any]) -> None:
    """Persist an old-format event through the real semantic-event lane."""
    arc.record_semantic_event(
        SemanticEvent(
            event_type=kind, session_id=sid, trace_id="legacy", span_id=kind, payload=body
        )
    )


def twice(app: Any) -> None:
    """Require a second conversion to leave canonical observations unchanged."""
    history = app.state.task_history
    migrate_history(app)
    before = len(list(history.arc._live.iter_event_contents()))
    snapshots = {handle: history.task_snapshot(handle) for handle in history.task_handles()}
    session_ids = tuple(
        session.id for session in app.state.sessions.select(PageQuery(archived=None))
    )
    pending = {sid: history.pending_mail(sid) for sid in session_ids}
    migrate_history(app)
    assert len(list(history.arc._live.iter_event_contents())) == before
    assert {handle: history.task_snapshot(handle) for handle in history.task_handles()} == snapshots
    assert {sid: history.pending_mail(sid) for sid in session_ids} == pending


@pytest.mark.parametrize("container", ["dictionary", "list"])
def test_metadata_task_question_and_mail_convert_with_core_event_precedence(
    arc: ARCMemory, tmp_path: Path, container: str
) -> None:
    """Metadata shapes survive; a later retained event wins over stale metadata."""
    app, session = fixture_app(arc, tmp_path)
    history, sessions = app.state.task_history, app.state.sessions
    parent = sessions.create(workspace_id="workspace")
    mcp = {
        "handle": "task_metadata_mcp",
        "key": {
            "server_id": "old-server",
            "backend_session_id": "old-session",
            "task_id": "old-task",
        },
        "tool": "old-tool",
        "description": "Stale metadata",
        "status": "running",
        "created_at": STAMP,
    }
    question = {
        "id": "structured-question",
        "handle": "task_metadata_question",
        "prompt": "Choose the retained options",
        "kind": "multi_choice",
        "options": [{"label": "First", "value": "first"}, {"label": "Second", "value": "second"}],
        "status": "answered",
        "allow_freeform": False,
        "selected_options": ["first", "second"],
        "answer": "Both",
        "metadata": {"a2ui_surface_id": "surface-original", "origin_session": session.id},
        "answer_metadata": {"human_id": "human", "request_id": "original-answer"},
        "source": "a2ui",
        "created_at": STAMP,
    }
    sessions.update(
        session.id,
        metadata_patch={
            "agent_task": {
                "task_id": "old-assignment",
                "handle": "task_metadata_assignment",
                "child_session_id": session.id,
                "parent_session_id": parent.id,
                "description": "Original delegated assignment",
                "status": "completed",
                "created_at": STAMP,
            },
            "mcp_tasks": {"one": mcp} if container == "dictionary" else [mcp],
            "user_questions": {"one": question} if container == "dictionary" else [question],
            "agent_mailbox": [
                {
                    "message_id": "metadata-mail",
                    "target_session_id": session.id,
                    "source_kind": "human",
                    "text": "Pending human feedback",
                    "accepted_at": STAMP,
                }
            ],
        },
    )
    legacy_event(
        arc,
        session.id,
        "task.mcp.observation",
        {"record": {**mcp, "description": "Newer Core observation", "status": "input_required"}},
    )
    twice(app)
    external = history.task_snapshot("task_metadata_mcp")
    assert external is not None and external.description == "Newer Core observation"
    assert external.raw_status == "input_required" and external.terminal_at is None
    assert external.backend is not None and external.backend.task_id == "old-task"
    assignment = history.task_snapshot("task_metadata_assignment")
    assert assignment is not None and assignment.description == "Original delegated assignment"
    assert assignment.owner.session_id == session.id
    assert assignment.owner.parent_handle is not None
    converted = history.task_snapshot("task_metadata_question")
    assert converted is not None and converted.status == "completed"
    definition = QuestionDefinition.model_validate(history.read_content(converted.details))
    reply = QuestionReply.model_validate(history.read_content(converted.result))
    assert definition.kind == "multi_choice" and not definition.allow_freeform
    assert tuple(option.value for option in definition.options) == ("first", "second")
    assert definition.metadata == question["metadata"] and definition.source == "a2ui"
    assert reply.selected_options == ("first", "second")
    assert reply.metadata == question["answer_metadata"] and reply.answer == "Both"
    mail = history.require_mail(session.id, "metadata-mail")
    assert mail.kind == "human" and mail.delivery == "eligible"
    assert history.read_content(mail.content)["text"] == "Pending human feedback"
    assert not history.acknowledged_notifications(session.id)


@pytest.mark.parametrize("family", ["native", "mcp"])
def test_nonterminal_legacy_work_keeps_identity_raw_progress_and_cancellation(
    arc: ARCMemory, tmp_path: Path, family: str
) -> None:
    """Missing execution custody remains uncertain; original evidence is not discarded."""
    app, session = fixture_app(arc, tmp_path)
    history = app.state.task_history
    original = {
        "handle": "task_unsettled_legacy",
        "kind": "Shell" if family == "native" else "MCP",
        "status": "running",
        "tool": "original-command",
        "created_at": STAMP,
        "updated_at": STAMP,
        "cancel_requested": True,
        "progress": {"completed": 3, "total": 10, "unit": "files"},
        "backend": {
            "server_id": "original-server",
            "backend_session_id": "original-session",
            "auth_scope": "original-scope",
        },
        "key": {
            "server_id": "original-server",
            "backend_session_id": "original-session",
            "task_id": "original-native-task",
        },
        "task_id": "original-native-task",
    }
    legacy_event(
        arc,
        session.id,
        f"task.{family}.observation",
        {"record": original} if family == "mcp" else original,
    )
    twice(app)
    converted = history.task_snapshot(original["handle"])
    assert converted is not None and converted.backend is not None
    assert converted.raw_status == "running" and converted.status == "task_state_uncertain"
    assert converted.terminal_at is None and converted.connection_freshness == "unavailable"
    assert converted.cancellation_requested_at == STAMP and converted.submission is None
    assert converted.backend.server == "original-server"
    assert converted.backend.session == "original-session"
    assert converted.backend.task_id == "original-native-task"
    assert converted.backend.auth_scope == "original-scope"
    raw = history.read_content(converted.backend_observation)
    assert raw["record"] == original
    assert raw["record"]["progress"] == {"completed": 3, "total": 10, "unit": "files"}
    assert history.native_attempts(converted.handle) == ()


def test_historical_relay_is_readable_with_no_special_execution_actions(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Removed relay placement stays historical and cannot acquire a new runtime owner."""
    app, session = fixture_app(arc, tmp_path)
    parent = app.state.sessions.create(workspace_id="workspace")
    original = {
        "task_id": "old-relay-assignment",
        "handle": "task_historical_relay",
        "child_session_id": session.id,
        "parent_session_id": parent.id,
        "placement": "relay",
        "status": "running",
        "description": "Original remote assignment",
        "created_at": STAMP,
    }
    legacy_event(arc, session.id, "task.assignment", {"task": original})
    twice(app)
    history = app.state.task_history
    converted = history.task_snapshot(original["handle"])
    assert converted is not None and converted.backend is not None
    assert converted.status == "task_state_uncertain" and converted.terminal_at is None
    assert converted.backend.transport == "historical_relay"
    assert (
        converted.backend.placement == "relay" and converted.connection_freshness == "unavailable"
    )
    assert converted.supported_actions == ("observe", "wait", "get_result")
    assert converted.description == original["description"]
    assert history.read_content(converted.backend_observation)["record"] == original
    assert converted.submission is None and history.native_attempts(converted.handle) == ()


def test_legacy_provider_receipt_is_retained_for_inspection_without_resubmission(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Missing trusted custody must not turn a historical provider ID into a new transfer."""
    app, session = fixture_app(arc, tmp_path)
    original = {
        "handle": "task_legacy_provider",
        "kind": "Download",
        "status": "running",
        "tool": "connected_data_download",
        "created_at": STAMP,
        "source_id": "original-source",
        "operation_id": "original-operation",
        "native_job_id": "original-globus-job",
        "native_request": {
            "submission_id": "original-submission",
            "DATA": [{"source_path": "/approved/input", "destination_path": "/approved/output"}],
        },
        "selected_paths": ["approved/input"],
        "provider_identity": {"provider": "globus", "collection_id": "approved-collection"},
    }
    legacy_event(arc, session.id, "task.native.observation", original)
    twice(app)
    history = app.state.task_history
    converted = history.task_snapshot(original["handle"])
    assert converted is not None and converted.status == "task_state_uncertain"
    assert converted.terminal_at is None and converted.submission is None
    assert history.read_content(converted.backend_observation)["record"] == original
    assert history.native_attempts(converted.handle) == ()
    assert history.resolve_handle(original["handle"]) == converted.handle


@pytest.mark.parametrize("fail_recording", [False, True])
def test_branch_transfer_identity_migrates_before_strict_operation_decode(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_recording: bool,
) -> None:
    """Retain old provider custody in Core before removing obsolete storage fields."""

    app, session = fixture_app(arc, tmp_path)
    history = app.state.task_history
    store = SourceStore(tmp_path / "old-connected-storage")
    app.state.connected_storage = SimpleNamespace(store=store)
    original = {
        "id": "old-transfer",
        "source_id": "old-source",
        "kind": "native_transfer",
        "state": "running",
        "native_job_id": "original-provider-job",
        "native_request": {"submission_id": "original-provider-submission"},
        "selected_paths": ["approved/data.csv"],
        "created_at": STAMP,
        "updated_at": STAMP,
        "task_handle": "task_original_transfer",
        "owner_session_id": session.id,
        "invocation_id": "original-model-call",
    }
    with store.transaction() as db:
        db.execute(
            "INSERT INTO records VALUES ('operation', ?, ?)", (original["id"], json.dumps(original))
        )
    if fail_recording:
        original_write = history.confirm_write

        def refuse_write(_operation: Any) -> Any:
            raise OSError("Injected Core persistence outage")

        monkeypatch.setattr(history, "confirm_write", refuse_write)
        with pytest.raises(OSError, match="persistence outage"):
            migrate_history(app)
        with store.transaction() as db:
            raw = db.execute(
                "SELECT body FROM records WHERE kind='operation' AND id=?", (original["id"],)
            ).fetchone()
        assert raw is not None and json.loads(raw[0]) == original
        monkeypatch.setattr(history, "confirm_write", original_write)
        history = TaskHistory(arc)
        history.restore()
        app.state.task_history = history
    twice(app)
    operation = store.get("operation", original["id"], TransferOperation)
    assert operation.native_job_id == original["native_job_id"]
    assert operation.native_request == original["native_request"]
    assert operation.selected_paths == original["selected_paths"]
    assert operation.state == "running"
    task = history.task_snapshot(original["task_handle"])
    assert task is not None and task.kind == "Download"
    assert task.owner.session_id == session.id and task.invocation_id == original["invocation_id"]
    assert task.submission is None and task.terminal_at is None
    assert operation.native_job_id in json.dumps(history.read_content(task.backend_observation))
    assert history.native_attempts(task.handle) == ()
    with store.transaction() as db:
        raw = db.execute(
            "SELECT body FROM records WHERE kind='operation' AND id=?", (original["id"],)
        ).fetchone()
    assert raw is not None
    assert not ({"task_handle", "owner_session_id", "invocation_id"} & json.loads(raw[0]).keys())
    views = source_operation_views(app, original["source_id"])
    assert len(views) == 1 and views[0]["id"] == original["id"]
    assert views[0]["task_handle"] == original["task_handle"]
    assert views[0]["native_job_id"] == original["native_job_id"]


def seed_old_operation(
    app: Any, session: Any, tmp_path: Path
) -> tuple[SourceStore, dict[str, Any]]:
    """Seed the real earlier storage wire form, with distinct operation and task IDs."""

    store = SourceStore(tmp_path / "legacy-operation")
    app.state.connected_storage = SimpleNamespace(store=store)
    original = {
        "id": "transfer_original",
        "source_id": "source_original",
        "kind": "native_transfer",
        "state": "running",
        "bytes_done": 33,
        "bytes_total": 64,
        "native_job_id": "provider_original_job",
        "native_request": {"submission_id": "provider_original_submission"},
        "selected_paths": ["approved/data.csv"],
        "created_at": STAMP,
        "updated_at": STAMP,
        "task_handle": "task_original",
        "owner_session_id": session.id,
        "invocation_id": "original_invocation",
    }
    with store.transaction() as db:
        db.execute(
            "INSERT INTO records VALUES ('operation', ?, ?)", (original["id"], json.dumps(original))
        )
    return store, original


def assert_retained_operation(history: TaskHistory, original: dict[str, Any]) -> None:
    """Require a confirmed Core association to a body containing the complete old row."""
    records = [
        event["payload"]["record"]
        for event in history.arc._live.iter_event_contents()
        if event.get("payload", {}).get("record", {}).get("operation_id") == original["id"]
        and event["payload"]["record"].get("content")
    ]

    def contains(value: Any) -> bool:
        if value == original:
            return True
        if isinstance(value, dict):
            return any(contains(child) for child in value.values())
        if isinstance(value, list):
            return any(contains(child) for child in value)
        return False

    assert (
        sum(
            contains(history.read_content(CoreReference.model_validate(record["content"])))
            for record in records
        )
        == 1
    )


def test_existing_core_outcome_wins_while_old_storage_counters_keep_their_task(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Association must not overwrite the current outcome or fabricate a second source row."""
    app, session = fixture_app(arc, tmp_path)
    _, original = seed_old_operation(app, session, tmp_path)
    history = app.state.task_history
    current = TaskSnapshot(
        handle=original["task_handle"],
        kind="Download",
        owner=TaskOwner(human_id="human", session_id=session.id, assignment_id="assignment"),
        label="Current canonical outcome",
        tool="connected_data_download",
        invocation_id=original["invocation_id"],
        created_at=STAMP,
        updated_at=STAMP,
        status="completed",
        raw_status="completed",
        terminal_at=STAMP,
        settlement="confirmed",
        result=history.write_content(session.id, {"output": "Current committed result"}),
    )
    history.record_snapshot(current, observation_id="current")
    twice(app)
    assert history.task_snapshot(current.handle) == current
    assert_retained_operation(history, original)
    views = source_operation_views(app, original["source_id"])
    assert len(views) == 1
    assert views[0]["id"] == original["id"] and views[0]["task_handle"] == current.handle
    assert views[0]["bytes_done"] == 33 and views[0]["bytes_total"] == 64
    assert views[0]["native_job_id"] == original["native_job_id"]
    assert views[0]["state"] == "completed"


@pytest.mark.parametrize("conflict", ["missing_owner", "other_owner", "other_source"])
def test_unknown_or_conflicting_storage_ownership_is_retained_without_invention(
    arc: ARCMemory, tmp_path: Path, conflict: str
) -> None:
    """An orphan or conflict stays inspectable and cannot acquire a task association."""

    app, session = fixture_app(arc, tmp_path)
    store, original = seed_old_operation(app, session, tmp_path)
    history = app.state.task_history
    current = None
    if conflict != "missing_owner":
        other = app.state.sessions.create(workspace_id="workspace")
        current = TaskSnapshot(
            handle=original["task_handle"],
            kind="Download",
            owner=TaskOwner(
                human_id="human",
                session_id=other.id if conflict == "other_owner" else session.id,
                assignment_id="other",
            ),
            label="Other immutable owner",
            tool="connected_data_download",
            invocation_id="other-invocation"
            if conflict == "other_owner"
            else original["invocation_id"],
            resource_id="explicitly-different-source" if conflict == "other_source" else None,
            created_at=STAMP,
            updated_at=STAMP,
        )
        history.record_snapshot(current, observation_id="other-owner")
    else:
        original["owner_session_id"] = ""
        with store.transaction() as db:
            db.execute(
                "UPDATE records SET body=? WHERE kind='operation' AND id=?",
                (json.dumps(original), original["id"]),
            )
    twice(app)
    assert history.task_snapshot(original["task_handle"]) == current
    assert history.resolve_handle(original["id"]) is None
    assert len(app.state.task_migration_issues) == 1
    assert_retained_operation(history, original)
    assert (
        store.get("operation", original["id"], TransferOperation).native_job_id
        == original["native_job_id"]
    )
    assert all(
        not row.get("task_handle") for row in source_operation_views(app, original["source_id"])
    )


@pytest.mark.parametrize("failure", ["write_error", "concurrent_change"])
def test_core_confirmation_survives_sqlite_prune_failure_and_retry(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A failed prune retains the old row; retry reuses the confirmed Core proof."""
    from collections.abc import Iterator
    from contextlib import contextmanager

    app, session = fixture_app(arc, tmp_path)
    store, original = seed_old_operation(app, session, tmp_path)
    transaction = store.transaction

    class BeforePrune:
        def __init__(self, database: Any) -> None:
            self.database = database

        def execute(self, statement: str, parameters: Any = ()) -> Any:
            if statement.startswith("UPDATE records SET body"):
                if failure == "write_error":
                    raise OSError("Injected failure before SQLite prune")
                changed = {**original, "bytes_done": 45}
                self.database.execute(
                    "UPDATE records SET body=? WHERE kind='operation' AND id=?",
                    (json.dumps(changed), original["id"]),
                )
            return self.database.execute(statement, parameters)

    @contextmanager
    def reject_prune() -> Iterator[Any]:
        with transaction() as db:
            yield BeforePrune(db)

    monkeypatch.setattr(store, "transaction", reject_prune)
    expected_error = OSError if failure == "write_error" else RuntimeError
    match = "before SQLite prune" if failure == "write_error" else "changed during migration"
    with pytest.raises(expected_error, match=match):
        migrate_history(app)
    assert_retained_operation(app.state.task_history, original)
    with transaction() as db:
        row = db.execute(
            "SELECT body FROM records WHERE kind='operation' AND id=?", (original["id"],)
        ).fetchone()
    remaining = original if failure == "write_error" else {**original, "bytes_done": 45}
    assert row is not None and json.loads(row[0]) == remaining
    monkeypatch.setattr(store, "transaction", transaction)
    twice(app)
    assert_retained_operation(app.state.task_history, original)
    operation = store.get("operation", original["id"], TransferOperation)
    assert operation.updated_at == STAMP and operation.native_request == original["native_request"]
    assert operation.bytes_done == remaining["bytes_done"]


def test_storage_migration_does_not_hide_other_invalid_fields(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Only the three obsolete fields may be pruned; strict validation stays intact."""
    from pydantic import ValidationError

    app, session = fixture_app(arc, tmp_path)
    store, original = seed_old_operation(app, session, tmp_path)
    original["unknown_workload_field"] = "must remain an explicit error"
    with store.transaction() as db:
        db.execute(
            "UPDATE records SET body=? WHERE kind='operation' AND id=?",
            (json.dumps(original), original["id"]),
        )
    with pytest.raises(ValidationError):
        migrate_history(app)
    with store.transaction() as db:
        row = db.execute(
            "SELECT body FROM records WHERE kind='operation' AND id=?", (original["id"],)
        ).fetchone()
    assert row is not None and json.loads(row[0]) == original
    assert app.state.task_history.task_snapshot(original["task_handle"]) is None
