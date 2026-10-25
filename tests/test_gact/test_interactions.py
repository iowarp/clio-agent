"""Public interaction routes over the sole Core Question owner.

Task acceptance, response identity, restart retention, validation and isolation are
asserted independently of removed registries and blocking ask modes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.blob_frame import BlobDecodeError
from clio_agent.arc.task_types import QuestionDefinition, QuestionReply, TaskSnapshot
from clio_agent.gact.app import build_app
from clio_agent.gact.question_surface import question_row
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_declarations import build_ask_user_tool
from clio_agent.gact.task_history import TaskHistory
from tests.test_gact.task_records import (
    active_turn_fixture,
    record_assignment_fixture,
    record_delegation_fixture,
)

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def post_question(app: Any, sid: str, prompt: str = "Which sample?") -> Any:
    """Post through the registered FastMCP control under actual owner context."""
    with active_turn_fixture(app, sid):
        return asyncio.run(
            app.state.builtin_boundary.bind(build_ask_user_tool()).acall(question=prompt)
        )


def test_question_header_is_not_an_answerable_interaction_before_posting(
    world: tuple[Any, TestClient, str],
) -> None:
    """A UI read between confirmed acceptance and posting preserves the reservation."""
    from clio_agent.gact.routes.interactions import _question_from_interaction_id

    app, client, sid = world
    questions = app.state.questions
    previous = questions.before_post
    observed: list[str] = []

    def inspect_header(task: TaskSnapshot, definition: QuestionDefinition) -> None:
        assert definition.prompt == "Which sample?"
        assert not task.raw_status and task.terminal_at is None
        current = app.state.task_history.task_snapshot(task.handle)
        assert current is not None and current.details == task.details
        response = client.get(
            f"/v1/sessions/{sid}/interactions",
            params={"include_children": "true", "include_recent_resolved": "true"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["interactions"] == []
        assert response.json()["degradations"] == [
            {"reason": "question_posting_unconfirmed", "owner_session_id": sid}
        ]
        assert _question_from_interaction_id(app, "question:" + task.handle) is None
        observed.append(task.handle)

    questions.before_post = inspect_header
    try:
        receipt = post_question(app, sid)
    finally:
        questions.before_post = previous
    assert observed == [receipt["handle"]]
    current = app.state.task_history.task_snapshot(receipt["handle"])
    assert current is not None and current.raw_status == "pending"
    response = client.get(f"/v1/sessions/{sid}/interactions")
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()["interactions"] if row["task_id"] == current.handle)
    assert row["status"] == "pending" and row["actions"] == ["answer", "cancel"]


def test_ask_returns_a_durable_handle_without_waiting(world: tuple[Any, TestClient, str]) -> None:
    app, client, sid = world
    receipt = post_question(app, sid)
    assert receipt["accepted"] and receipt["kind"] == "Question"
    current = app.state.task_history.task_snapshot(receipt["handle"])
    assignment = app.state.task_history.latest_assignment(sid)
    assert current is not None and assignment is not None
    assert current.owner.session_id == sid
    assert current.owner.assignment_id == assignment.handle
    assert current.owner.parent_handle == assignment.handle
    assert current.status == "input_required" and current.terminal_at is None
    assert current.recording == "confirmed"
    definition = QuestionDefinition.model_validate(
        app.state.task_history.read_content(current.details)
    )
    assert definition.expires_at is None
    assert app.state.messages.get(sid, []) == []
    assert not app.state.turn_runner.busy(sid)
    listed = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"]
    row = next(row for row in listed if row["id"] == "question:" + current.handle)
    assert row["owner_session_id"] == sid and row["status"] == "pending"
    assert row["task_id"] == current.handle
    assert row["payload"]["task_handle"] == current.handle
    assert row["payload"]["response_mode"] == "async"


@pytest.mark.parametrize("include_children", [False, True])
def test_session_interactions_do_not_load_unrelated_retained_task_trees(
    world: tuple[Any, TestClient, str], monkeypatch: pytest.MonkeyPatch, include_children: bool
) -> None:
    """One conversation must remain usable when another retained tree cannot load."""
    app, client, sid = world
    other = client.post("/v1/sessions", json={"title": "Unrelated retained work"}).json()["id"]
    record_assignment_fixture(app, other)
    unrelated = post_question(app, other, "An unrelated decision")
    owned = post_question(app, sid)
    history = app.state.task_history
    unrelated_root = history.index.session_root(other)
    history.trees.drop(unrelated_root)
    original = history._load_tree

    def load(root: str) -> Any:
        if root == unrelated_root:
            raise RuntimeError("Unrelated retained tree cannot be loaded")
        return original(root)

    with monkeypatch.context() as request_patch:
        request_patch.setattr(history, "_load_tree", load)
        response = client.get(
            f"/v1/sessions/{sid}/interactions",
            params={"include_children": include_children, "include_recent_resolved": True},
        )
    assert response.status_code == 200, response.text
    handles = {row["task_id"] for row in response.json()["interactions"]}
    assert owned["handle"] in handles
    assert unrelated["handle"] not in handles


@pytest.mark.parametrize("decision", ["answer", "cancel"])
def test_root_question_interaction_response_uses_the_same_core_owner(
    world: tuple[Any, TestClient, str], decision: str
) -> None:
    """The UI response route must read and settle real Core content off the loop."""
    app, client, sid = world
    handle = post_question(app, sid)["handle"]
    response = client.post(
        f"/v1/sessions/{sid}/interactions/question:{handle}/respond",
        json={"action": decision, "answer": "sample A"}
        if decision == "answer"
        else {"action": decision},
    )
    assert response.status_code == 200, response.text
    current = app.state.task_history.task_snapshot(handle)
    assert current is not None and current.terminal_at is not None
    assert current.settlement == "confirmed"
    assert current.raw_status == ("answered" if decision == "answer" else "cancelled")
    interaction = response.json()["interaction"]
    assert interaction["id"] == "question:" + handle
    assert interaction["owner_session_id"] == sid
    assert interaction["status"] == current.raw_status
    assert interaction["payload"]["task_handle"] == handle
    assert interaction["payload"]["response_mode"] == "async"
    if decision == "answer":
        assert (
            QuestionReply.model_validate(app.state.task_history.read_content(current.result)).answer
            == "sample A"
        )


@pytest.mark.parametrize("decision", ["answer", "cancel"])
def test_question_response_settles_the_same_core_identity(
    world: tuple[Any, TestClient, str], decision: str
) -> None:
    app, client, sid = world
    receipt = post_question(app, sid)
    handle = receipt["handle"]
    if decision == "answer":
        response = client.post(
            f"/v1/sessions/{sid}/questions/{handle}/answer", json={"answer": "sample A"}
        )
    else:
        response = client.post(f"/v1/sessions/{sid}/questions/{handle}/cancel")
    assert response.status_code in {200, 204}, response.text
    task = app.state.task_history.task_snapshot(handle)
    assert task is not None and task.terminal_at is not None and task.settlement == "confirmed"
    assert task.raw_status == ("answered" if decision == "answer" else "cancelled")
    if decision == "answer":
        reply = QuestionReply.model_validate(app.state.task_history.read_content(task.result))
        assert reply.answer == "sample A"
    history = app.state.task_history
    notices = [item for item in history.pending_mail(sid) if item.handle == handle]
    assert len(notices) == 1
    assert notices[0].kind == ("question_answer" if decision == "answer" else "task_result")
    assert question_row(app, handle).id == handle


def test_stop_does_not_cancel_an_open_question(world: tuple[Any, TestClient, str]) -> None:
    app, client, sid = world
    receipt = post_question(app, sid)
    before = app.state.task_history.task_snapshot(receipt["handle"])
    stopped = client.post(f"/v1/sessions/{sid}/cancel")
    assert stopped.status_code == 204, stopped.text
    after = app.state.task_history.task_snapshot(receipt["handle"])
    assert after == before and after.terminal_at is None
    assert not app.state.turn_runner.busy(sid)


def test_native_restore_leaves_the_pending_question_with_its_owner(
    world: tuple[Any, TestClient, str],
) -> None:
    """Execution recovery must not reinterpret another owner's saved submission."""
    app, client, sid = world
    handle = post_question(app, sid)["handle"]
    before = app.state.task_history.task_snapshot(handle)
    assert client.portal is not None
    client.portal.call(app.state.native_tools.restore)
    assert app.state.task_history.task_snapshot(handle) == before
    pending = client.get(f"/v1/sessions/{sid}/questions", params={"status": "pending"})
    assert pending.status_code == 200, pending.text
    assert [row["task_handle"] for row in pending.json()["questions"]] == [handle]


