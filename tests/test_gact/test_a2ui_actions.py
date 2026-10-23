"""A2UI action dispatcher: durable, idempotent, destination-routed (S5).

Covers docs/design/a2ui-compat-campaign-2026-09.md S5's delivery matrix and
lifecycle beyond what ``test_a2ui_v3.py``/``test_interactions.py`` already
exercise through the rewritten agent-destination tests: idempotency,
permission/run destinations, waiting_user resume/uncorrelated, the client
data-model per-surface filter, VALIDATION_FAILED repair/exhaustion, generic
error ingestion, the ``a2ui.action.*`` lifecycle events, and compaction/
rollback survival of the ``a2ui_action`` part type.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch, mark

from clio_agent.arc.task_types import ContextBoundary
from clio_agent.gact.a2ui_actions.record import mark_a2ui_action_consumed
from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event
from clio_agent.gact.interaction_types import UserQuestion
from clio_agent.gact.question_surface import question_row
from clio_agent.gact.routes.interactions import project_pending_interactions
from clio_agent.gact.routes.session_a2ui_preservation import preserve_a2ui
from clio_agent.gact.types import Message, Part
from tests.test_gact.input_capture import capture_inputs
from tests.test_gact.task_records import record_question_fixture

from .task_records import hold_model_segment, record_assignment_fixture
from .test_a2ui_v3 import HEADERS, WORKSPACE_CATALOG_ID, _create_message, _session_client

pytestmark = mark.usefixtures("a2ui_session_lifespan")


def _capture_inputs(app: Any, monkeypatch: Any = None) -> list[Any]:
    """Hold model admission and observe actual confirmed Core inputs."""
    return capture_inputs(app, monkeypatch)


def _event(name: str, surface_id: str, context: dict[str, Any], timestamp: str) -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "action": {
            "name": name,
            "surfaceId": surface_id,
            "sourceComponentId": "source",
            "timestamp": timestamp,
            "context": context,
        },
    }


# --------------------------------------------------------------------------- #
# Idempotency                                                                 #
# --------------------------------------------------------------------------- #


def test_duplicate_submission_returns_same_record_no_second_turn(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    spawned = _capture_inputs(app, monkeypatch)
    action = _event(
        "earthscope.stations.selected",
        "surface_1",
        {"stationIds": ["SGPS", "P123"]},
        "2026-09-17T00:00:00Z",
    )

    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["action_id"] == second.json()["action_id"]
    assert len(spawned) == 1
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert len(surface.actions) == 1
    # Undeclared destination -> the event still delivers verbatim.
    message = next(m for m in reversed(app.state.messages[sid]) if m.role == "user")
    assert message.metadata["a2ui_action_context"] == {"stationIds": ["SGPS", "P123"]}


def test_two_distinct_timestamps_create_two_records(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    _capture_inputs(app, monkeypatch)
    first_action = _event(
        "earthscope.stations.selected", "surface_1", {"x": 1}, "2026-09-17T00:00:00Z"
    )
    second_action = _event(
        "earthscope.stations.selected", "surface_1", {"x": 1}, "2026-09-17T00:00:01Z"
    )

    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": first_action}
    )
    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": second_action}
    )

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["action_id"] != second.json()["action_id"]
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert len(surface.actions) == 2


# --------------------------------------------------------------------------- #
# Running -> steer -> consumed at the mid-turn drain                          #
# --------------------------------------------------------------------------- #


@mark.parametrize("projection_failure", [False, True])
def test_running_feedback_is_consumed_only_after_core_input_commit(
    tmp_path: Path, monkeypatch: MonkeyPatch, projection_failure: bool
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )

    hold_model_segment(app, sid, monkeypatch)
    action = _event("earthscope.stations.selected", "surface_1", {"x": 1}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivery"] == "steer"
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["state"] == "delivered"

    history, owner = app.state.task_history, app.state.conversations
    assignment = history.latest_assignment(sid) or record_assignment_fixture(app, sid)
    caller = owner.caller(sid)
    staged = owner.mail.stage(caller)
    assert staged.items and surface.actions[0]["state"] == "delivered"
    from clio_agent.gact.a2ui_actions import record as action_records

    original = action_records.mark_a2ui_action_consumed
    hold_projection = projection_failure

    def project(*args: Any, **kwargs: Any) -> Any:
        if hold_projection:
            raise OSError("Owned display projection is temporarily unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(action_records, "mark_a2ui_action_consumed", project)
    owner.mail.commit(
        caller,
        staged,
        assignment_id=assignment.handle,
        iteration_id="a2ui-feedback",
        boundary=ContextBoundary(session_id=sid, scope="main", logical_time=0),
    )
    assert "a2ui:" + record["id"] in history.acknowledged_notifications(sid)
    if projection_failure:
        held = app.state.a2ui_store.get(sid, "surface_1")
        assert held is not None and held.actions[0]["state"] == "delivered"
        hold_projection = False
        owner._restore_input_projections()

    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [consumed_record] = surface.actions
    assert consumed_record["id"] == record["id"]
    assert consumed_record["state"] == "consumed"


# --------------------------------------------------------------------------- #
# Waiting-user resume / uncorrelated                                         #
# --------------------------------------------------------------------------- #


def _pending_question(sid: str, *, surface_id: str = "", qid: str = "q_1") -> UserQuestion:
    now = datetime.now(timezone.utc).isoformat()
    return UserQuestion(
        id=qid,
        session_id=sid,
        owner_session_id=sid,
        attended_session_id=sid,
        prompt="Continue?",
        allow_freeform=True,
        created_at=now,
        updated_at=now,
        metadata={
            "resume_on_answer": True,
            "a2ui_surface_id": surface_id,
            "a2ui_answer_action": "agent.submit",
        },
    )


def test_waiting_user_surface_tagged_question_resumes_with_context(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    app.state.agent = object()
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    _capture_inputs(app, monkeypatch)
    question = _pending_question(sid, surface_id="surface_1")
    question = record_question_fixture(app, question)
    app.state.sessions.update(
        sid, status="waiting_user", metadata_patch={"pending_user_question_id": question.id}
    )
    action = _event(
        "agent.submit", "surface_1", {"text": "go", "answer": "go"}, "2026-09-17T00:00:00Z"
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery"] == "resolve_question"
    assert body["state"] == "delivered"
    answered = question_row(app, question.id)
    assert answered.status == "answered"
    assert answered.answer == "go"
    assert answered.answer_metadata["text"] == "go"
    task = app.state.task_history.task_snapshot(question.id)
    assert task is not None and task.result is not None
    assert (
        app.state.task_history.read_content(task.result)["source_action"]["surface_id"]
        == "surface_1"
    )


def test_explicit_question_correlation_refuses_a_foreign_surface(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    app.state.agent = object()
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    _capture_inputs(app, monkeypatch)
    # A pending question tagged for a DIFFERENT surface never correlates.
    question = _pending_question(sid, surface_id="unrelated-surface")
    question = record_question_fixture(app, question)
    app.state.sessions.update(
        sid, status="waiting_user", metadata_patch={"pending_user_question_id": question.id}
    )
    action = _event("agent.submit", "surface_1", {"text": "go"}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={"message": action, "correlation": {"question_id": question.id}},
    )

    assert response.status_code == 409
    assert "does not address this Question" in response.json()["error"]["message"]
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["state"] == "failed"
    assert record["reason"]
    assert question_row(app, question.id).status == "pending"
    assert not [
        row for row in app.state.task_history.pending_mail(sid) if row.id.startswith("a2ui:")
    ]


# --------------------------------------------------------------------------- #
# Permission destination                                                     #
# --------------------------------------------------------------------------- #


def _permission_action(surface_id: str, permission_id: str, decision: str) -> dict[str, Any]:
    return _event(
        "approval.respond",
        surface_id,
        {"permission_id": permission_id, "action": decision},
        "2026-09-17T00:00:00Z",
    )


def test_permission_destination_resolves_in_scope_permission_only(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    now = datetime.now(timezone.utc).isoformat()
    app.state.permissions["perm_in_scope"] = {
        "id": "perm_in_scope",
        "session_id": sid,
        "status": "pending",
        "summary": "Allow write",
        "created_at": now,
        "tool_call": {"tool_name": "fs_write_file", "input": {"path": "x"}},
    }
    app.state.permissions["perm_out_of_scope"] = {
        "id": "perm_out_of_scope",
        "session_id": "sess_unrelated",
        "status": "pending",
        "summary": "Allow write elsewhere",
        "created_at": now,
        "tool_call": {"tool_name": "fs_write_file", "input": {"path": "y"}},
    }

    out_of_scope = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={"message": _permission_action("surface_1", "perm_out_of_scope", "allow")},
    )
    in_scope = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={"message": _permission_action("surface_1", "perm_in_scope", "allow")},
    )

    assert out_of_scope.status_code == 404
    assert app.state.permissions["perm_out_of_scope"]["status"] == "pending"
    assert in_scope.status_code == 200, in_scope.text
    body = in_scope.json()
    assert body["delivery"] == "permission"
    assert body["state"] == "delivered"
    assert app.state.permissions["perm_in_scope"]["status"] == "resolved"


def test_permission_destination_unknown_permission_id_returns_typed_404(
    tmp_path: Path,
) -> None:
    """A ``permission_id`` that IS present but names no pending permission

    (unknown or already resolved/expired) must still reach
    ``_deliver_permission`` and get its existing typed refusal -- never a
    silent reroute to the agent lane (issue #1549 #28, design rule 2).
    """

    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={"message": _permission_action("surface_1", "perm_does_not_exist", "allow")},
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["error"] == "not_found"
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["state"] == "failed"
    assert record["reason"] == "not_found"


def test_standalone_approval_without_permission_id_delivers_to_agent(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A ``clio.approval.v1`` card not bound to a real pending permission.

    The sidecar names ``approval.respond``'s target lane as "permission",
    but a standalone card (the agent's own yes/no question, never wired to a
    tool-call permission) carries no ``permission_id`` at all -- that is
    structurally NOT a permission response, so it must deliver as an
    ordinary agent action instead of 422ing in ``_deliver_permission``
    (issue #1549 #28; was gact-tui#514's merge-gate finding).
    """

    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    _capture_inputs(app, monkeypatch)
    action = _event("approval.respond", "surface_1", {"approved": True}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["destination"] == "agent"
    assert body["delivery"] == "steer"
    assert body["state"] == "delivered"
    message = next(m for m in reversed(app.state.messages[sid]) if m.role == "user")
    assert message.metadata["a2ui_action_context"] == {"approved": True}


def test_standalone_approval_cancel_without_permission_id_delivers_to_agent(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Symmetric to the approve case: ``{"approved": false}`` also reaches the agent."""

    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    _capture_inputs(app, monkeypatch)
    action = _event("approval.respond", "surface_1", {"approved": False}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["destination"] == "agent"
    assert body["delivery"] == "steer"
    message = next(m for m in reversed(app.state.messages[sid]) if m.role == "user")
    assert message.metadata["a2ui_action_context"] == {"approved": False}


def test_action_card_custom_event_unaffected_by_permission_routing(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A ``clio.action-card.v1``-style custom event never comes near the

    permission lane and keeps working exactly as before this fix (the G1b
    regression requirement: "the action card is unaffected").
    """

    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    _capture_inputs(app, monkeypatch)
    action = _event("workspace.summarize", "surface_1", {"scope": "all"}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["destination"] == "agent"
    assert body["delivery"] == "steer"
    message = next(m for m in reversed(app.state.messages[sid]) if m.role == "user")
    assert message.metadata["a2ui_action_context"] == {"scope": "all"}


# --------------------------------------------------------------------------- #
# Run destination                                                             #
# --------------------------------------------------------------------------- #


def test_run_cancel_destination_cancels_the_session(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event("run.cancel", "surface_1", {}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery"] == "run_cancel"
    assert body["state"] == "delivered"
    assert app.state.sessions.get(sid).status == "idle"
    assert app.state.sessions.get(sid).metadata["composer_queue_paused"] is True
    assert not app.state.turn_runner.busy(sid)
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["delivery"] == "run_cancel"


def test_run_retry_destination_queues_the_existing_owner_attempt(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    app.state.agent = object()
    spawned = _capture_inputs(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    posted = client.post(
        f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "hi"}]}
    )
    assert posted.status_code == 202, posted.text
    source_message_id = posted.json()["message_id"]
    spawned.clear()

    action = _event(
        "run.retry", "surface_1", {"message_id": source_message_id}, "2026-09-17T00:00:00Z"
    )
    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery"] == "run_retry"
    assert body["state"] == "delivered"
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["correlation"]["attempt_id"]


# --------------------------------------------------------------------------- #
# Client data-model per-surface filtering                                    #
# --------------------------------------------------------------------------- #


def test_foreign_surface_data_model_dropped_while_action_still_delivers(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    accepted_inputs = _capture_inputs(app, monkeypatch)
    create_owned = {
        "version": "v0.9.1",
        "createSurface": {
            "surfaceId": "owned-surface",
            "catalogId": WORKSPACE_CATALOG_ID,
            "sendDataModel": True,
        },
    }
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [create_owned]}
    )
    action = _event("agent.submit", "owned-surface", {"text": "go"}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={
            "message": action,
            "metadata": {
                "a2uiClientDataModel": {
                    "version": "v0.9.1",
                    "surfaces": {
                        "owned-surface": {"selection": "x"},
                        "foreign-surface": {"selection": "y"},
                    },
                }
            },
        },
    )

    assert response.status_code == 200, response.text
    carried = response.json()["a2ui_client_data_model"]
    assert carried["surfaces"] == {"owned-surface": {"selection": "x"}}
    assert len(accepted_inputs) == 1
    content = app.state.task_history.read_content(accepted_inputs[0].content)
    text = content["text"]
    assert text.count("A2UI client data model:\n") == 1
    assert json.loads(text.split("A2UI client data model:\n", 1)[1]) == carried
    assert "foreign-surface" not in text
    reasons = app.state.a2ui_catalogs.session_reasons(sid)
    assert any(
        row["reason"] == "a2ui_data_model_foreign_surface"
        and row.get("surface_id") == "foreign-surface"
        for row in reasons
    )


# --------------------------------------------------------------------------- #
# Client error ingestion: VALIDATION_FAILED repair/exhaustion, generic       #
# --------------------------------------------------------------------------- #


def _error_envelope(surface_id: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"version": "v0.9.1", "error": {"code": code, "surfaceId": surface_id, **extra}}


def test_validation_failed_repairs_once_then_exhausts_and_fails_the_surface(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    spawned = _capture_inputs(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    envelope = _error_envelope(
        "surface_1", "VALIDATION_FAILED", path="/root/0", message="unknown component"
    )

    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )
    assert first.status_code == 200, first.text
    assert first.json()["delivery"] == "steer"
    assert len(spawned) == 1
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert surface.state != "failed"

    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "failed"
    assert second.json()["reason"] == "a2ui_repair_exhausted"
    # No second repair turn.
    assert len(spawned) == 1
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert surface.state == "failed"
    assert len(surface.actions) == 2


def test_generic_client_error_persisted_never_delivered(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    spawned = _capture_inputs(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    envelope = _error_envelope("surface_1", "SOME_OTHER_ERROR", message="renderer had a problem")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "failed"
    assert body["reason"] == "a2ui_client_error_unhandled"
    assert body["delivery"] == "rejected"
    assert not spawned


def test_error_envelope_routes_before_surface_lookup(tmp_path: Path) -> None:
    """An error naming a surfaceId that never existed is still accepted (S6-review)."""

    client, sid, _ = _session_client(tmp_path)
    envelope = _error_envelope("never-created", "VALIDATION_FAILED", path="/x", message="boom")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )

    assert response.status_code == 200, response.text
    assert response.json()["surface_id"] == "never-created"


# --------------------------------------------------------------------------- #
# Interactions projection: pending -> answered from records                  #
# --------------------------------------------------------------------------- #


def test_interactions_row_flips_pending_to_answered_once_delivered(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    _capture_inputs(app, monkeypatch)
    components = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {"id": "label", "component": "Text", "text": "Go"},
                {
                    "id": "go",
                    "component": "Button",
                    "child": "label",
                    "action": {"event": {"name": "agent.submit", "context": {}}},
                },
            ],
        },
    }
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), components]},
    )

    before = project_pending_interactions(app, sid, include_children=False)
    [row_before] = [r for r in before.rows if r.kind == "a2ui"]
    assert row_before.status == "pending"
    # Offering a button does not request an answer or take over its UI view.
    assert row_before.requires_human_response is False

    action = _event("agent.submit", "surface_1", {"text": "go"}, "2026-09-17T00:00:00Z")
    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert response.status_code == 200, response.text

    after = project_pending_interactions(
        app, sid, include_children=False, include_recent_resolved=True
    )
    [row_after] = [r for r in after.rows if r.kind == "a2ui"]
    assert row_after.status == "answered"
    assert row_after.requires_human_response is False
    last_action = row_after.payload["last_action"]
    assert last_action["state"] == "delivered"
    # Contract: payload.last_action is a WIRE shape the client reads --
    # pre-S5 it was the /lastAction data-model value dispatch_action wrote
    # ({name, status, receivedAt, context}); that write is deleted, but every
    # field it exposed must survive byte-compatible off the durable record,
    # with action_id/state/delivery as S5-only additions (never a
    # replacement for the originals).
    assert last_action["name"] == "agent.submit"
    assert last_action["status"] == "accepted"
    assert last_action["receivedAt"]
    assert last_action["context"] == {"text": "go"}
    assert last_action["action_id"]
    assert last_action["delivery"] == "steer"


