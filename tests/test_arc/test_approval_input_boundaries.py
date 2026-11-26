"""Approval posting and native answer input use the real Core owners."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionDefinition, QuestionOption, QuestionReply
from clio_agent.gact import context
from clio_agent.gact.agents.clio_react_record import StepRecorder
from clio_agent.gact.conversation_assignments import (
    ConversationOutcome,
    ConversationSegment,
    _finish_segment,
)
from clio_agent.gact.events import EventBus
from clio_agent.gact.plan_exit_resolution import PlanApproval
from clio_agent.gact.question_surface import (
    answer_question,
    prepare_question_gate,
    render_answer_input,
)
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_hook_approval_contracts import PNG, prepared


@pytest.mark.asyncio
async def test_plan_gate_recovers_a_header_when_installation_failed(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    questions = app.state.questions = QuestionCommands(owner.history)
    app.state.bus, app.state.gact_deps = EventBus(), SimpleNamespace()
    questions.before_post = lambda task, definition: prepare_question_gate(app, task, definition)
    definition = QuestionDefinition(
        prompt="Review this plan",
        source="plan_exit",
        kind="choice",
        options=(QuestionOption(label="Revise", value="reject"),),
        metadata={
            "plan_approval": PlanApproval(
                assignment=root.handle, context_epoch="", request={"summary": "Original plan"}
            ).model_dump(mode="json")
        },
    )
    update = app.state.sessions.update
    failed = False

    def fail_install(sid: str, **kwargs: Any) -> Any:
        nonlocal failed
        if kwargs.get("metadata_patch", {}).get("execution_gate") and not failed:
            failed = True
            raise OSError("Injected gate installation failure")
        return update(sid, **kwargs)

    monkeypatch.setattr(app.state.sessions, "update", fail_install)
    try:
        with pytest.raises(OSError, match="installation failure"):
            await asyncio.to_thread(
                questions.create, root.owner, definition, invocation_id="plan-call"
            )
        handles = await asyncio.to_thread(owner.history.task_handles, kind="Question")
        assert len(handles) == 1
        handle = next(iter(handles))
        header = await asyncio.to_thread(owner.history.task_snapshot, handle)
        assert header.raw_status == "" and header.details is not None
        assert app.state.sessions.get(root.owner.session_id).metadata.get("execution_gate") is None
        original = await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id)
        await asyncio.to_thread(owner.history.restore)
        await asyncio.to_thread(questions.restore)
        posted = await asyncio.to_thread(owner.history.task_snapshot, handle)
        assert posted.raw_status == "pending" and posted.handle == header.handle
        assert await asyncio.to_thread(owner.history.read_content, posted.details) == (
            definition.model_dump(mode="json")
        )
        assert app.state.sessions.get(root.owner.session_id).metadata["execution_gate"] == handle
        assert (
            await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id) == original
        )
    finally:
        for handle in owner.history.task_handles(kind="Question", active_only=True):
            await asyncio.to_thread(questions.cancel, handle)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_posting_installs_gate_before_answer_and_agent_cannot_approve_it(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    questions = app.state.questions = QuestionCommands(owner.history)
    observed = []

    def before_post(task: Any, definition: QuestionDefinition) -> None:
        prepare_question_gate(app, task, definition)
        assert owner.history.task_snapshot(task.handle).raw_status == ""
        assert (
            app.state.sessions.get(root.owner.session_id).metadata["execution_gate"] == task.handle
        )
        observed.append(task.handle)

    questions.before_post = before_post
    task = await asyncio.to_thread(
        questions.create,
        root.owner,
        QuestionDefinition(
            prompt="Approve?",
            source="plan_exit",
            kind="choice",
            options=(QuestionOption(label="Approve", value="auto"),),
            metadata={
                "plan_approval": PlanApproval(
                    assignment=root.handle, context_epoch="", request={}
                ).model_dump(mode="json")
            },
        ),
        invocation_id="approval",
    )
    try:
        assert observed == [task.handle]
        with pytest.raises(PermissionError, match="authenticated human"):
            await asyncio.to_thread(
                answer_question,
                app,
                TaskCaller(root.owner.session_id, "human"),
                task.handle,
                QuestionReply(selected_options=("auto",)),
            )
        assert owner.history.task_snapshot(task.handle).raw_status == "pending"
    finally:
        await asyncio.to_thread(questions.cancel, task.handle)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_explicit_complete_cannot_bypass_approval_or_replace_captured_output(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.permission_gate import resolve_permission

    app, owner, root, original, handle = await prepared(arc, tmp_path, monkeypatch, event="Stop")
    sid = root.owner.session_id
    try:
        await asyncio.to_thread(
            _finish_segment,
            owner,
            ConversationSegment(sid, root, "attempt", original, "Original input"),
            ConversationOutcome({"answer": "Later different output"}, disposition="complete"),
        )
        assert owner.history.task_snapshot(root.handle).terminal_at is None
        await asyncio.to_thread(resolve_permission, app, handle, "allow")
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(owner._controls.values()))
        settled = owner.history.task_snapshot(root.handle)
        assert settled.status == "completed"
        assert await asyncio.to_thread(owner.history.read_content, settled.result) == {
            "answer": "Approved exact output"
        }
        assert app.state.sessions.get(sid).metadata["execution_gate"] == ""
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_answer_renderer_preserves_native_media_without_another_message(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    questions = app.state.questions = QuestionCommands(owner.history)
    task = await asyncio.to_thread(
        questions.create,
        root.owner,
        QuestionDefinition(prompt="Show the image"),
        invocation_id="image",
    )
    await asyncio.to_thread(
        questions.answer,
        task.handle,
        QuestionReply(
            answer="Inspect this image",
            parts=({"type": "image", "data": PNG, "media_type": "image/png"},),
        ),
    )
    sid = root.owner.session_id
    before = await asyncio.to_thread(owner.history.pending_mail, sid)
    try:
        rendered = await asyncio.to_thread(
            render_answer_input,
            app,
            TaskCaller(sid, "human"),
            task.handle,
            "Question answered: inspect this image",
        )
        assert rendered["actor"] == "algorithm" and rendered["source"] == "question_answer"
        assert rendered["media"][0]["type"] == "image" and PNG in rendered["media"][0]["data"]
        assert await asyncio.to_thread(owner.history.pending_mail, sid) == before
        assert owner.history.acknowledged_notifications(sid) == frozenset()
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_native_answer_uses_one_recipient_input_for_automatic_and_explicit_delivery(
    arc: ARCMemory,
    tmp_path: Path,
    explicit: bool,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    questions = app.state.questions = QuestionCommands(owner.history)
    task = await asyncio.to_thread(
        questions.create,
        root.owner,
        QuestionDefinition(prompt="Inspect"),
        invocation_id="native-answer",
    )
    await asyncio.to_thread(
        questions.answer,
        task.handle,
        QuestionReply(
            answer="Use this image",
            parts=({"type": "image", "data": PNG, "media_type": "image/png"},),
        ),
    )
    sid = root.owner.session_id

    def collect() -> None:
        notice = next(
            item for item in owner.history.pending_mail(sid) if item.handle == task.handle
        )
        token = context.set_app(app)
        try:
            context.set_turn_identity(
                app=app,
                session_id=sid,
                turn_id="answer-turn",
                trace_id="answer-trace",
                assignment_handle=root.handle,
            )
            recorder = StepRecorder(arc, sid, "main", expert_id="main")
            if explicit:
                content = {
                    "call_id": "collect",
                    "text": "Question answered",
                    "is_error": False,
                    "notification_ids": [notice.id],
                    "task_read_handles": [task.handle],
                }
                recorder._write(
                    "tool_call",
                    {"id": "collect", "name": "get_task_result", "args": {"handle": task.handle}},
                    1,
                    "step",
                )
                reference = recorder._write("observation", content, 1, "step")
                recorder._commit_collection(reference, content, (notice.id,), "collect")
            else:
                recorder.commit_input(1)
            committed = owner.history.committed_notification_input(sid, notice.id)
            assert committed is not None and committed.notification_ids == (notice.id,)
            assert len(committed.rendered_content) == 1
            rendered = owner.history.read_content(committed.rendered_content[0])
            assert PNG in rendered["media"][0]["data"]
            assert owner.history.acknowledged_notifications(sid) == frozenset({notice.id})
            assert not owner.history.pending_mail(sid)
            assert app.state.messages == {}
            recorder.commit_input(2)
            assert owner.history.committed_notification_input(sid, notice.id) == committed
        finally:
            context.reset(token)

    try:
        await asyncio.to_thread(collect)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_human_can_wait_for_own_assignment_but_agent_cannot_deadlock_itself(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    _, owner, root = await create_environment(arc, tmp_path)

    async def unused_cancel(_task: Any, _caller: TaskCaller) -> dict[str, Any]:
        pytest.fail("A read-only wait cannot request cancellation")

    operations = TaskOperations(owner.history, unused_cancel)
    sid = root.owner.session_id
    try:
        human = await operations.wait(
            TaskCaller(sid, "human", human_action=True), root.handle, timeout_s=0
        )
        agent = await operations.wait(TaskCaller(sid, "human"), root.handle, timeout_s=0)
        assert human["outcomes"] == [] and human["timed_out"] is True
        assert [row["handle"] for row in human["unfinished"]] == [root.handle]
        assert agent["outcomes"] == [
            {"handle": root.handle, "error": "cannot_wait_for_own_active_assignment"}
        ]
        assert not agent["unfinished"]
        assert owner.history.task_snapshot(root.handle).terminal_at is None
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