@pytest.mark.parametrize("decision", ["answer", "cancel"])
def test_unrelated_session_cannot_answer_cancel_or_observe_question(
    world: tuple[Any, TestClient, str],
    decision: str,
) -> None:
    app, client, sid = world
    other = client.post("/v1/sessions", json={"title": "Other"}).json()["id"]
    record_assignment_fixture(app, other)
    handle = post_question(app, sid)["handle"]
    before = app.state.task_history.task_snapshot(handle)
    listed = client.get(f"/v1/sessions/{other}/interactions").json()["interactions"]
    assert not any(row["id"] == "question:" + handle for row in listed)
    denied = client.post(
        f"/v1/sessions/{other}/questions/{handle}/{decision}",
        json={"answer": "forged"} if decision == "answer" else None,
    )
    assert denied.status_code in {403, 404}, denied.text
    assert app.state.task_history.task_snapshot(handle) == before


@pytest.mark.parametrize("route", ["interaction", "question"])
def test_child_question_has_exact_owner_and_parent_attribution(
    world: tuple[Any, TestClient, str],
    route: str,
) -> None:
    app, client, sid = world
    child = record_delegation_fixture(app, sid, handle="task_child_question")
    handle = post_question(app, child.owner.session_id)["handle"]
    task = app.state.task_history.task_snapshot(handle)
    assert task.owner.session_id == child.owner.session_id
    assert task.owner.assignment_id == child.handle
    rows = client.get(f"/v1/sessions/{sid}/interactions", params={"include_children": True}).json()[
        "interactions"
    ]
    row = next(row for row in rows if row["id"] == "question:" + handle)
    assert row["owner_session_id"] == child.owner.session_id
    assert row["attended_session_id"] == sid
    assert row["task_id"] == handle
    assert row["payload"]["task_handle"] == handle
    assert row["payload"]["response_mode"] == "async"
    path = (
        f"/v1/sessions/{sid}/interactions/{row['id']}/respond"
        if route == "interaction"
        else f"/v1/sessions/{sid}/questions/{handle}/answer"
    )
    response = client.post(path, json={"answer": "child sample"})
    assert response.status_code == 200, response.text
    assert app.state.task_history.task_snapshot(handle).raw_status == "answered"
    assert not any(item.handle == handle for item in app.state.task_history.pending_mail(sid))
    assert any(
        item.handle == handle
        for item in app.state.task_history.pending_mail(child.owner.session_id)
    )