# --------------------------------------------------------------------------- #
# Consumed transition (unit-level, no real turn needed)                      #
# --------------------------------------------------------------------------- #


def test_mark_consumed_transitions_delivered_record_and_publishes_event(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = cast(FastAPI, client.app)
    _capture_inputs(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event("agent.submit", "surface_1", {"text": "go"}, "2026-09-17T00:00:00Z")
    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert response.status_code == 200, response.text
    record_id = response.json()["action_id"]

    published: list[Event] = []
    original_publish = app.state.bus.publish

    def _capture(event: Event) -> None:
        published.append(event)
        original_publish(event)

    monkeypatch.setattr(app.state.bus, "publish", _capture)

    consumed_once = mark_a2ui_action_consumed(
        app, sid, {"a2ui_action": record_id, "surface_id": "surface_1"}
    )
    consumed_twice = mark_a2ui_action_consumed(
        app, sid, {"a2ui_action": record_id, "surface_id": "surface_1"}
    )

    assert consumed_once is True
    assert consumed_twice is False, "an already-consumed record is a no-op, not a second transition"
    assert any(event.type == "a2ui.action.consumed" for event in published)
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["state"] == "consumed"


# --------------------------------------------------------------------------- #
# Compaction/rollback survival of the a2ui_action part type                  #
# --------------------------------------------------------------------------- #


def test_preserve_a2ui_keeps_both_surface_and_action_parts() -> None:
    """Unit-level: the SHARED preservation function both undo and compaction
    call keeps a surface part AND an a2ui_action part targeting it, exactly
    like it already keeps a surface part alone."""

    now = "2026-09-17T00:00:00+00:00"
    surface_part = Part(id="part_surface", type="a2ui", surface_id="s1", metadata={})
    action_part = Part(
        id="part_action",
        type="a2ui_action",
        surface_id="s1",
        a2ui_action_record={"id": "a2ui_action_1", "surface_id": "s1", "state": "delivered"},
        metadata={},
    )
    removed = Message(
        id="msg_removed",
        session_id="sid",
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=[surface_part, action_part],
    )
    retained = Message(
        id="msg_kept",
        session_id="sid",
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="part_kept", type="text", text="hi")],
    )

    result = preserve_a2ui("sid", [retained], [removed], "undo")

    assert len(result) == 2
    preserved_types = {part.type for part in result[-1].parts}
    assert preserved_types == {"a2ui", "a2ui_action"}
    assert result[-1].metadata == {"synthetic": "a2ui_preservation", "preserved_by": "undo"}


