"""Wait outcomes keep per-member authorization through actual recipient-input commitment."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from dspy.lm15 import ToolCallPart

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    CoordinationGrant,
    MailItem,
    QuestionDefinition,
    QuestionReply,
    TaskProgress,
)
from clio_agent.gact import context
from clio_agent.gact.agents.clio_react_record import StepRecorder
from clio_agent.gact.events import EventBus
from clio_agent.gact.native_work import now
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_declarations import build_task_compatibility_tools, build_task_tools
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.task_wait_presentation import wait_start_presentation
from clio_agent.gact.tool_observation import CollectedTaskResult
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_replacement_contracts import finish, forbid_cancel, record_task


@pytest.mark.asyncio
async def test_wait_start_uses_authorized_question_kind_without_consuming_its_answer(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Presentation reads real Core data while preserving pending answer delivery."""
    app, owner, root = await create_environment(arc, tmp_path)
    questions = QuestionCommands(owner.history)
    question = await asyncio.to_thread(
        questions.create,
        root.owner,
        QuestionDefinition(prompt="Which dataset should the child inspect?", source="native"),
        invocation_id="presented-question",
    )
    await asyncio.to_thread(questions.answer, question.handle, QuestionReply(answer="Dataset B"))
    before = await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id)
    assert any(item.kind == "question_answer" and item.handle == question.handle for item in before)
    app_token = context.set_app(app)
    sid_token = context.set_session_id(root.owner.session_id)
    try:
        assert await asyncio.to_thread(wait_start_presentation, {"tasks": question.handle}) == {
            "summary": "Waiting for your answer",
            "blocks": [],
        }
        assert await asyncio.to_thread(
            wait_start_presentation, {"tasks": [question.handle], "timeout_s": 0}
        ) == {"summary": "Checking task status", "blocks": []}
        assert await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id) == before
        assert not owner.history.acknowledged_notifications(root.owner.session_id)
    finally:
        context.reset(sid_token)
        context.reset(app_token)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["wait_tasks", "wait_agent_tasks"])
@pytest.mark.parametrize("stage", ["running", "uncertain", "completed"])
@pytest.mark.parametrize("revoke", [False, True])
async def test_status_only_wait_rechecks_rights_without_failing_its_assignment(
    arc: ARCMemory, tmp_path: Path, tool_name: str, stage: str, revoke: bool
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    app.state.arc, app.state.bus = arc, EventBus()
    app.state.task_operations = TaskOperations(owner.history, forbid_cancel)

    def prepare() -> tuple[str, str, str, CoordinationGrant]:
        peer = app.state.sessions.create(workspace_id="workspace", agent={"id": "main"})
        assignment = owner._new_assignment(
            peer.id, "Independent work", "human", invocation_id="peer"
        )
        task = record_task(owner.history, "task_status_only", session=root.owner.session_id)
        if stage == "completed":
            finish(owner.history, task)
        elif stage == "uncertain":
            owner.history.record_snapshot(
                task.model_copy(
                    update={
                        "status": "task_state_uncertain",
                        "progress": TaskProgress(message="PRIVATE OWNER EVIDENCE"),
                    }
                ),
                observation_id="uncertain-evidence",
            )
        grant = CoordinationGrant(
            id="status-only",
            granting_human="human",
            recipient_session=peer.id,
            target_session=root.owner.session_id,
            rights=("observe",),
            created_at=now(),
        )
        owner.history.record_grant(grant, observation_id="observe-accepted")
        return peer.id, assignment.handle, task.handle, grant

    sid, assignment, handle, grant = await asyncio.to_thread(prepare)
    token, session_token = context.set_app(app), context.set_session_id(sid)
    try:
        declarations = (
            build_task_tools()
            if tool_name == "wait_tasks"
            else build_task_compatibility_tools([tool_name])
        )
        tool = next(tool for tool in declarations if tool.name == tool_name)
        value = await tool.func([handle], timeout_s=0)
        assert isinstance(value, CollectedTaskResult)
        assert value.observe_handles == (handle,) and value.read_handles == ()
        assert value.notification_ids == () and "PRIVATE OWNER EVIDENCE" not in str(value)
        rows = value["outcomes"] if tool_name == "wait_tasks" else value["results"]
        assert ([row["handle"] for row in value["unfinished"]] == [handle]) is (
            stage != "completed"
        )
        if stage == "completed":
            assert rows[0]["status"] == "completed" and "result" not in rows[0]

        def record() -> None:
            if revoke:
                owner.history.record_grant(
                    grant.model_copy(update={"revoked_at": now()}), observation_id="observe-revoked"
                )
            context.set_turn_identity(
                app=app,
                session_id=sid,
                turn_id="status-wait",
                trace_id="status-wait",
                assignment_handle=assignment,
            )
            context.set_react_run(-1)
            recorder = StepRecorder(arc, sid, "main", expert_id="main")
            recorder.step_done(
                0,
                "step",
                text="",
                thinking=(),
                calls=(ToolCallPart(id="wait", name=tool_name, input={}),),
                results={"wait": (value, False)},
            )
            current = owner.history.task_snapshot(assignment)
            assert current is not None and current.terminal_at is None
            if revoke:
                assert handle not in str(recorder.read_steps())
                assert not owner.history.committed_observations(sid, "main")
                owner.history.record_mail(
                    MailItem(
                        id="independent-human",
                        recipient_session=sid,
                        sender_human="human",
                        kind="human",
                        content=owner.history.write_content(
                            sid, {"text": "Continue independent work"}
                        ),
                        created_at=now(),
                    )
                )
                recorder.commit_input(1)
                assert owner.history.acknowledged_notifications(sid) == {"independent-human"}
                assert "Continue independent work" in str(recorder.read_steps())
                assert handle not in str(recorder.read_steps())
            else:
                assert owner.history.committed_observations(sid, "main")
                assert handle in str(recorder.read_steps())
                assert not owner.history.acknowledged_notifications(sid)
            assert not owner.history.acknowledged_notifications(root.owner.session_id)

        await asyncio.to_thread(record)
    finally:
        context.reset(session_token)
        context.reset(token)
        await owner.shutdown()
        await owner.recovery.drain()