def test_core_restore_preserves_open_and_answered_question_identity(
    world: tuple[Any, TestClient, str],
) -> None:
    app, _client, sid = world
    open_handle = post_question(app, sid, "Open choice")["handle"]
    answered_handle = post_question(app, sid, "Answered choice")["handle"]
    app.state.questions.answer(answered_handle, QuestionReply(answer="retained"))
    restored = TaskHistory(app.state.arc)
    restored.restore()
    assert restored.task_snapshot(open_handle) == app.state.task_history.task_snapshot(open_handle)
    answered = restored.task_snapshot(answered_handle)
    assert answered == app.state.task_history.task_snapshot(answered_handle)
    assert QuestionReply.model_validate(restored.read_content(answered.result)).answer == "retained"
    assert len([item for item in restored.pending_mail(sid) if item.handle == answered_handle]) == 1
    QuestionCommands(restored).restore()
    assert restored.task_snapshot(open_handle).raw_status == "pending"


@pytest.mark.parametrize("route", ["question", "interaction"])
def test_invalid_answer_retains_the_question_and_prefill(
    world: tuple[Any, TestClient, str],
    route: str,
) -> None:
    app, client, sid = world
    task = app.state.questions.create(
        app.state.task_history.latest_assignment(sid).owner,
        QuestionDefinition(
            prompt="Choose a sample",
            kind="choice",
            options=({"label": "A", "value": "a"},),
            allow_freeform=False,
        ),
        invocation_id="validated-choice",
    )
    path = (
        f"/v1/sessions/{sid}/questions/{task.handle}/answer"
        if route == "question"
        else f"/v1/sessions/{sid}/interactions/question:{task.handle}/respond"
    )
    denied = client.post(
        path,
        json={"selected_options": ["unknown"], "answer": "preserve me"},
    )
    assert denied.status_code == 422, denied.text
    assert app.state.task_history.task_snapshot(task.handle) == task
    assert not any(item.handle == task.handle for item in app.state.task_history.pending_mail(sid))