def test_undo_preserves_action_record_from_removed_message(tmp_path: Path) -> None:
    """End-to-end: an undo that removes the message carrying an ``a2ui_action``
    part still leaves the record readable through the normal store projection."""

    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = cast(FastAPI, client.app)
        sid = client.post("/v1/sessions", json={"title": "rollback action"}).json()["id"]
        app.state.messages[sid] = [
            Message(
                id="msg_1",
                session_id=sid,
                role="user",
                created_at="2026-05-20T00:00:00+00:00",
                updated_at="2026-05-20T00:00:00+00:00",
                parts=[Part(id="part_1", type="text", text="hi")],
            )
        ]
        app.state.sessions.update(sid, message_count=1)
        app.state.a2ui_store.apply_batch(sid, [_create_message()])
        action_envelope = _event(
            "agent.submit", "surface_1", {"text": "go"}, "2026-09-17T00:00:00Z"
        )

        capture_inputs(client.app)
        response = client.post(
            f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action_envelope}
        )
        assert response.status_code == 200, response.text
        record_id = response.json()["action_id"]
        messages_before = [m.id for m in app.state.messages[sid]]
        # The LAST message minted is the record's own "delivered" snapshot
        # (persisted after the "received" one and the staged turn message);
        # undo count=1 removes exactly that one -- the record must survive
        # through the SAME preservation path an ``a2ui`` surface part uses.
        assert any(p.type == "a2ui_action" for p in app.state.messages[sid][-1].parts)
        # The stubbed spawn closed the coroutine before it could settle the
        # session back to idle; undo refuses on a running session.
        app.state.sessions.update(sid, status="idle")

        undo_response = client.post(f"/v1/sessions/{sid}/undo", json={"count": 1})
        assert undo_response.status_code == 200, undo_response.text
        assert [m.id for m in app.state.messages[sid]] != messages_before

        surface = app.state.a2ui_store.get(sid, "surface_1")
        assert surface is not None
        assert any(row["id"] == record_id for row in surface.actions)
