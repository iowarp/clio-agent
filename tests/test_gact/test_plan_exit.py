"""Plan guardrails and approval through the actual Core Question/mailbox boundary.

Legacy direct-resume and mutable question-ledger arrangements are replaced with
public answer routes, confirmed content and the sole admission mailbox.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.arc.task_types import (
    AssignmentDetails,
    QuestionDefinition,
    QuestionOption,
    TaskSnapshot,
)
from clio_agent.gact import context as _ctx
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import (
    PlanExitError,
    build_plan_exit_tool,
    maybe_pause_for_plan_exit,
)
from clio_agent.gact.transcript import EventBusTranscriptPublisher
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import Message
from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture

pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")

CLIO_A2UI_CATALOG_ID = workspace_catalog_id()


def _make_app(tmp_path: Path) -> FastAPI:
    return build_app(sessions_path=tmp_path / "s.json")


def _plan_session(
    app: Any, tmp_path: Path, *, mode: str = "plan", with_plan_file: bool = True
) -> Any:
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode=mode)
    if with_plan_file:
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Plan\n- do a thing\n", encoding="utf-8")
        app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})
    return app.state.sessions.get(sess.id)


# --------------------------------------------------------------------------- #
# The tool's two hard-error guardrails
# --------------------------------------------------------------------------- #


def _call_plan_exit(app: Any, sid: str, **kwargs: Any) -> str:
    tool = build_plan_exit_tool(SimpleNamespace(id="main"))
    token_app = _ctx.set_app(app)
    token_sid = _ctx.set_session_id(sid)
    try:
        return tool.func(**kwargs)
    finally:
        _ctx.reset(token_sid)
        _ctx.reset(token_app)


def test_plan_exit_no_plan_file_hard_errors_naming_path(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path, with_plan_file=False)
    with pytest.raises(PlanExitError) as exc:
        _call_plan_exit(app, sess.id, summary="done")
    msg = str(exc.value)
    assert "no plan file exists at" in msg
    # The message names the expected plans-dir path (the model can act on it).
    from clio_agent.gact.runtime.grant_resolver import plans_dir

    assert str(plans_dir()) in msg
    # Mode is unchanged and no pending request was stashed.
    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"
    assert not fresh.metadata.get("pending_plan_exit")


def test_plan_exit_outside_plan_mode_errors(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path, mode="edit")
    with pytest.raises(PlanExitError, match="only available in plan mode"):
        _call_plan_exit(app, sess.id, summary="done")
    assert app.state.sessions.get(sess.id).mode == "edit"


def test_plan_exit_requires_summary(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    with pytest.raises(PlanExitError, match="requires a 1-2 sentence 'summary'"):
        _call_plan_exit(app, sess.id, summary="   ")


def test_plan_exit_rejects_bad_recommended_mode(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    with pytest.raises(PlanExitError, match="recommendedMode must be one of"):
        _call_plan_exit(app, sess.id, summary="ok", recommendedMode="whatever")


def test_plan_exit_success_records_pending_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    monkeypatch.setattr("clio_agent.gact.plan_mode.observer_call_id", lambda: "call_plan_exit")
    out = _call_plan_exit(app, sess.id, summary="ship it", recommendedMode="auto", riskNotes="none")
    assert "handed back to the user" in out
    pending = app.state.sessions.get(sess.id).metadata.get("pending_plan_exit")
    assert pending["summary"] == "ship it"
    assert pending["recommended_mode"] == "auto"
    assert pending["invocation_id"] == "call_plan_exit"
    assert pending["surfaced"] is False


# --------------------------------------------------------------------------- #
# The post-forward yield seam mints the N-way approval question
# --------------------------------------------------------------------------- #


@dataclass
class ApprovalWorld:
    """A surfaced approval on real Core, with no test model execution."""

    app: FastAPI
    client: TestClient
    sid: str
    question: TaskSnapshot
    state: TurnState

    def resume_items(self) -> list[Any]:
        """Read the exact approved/revised input from the indexed Core mailbox."""
        return [
            item
            for item in self.app.state.task_history.pending_mail(self.sid)
            if item.id == "plan-answer:" + self.question.handle
        ]


def surface_approval(
    app: FastAPI, client: TestClient, sid: str, monkeypatch: pytest.MonkeyPatch
) -> ApprovalWorld:
    """Surface an existing plan through its real owner and hold subsequent model input."""
    session = app.state.sessions.get(sid)
    record_assignment_fixture(app, sid)
    user = Message(
        id="user_plan",
        session_id=sid,
        role="user",
        parts=[],
        created_at=session.created_at,
        updated_at=session.updated_at,
    )
    state = TurnState(
        app=app,
        sid=sid,
        sess=session,
        bus=app.state.bus,
        user_msg=user,
        user_text="Review this plan",
        turn_agent_id="main",
        turn_id=user.id,
        trace_id="trace_plan",
        retry_attempt_id="",
        native_images=[],
        context_frame={"id": "fixture_plan_frame"},
    )
    state.transcript = app.state.turn_transcripts.open_turn(
        sid, user.id, EventBusTranscriptPublisher(app.state.bus, sid)
    )
    # Context-frame finalization is independent of approval arbitration.
    monkeypatch.setattr("clio_agent.gact.enrichment._finalize_context_frame", lambda *a, **k: None)
    with active_turn_fixture(app, sid):
        _call_plan_exit(app, sid, summary="ship it", recommendedMode="auto")
        assert maybe_pause_for_plan_exit(state)
    history = app.state.task_history
    handles = history.session_handles(sid) & history.task_handles(kind="Question")
    assert len(handles) == 1
    question = history.task_snapshot(next(iter(handles)))
    assert question is not None and question.raw_status == "pending"
    assert app.state.sessions.get(sid).status == "waiting_user"
    assignment = history.latest_assignment(sid)
    assert assignment is not None
    paused = client.post(f"/v1/sessions/{sid}/agent-controls/{assignment.handle}/pause")
    assert paused.status_code == 200, paused.text
    assert app.state.conversations.model_paused(sid)
    return ApprovalWorld(app, client, sid, question, state)


@pytest.fixture
def approval_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ApprovalWorld]:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        session = _plan_session(app, tmp_path)
        yield surface_approval(app, client, session.id, monkeypatch)


def answer(world: ApprovalWorld, selected: list[str], text: str = "") -> dict[str, Any]:
    """Use the authenticated product answer route, not a mirrored Question mutation."""
    response = world.client.post(
        f"/v1/sessions/{world.sid}/questions/{world.question.handle}/answer",
        json={"selected_options": selected, "answer": text},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "selection,mode,approval_mode,expected_text,expects_resume",
    [
        (["auto"], "edit", "auto-edits", "Begin implementing the approved plan now.", True),
        (["interactive"], "edit", "ask", "prompted to approve each action", True),
        (["exit_only"], "edit", "ask", "", False),
        (["reject"], "plan", "ask", "add a rollback section", True),
        ([], "plan", "ask", "REJECTED", True),
    ],
    ids=["auto", "interactive", "exit-only", "reject", "empty-safe-reject"],
)
def test_approval_decision_commits_posture_and_exact_resume_input(
    approval_world: ApprovalWorld,
    selection: list[str],
    mode: str,
    approval_mode: str,
    expected_text: str,
    expects_resume: bool,
) -> None:
    """Each decision preserves its safeguards without a producer launching a turn."""
    world = approval_world
    reply = answer(world, selection, "add a rollback section" if selection == ["reject"] else "")
    session = world.app.state.sessions.get(world.sid)
    assert session.mode == mode and session.approval_mode == approval_mode
    assert not session.metadata.get("pending_plan_exit")
    assert reply["status"] == "answered"
    current = world.app.state.task_history.task_snapshot(world.question.handle)
    assert current is not None and current.terminal_at is not None
    items = world.resume_items()
    assert len(items) == int(expects_resume)
    if items:
        item = items[0]
        assert item.handle == world.question.handle
        content = world.app.state.task_history.read_content(item.content)
        assert expected_text in content["text"]
        assert str(Path(session.metadata["plan_file"])) in content["text"]
        if mode == "edit":
            assert "[STATE TRANSITION OVERRIDE]" in content["text"]
            assert "<approved-plan>\n# Plan\n- do a thing\n</approved-plan>" in content["text"]
            assert content["metadata"]["approved_plan"]["content_status"] == "complete"
            assert content["metadata"]["approved_plan"]["content"] == "# Plan\n- do a thing\n"
    # A paused owner may transiently reserve admission to inspect new mail.
    # The durable input boundary proves that no model received that input.
    assert world.app.state.conversations.model_paused(world.sid)
    assert all(
        world.app.state.task_history.committed_notification_input(world.sid, item.id) is None
        for item in items
    )
    with active_turn_fixture(world.app, world.sid):
        assert maybe_pause_for_plan_exit(world.state) is False
    # Repeated delivery of the same answer cannot mint approval or resume again.
    assert (
        answer(world, selection, "add a rollback section" if selection == ["reject"] else "")
        == reply
    )
    assert world.resume_items() == items
    history = world.app.state.task_history
    assert len(history.task_handles(kind="Question", session_id=world.sid)) == 1


def test_approval_reuses_the_reviewed_immutable_plan_artifact(
    approval_world: ApprovalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution refers to the human-reviewed version instead of resaving a new one."""
    world = approval_world
    history = world.app.state.task_history
    original = history.read_content(world.question.details)["metadata"]["artifact_ref"]
    assert original["saved"] is True
    monkeypatch.setattr(
        "clio_agent.gact.plan_reuse.save_approved_plan",
        lambda *args, **kwargs: pytest.fail("Approval minted a second plan artifact"),
    )
    answer(world, ["auto"])
    frozen = world.app.state.questions.resolution(world.question.handle)
    assert frozen["approved_plan"]["artifact_ref"] == original
    assert frozen["approved_plan"]["content"] == "# Plan\n- do a thing\n"