def test_capabilities_advertise_normalized_interactions(world: tuple[Any, TestClient, str]) -> None:
    _app, client, _sid = world
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    assert response.json()["capabilities"]["x_clio_interactions"] is True


def test_damaged_surface_ledger_degrades_interactions_and_snapshot(tmp_path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="damaged")

    def unreadable(_session_id: str) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        raise BlobDecodeError("damaged-record")

    monkeypatch.setattr(app.state.a2ui_store, "list_wire_with_degradations", unreadable)
    with TestClient(app) as client:
        response = client.get(f"/v1/sessions/{session.id}/interactions", headers=HEADERS)
        snapshot = client.get(f"/v1/sessions/{session.id}/a2ui/surfaces", headers=HEADERS)

    assert response.status_code == snapshot.status_code == 200
    assert response.json()["interactions"] == []
    assert response.json()["degradations"][0]["reason"] == "clio_core_blob_invalid_base64"
    assert snapshot.json()["surfaces"] == []
    assert snapshot.json()["degradations"][0]["reason"] == "clio_core_blob_invalid_base64"


def test_invalid_transcript_text_degrades_surfaces_without_hiding_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt session cannot turn the global attention poll into an HTTP 500."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="invalid text")

    class UnreadableMessages(dict[str, list[Any]]):
        def get(self, key: str, default: Any = None) -> Any:
            raise UnicodeDecodeError("utf-8", b"\xa0", 0, 1, "invalid start byte")

    monkeypatch.setattr(app.state, "messages", UnreadableMessages())
    app.state.permissions["perm_keep"] = {
        "id": "perm_keep",
        "session_id": session.id,
        "status": "pending",
        "reason": "Read source data",
        "created_at": "2026-10-03T11:00:00Z",
        "tool_call": {"tool_name": "shell_bash", "input": {"command": "Get-Content data.csv"}},
    }
    with TestClient(app) as client:
        response = client.get(f"/v1/sessions/{session.id}/interactions", headers=HEADERS)
        snapshot = client.get(f"/v1/sessions/{session.id}/a2ui/surfaces", headers=HEADERS)

    assert response.status_code == snapshot.status_code == 200
    assert response.json()["interactions"][0]["id"] == "permission:perm_keep"
    assert response.json()["degradations"][0]["reason"] == "a2ui_transcript_decode_failed"
    assert snapshot.json()["surfaces"] == []
    assert snapshot.json()["degradations"][0]["reason"] == "a2ui_transcript_decode_failed"