def test_busy_approval_retains_core_input_until_runner_admission(
    approval_world: ApprovalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy owner keeps the exact accepted approval input without another worker."""
    world = approval_world
    monkeypatch.setattr(world.app.state.turn_runner, "busy", lambda sid: True)
    answer(world, ["auto"])
    [item] = world.resume_items()
    assert item.handle == world.question.handle
    assert world.app.state.task_history.committed_notification_input(world.sid, item.id) is None
    assert world.app.state.sessions.get(world.sid).mode == "edit"


def test_clear_context_retains_core_history_and_announces_removed_surfaces(
    approval_world: ApprovalWorld,
) -> None:
    """Context reset references a fixed epoch while retaining the approval's history."""
    world = approval_world
    world.app.state.a2ui_store.apply_batch(
        world.sid,
        [
            {
                "version": "v0.9.1",
                "createSurface": {
                    "surfaceId": "plan_surface",
                    "catalogId": CLIO_A2UI_CATALOG_ID,
                },
            }
        ],
    )
    original = world.app.state.task_history.read_content(world.question.details)
    answer(world, ["auto", "clear_context"])
    history = world.app.state.task_history
    assignment = history.latest_assignment(world.sid)
    details = AssignmentDetails.model_validate(history.read_content(assignment.details))
    assert details.context_epoch == world.question.handle
    assert details.context_epoch_after == world.question.created_at
    assert history.read_content(world.question.details) == original
    assert world.app.state.sessions.get(world.sid).metadata["plan_exit_context_cleared"] is True
    deletions = [
        event.payload
        for event in world.app.state.bus._history[world.sid]
        if event.type == "a2ui.surface.deleted"
    ]
    assert deletions == [{"surface_id": "plan_surface", "reason": "plan_exit_context_cleared"}]


@pytest.mark.parametrize(
    "selection",
    [
        ["auto", "interactive", "clear_context"],
        ["auto", "clear_context", "clear_context"],
        ["clear_context"],
    ],
)
def test_plan_modifier_keeps_primary_and_duplicate_choice_checks(
    approval_world: ApprovalWorld, selection: list[str]
) -> None:
    """An auxiliary modifier grants neither two decisions nor duplicate options."""
    world = approval_world
    response = world.client.post(
        f"/v1/sessions/{world.sid}/questions/{world.question.handle}/answer",
        json={"selected_options": selection},
    )
    assert response.status_code == 422
    current = world.app.state.task_history.task_snapshot(world.question.handle)
    assert current is not None and current.raw_status == "pending"
    assert world.app.state.sessions.get(world.sid).mode == "plan"
    assert world.resume_items() == []


def test_context_modifier_forms_share_one_canonical_answer(
    approval_world: ApprovalWorld,
) -> None:
    """Old selected-option and explicit-metadata forms have one retained outcome."""
    world = approval_world
    first = answer(world, ["auto", "clear_context"])
    current = world.app.state.task_history.task_snapshot(world.question.handle)
    assert current is not None and current.result is not None
    stored = world.app.state.task_history.read_content(current.result)
    assert stored["selected_options"] == ["auto"]
    assert stored["metadata"]["clear_context"] is True
    response = world.client.post(
        f"/v1/sessions/{world.sid}/questions/{world.question.handle}/answer",
        json={"selected_options": ["auto"], "metadata": {"clear_context": True}},
    )
    assert response.status_code == 200, response.text
    assert response.json() == first
    assert len(world.resume_items()) == 1
    assert world.app.state.task_history.read_content(current.result) == stored


@pytest.mark.parametrize("selection", [["auto", "clear_context"], []])
def test_plan_modifier_does_not_relax_an_ordinary_choice(
    approval_world: ApprovalWorld, selection: list[str]
) -> None:
    """An unrelated question cannot acquire plan semantics from an option's name."""
    world = approval_world
    history = world.app.state.task_history
    assignment = history.latest_assignment(world.sid)
    assert assignment is not None
    question = world.app.state.questions.create(
        assignment.owner.model_copy(update={"parent_handle": assignment.handle}),
        QuestionDefinition(
            prompt="Choose one label.",
            kind="choice",
            source="ask_user",
            options=(
                QuestionOption(label="Auto", value="auto"),
                QuestionOption(label="Clear", value="clear_context"),
            ),
        ),
        invocation_id="ordinary-choice",
    )
    response = world.client.post(
        f"/v1/sessions/{world.sid}/questions/{question.handle}/answer",
        json={"selected_options": selection},
    )
    assert response.status_code == 422
    current = history.task_snapshot(question.handle)
    assert current is not None and current.raw_status == "pending"


def test_plan_posture_is_updated_before_approved_work_enters_mailbox(
    approval_world: ApprovalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No admitted continuation can observe the old planning constraints."""
    world = approval_world
    history = world.app.state.task_history
    original = history.record_mail
    seen: list[tuple[str, str]] = []

    def record(item: Any, **kwargs: Any) -> Any:
        if item.id == "plan-answer:" + world.question.handle:
            session = world.app.state.sessions.get(world.sid)
            seen.append((session.mode, session.approval_mode))
        return original(item, **kwargs)

    monkeypatch.setattr(history, "record_mail", record)
    answer(world, ["auto"])
    assert seen and all(posture == ("edit", "auto-edits") for posture in seen)
