"""Retained public behavior on real Core; no live-model acceptance is claimed."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import JsonValue

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    AssignmentDetails,
    BackendReference,
    DeferredCompletion,
    MailItem,
    QuestionDefinition,
    QuestionOption,
    QuestionReply,
    TaskSnapshot,
)
from clio_agent.gact.native_work import now
from clio_agent.gact.plan_exit_resolution import PlanApproval, normalize_plan_exit_reply
from clio_agent.gact.planning import (
    Playbook,
    PlaybookStep,
    record_playbook,
    recorded_execution_playbook,
)
from clio_agent.gact.question_surface import prepare_question_gate, resolve_plan_question
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.gact.types import Message, Part
from tests.test_arc.test_conversation_owner_contracts import create_environment


async def no_cancel(_task: TaskSnapshot, _caller: TaskCaller) -> dict[str, Any]:
    """Reads must not dispatch a cancellation."""
    pytest.fail("Result inspection attempted to cancel work")


@pytest.mark.asyncio
async def test_old_pause_metadata_cannot_hold_a_successor_after_restore(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)

    def prepare() -> TaskSnapshot:
        previous = root.model_copy(
            update={"status": "cancelled", "settlement": "confirmed", "terminal_at": now()}
        )
        owner.history.record_snapshot(previous, observation_id="previous-settled")
        successor = owner._new_assignment(
            root.owner.session_id,
            "New assignment",
            "human",
            predecessor=previous,
            invocation_id="new-request",
        )
        app.state.sessions.update(
            root.owner.session_id,
            metadata_patch={"agent_paused": True, "agent_paused_assignment": root.handle},
        )
        return successor

    try:
        successor = await asyncio.to_thread(prepare)
        owner._paused.clear()
        await owner.restore()
        latest = owner.history.latest_assignment(root.owner.session_id)
        assert latest is not None
        assert latest.handle == successor.handle
        assert not owner.model_paused(root.owner.session_id)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_deferred_stop_hook_cannot_complete_an_expired_assignment(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)

    def prepare() -> DeferredCompletion:
        output = owner.history.write_content(root.owner.session_id, {"answer": "Retained work"})
        details = owner.history.write_content(
            root.owner.session_id,
            AssignmentDetails(
                last_output=output,
                execution_deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            ).model_dump(mode="json"),
        )
        owner.history.record_snapshot(
            root.model_copy(update={"details": details, "activity": "yielded"}),
            observation_id="expired-held-output",
        )
        return DeferredCompletion(result=output)

    try:
        completion = await asyncio.to_thread(prepare)
        await owner.complete_deferred(
            root.owner.session_id, root.handle, context_epoch="", completion=completion
        )
        current = await asyncio.to_thread(owner.history.task_snapshot, root.handle)
        assert current is not None
        assert current.terminal_at is None and current.status != "completed"
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_closed_visual_answer_never_becomes_an_unaddressed_surface(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    questions = QuestionCommands(owner.history)
    definition = QuestionDefinition(
        prompt="Choose the data", source="ask_user", metadata={"a2ui_surface_id": "surface"}
    )
    try:
        first = await asyncio.to_thread(
            questions.create, root.owner, definition, invocation_id="first"
        )
        await asyncio.to_thread(questions.cancel, first.handle)
        closed = await asyncio.to_thread(
            questions.visual_question, root.owner.session_id, "surface", "question.submit"
        )
        assert (
            closed is not None
            and closed.handle == first.handle
            and closed.raw_status == "cancelled"
        )
        with pytest.raises(ValueError, match="no longer awaiting"):
            await asyncio.to_thread(
                questions.answer, closed.handle, QuestionReply(answer="Late answer")
            )
        second = await asyncio.to_thread(
            questions.create, root.owner, definition, invocation_id="second"
        )
        with pytest.raises(ValueError, match="ambiguous"):
            await asyncio.to_thread(
                questions.visual_question, root.owner.session_id, "surface", "question.submit"
            )
        answered = await asyncio.to_thread(
            questions.answer, second.handle, QuestionReply(answer="Exact answer")
        )
        assert answered.handle == second.handle and answered.raw_status == "answered"
        pending = await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id)
        assert all(item.kind != "a2ui" for item in pending)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_cancelled_plan_gate_repairs_without_duplicate_continuation(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    app.state.gact_deps = SimpleNamespace()
    sid = root.owner.session_id
    await asyncio.to_thread(app.state.sessions.update, sid, mode="plan")
    await owner.pause(TaskCaller(sid, "human", human_action=True), root.handle)
    for job in tuple(owner._controls.values()):
        await job
    await owner.recovery.drain()
    app.state.questions.before_post = lambda task, definition: prepare_question_gate(
        app, task, definition
    )
    question = await asyncio.to_thread(
        app.state.questions.create,
        root.owner.model_copy(update={"parent_handle": root.handle}),
        QuestionDefinition(
            prompt="Approve the plan",
            source="plan_exit",
            kind="choice",
            options=(
                QuestionOption(label="Approve", value="auto"),
                QuestionOption(label="Reject", value="reject"),
            ),
            allow_freeform=True,
            metadata={
                "plan_file": str(tmp_path / "plan.md"),
                "plan_approval": PlanApproval(
                    assignment=root.handle,
                    context_epoch="",
                    request={"summary": "Approve the plan", "plan_file": str(tmp_path / "plan.md")},
                ).model_dump(mode="json"),
            },
        ),
        invocation_id="plan",
    )
    assert app.state.sessions.get(sid).status == "waiting_user"
    closed = await asyncio.to_thread(app.state.questions.cancel, question.handle)
    original = app.state.sessions.update
    failed = False

    def fail_gate_release(session_id: str, **kwargs: Any) -> Any:
        nonlocal failed
        if kwargs.get("metadata_patch", {}).get("execution_gate") == "" and not failed:
            failed = True
            raise OSError("Injected gate projection failure")
        return original(session_id, **kwargs)

    monkeypatch.setattr(app.state.sessions, "update", fail_gate_release)
    try:
        with pytest.raises(OSError, match="gate projection"):
            await asyncio.to_thread(resolve_plan_question, app, closed)
        before = await asyncio.to_thread(owner.history.pending_mail, sid)
        notices = [
            item for item in before if item.kind == "task_result" and item.handle == question.handle
        ]
        assert (
            len(notices) == 1
            and app.state.sessions.get(sid).metadata["execution_gate"] == question.handle
        )
        assert not any(item.id == "plan-answer:" + question.handle for item in before)
        await asyncio.to_thread(resolve_plan_question, app, closed)
        released = app.state.sessions.get(sid)
        await asyncio.to_thread(resolve_plan_question, app, closed)
        after = await asyncio.to_thread(owner.history.pending_mail, sid)
        assert [item.id for item in after].count(notices[0].id) == 1
        assert not any(item.id == "plan-answer:" + question.handle for item in after)
        session = app.state.sessions.get(sid)
        assert session.mode == "plan" and session.metadata["execution_gate"] == ""
        assert session.status == "idle" and session == released
        assert owner.model_paused(sid)
        assert owner.history.acknowledged_notifications(sid) == frozenset()
        current_question = owner.history.task_snapshot(question.handle)
        assert current_question is not None
        assert current_question.raw_status == "cancelled"
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_approval_repair_preserves_execution_playbook_and_later_feedback(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    app.state.a2ui_store = SimpleNamespace(announce_ledger_clear=lambda *_args: None)
    app.state.messages = {sid: []}

    def replace_messages(_app: Any, session: str, messages: list[Message]) -> None:
        app.state.messages[session] = list(messages)

    app.state.gact_deps = SimpleNamespace(replace_session_messages=replace_messages)
    artifact: dict[str, JsonValue] = {
        "saved": True,
        "artifact_id": "reviewed-original",
        "sha256": "captured",
    }
    await asyncio.to_thread(app.state.sessions.update, sid, mode="plan")
    await owner.pause(TaskCaller(sid, "human", human_action=True), root.handle)
    for job in tuple(owner._controls.values()):
        await job
    await owner.recovery.drain()
    app.state.questions.before_post = lambda task, definition: prepare_question_gate(
        app, task, definition
    )
    definition = QuestionDefinition(
        prompt="Approve",
        source="plan_exit",
        kind="choice",
        allow_freeform=True,
        options=(
            QuestionOption(label="Approve", value="auto"),
            QuestionOption(label="Clear context", value="clear_context"),
        ),
        metadata={
            "plan_file": str(tmp_path / "plan.md"),
            "plan_content": "Approved original",
            "plan_content_status": "available",
            "artifact_ref": artifact,
            "plan_approval": PlanApproval(
                assignment=root.handle,
                context_epoch="",
                request={"summary": "Approve", "plan_file": str(tmp_path / "plan.md")},
            ).model_dump(mode="json"),
        },
    )
    question = await asyncio.to_thread(
        app.state.questions.create,
        root.owner.model_copy(update={"parent_handle": root.handle}),
        definition,
        invocation_id="approve",
    )
    assert app.state.sessions.get(sid).status == "waiting_user"
    await asyncio.to_thread(
        record_playbook, app, sid, Playbook("Plan", (PlaybookStep("Implement"),))
    )
    closed = await asyncio.to_thread(
        app.state.questions.answer,
        question.handle,
        normalize_plan_exit_reply(
            definition,
            QuestionReply(selected_options=("auto", "clear_context"), responder_human="human"),
        ),
    )
    earlier = datetime.fromisoformat(question.created_at) - timedelta(seconds=1)
    app.state.messages[sid] = [
        Message(
            id="old",
            session_id=sid,
            role="user",
            created_at=earlier.isoformat(),
            updated_at=earlier.isoformat(),
            parts=[Part(type="text", text="Covered planning input")],
        )
    ]
    original = app.state.sessions.update
    failed = False

    def fail_release(session: str, **kwargs: Any) -> Any:
        nonlocal failed
        if kwargs.get("metadata_patch", {}).get("execution_gate") == "" and not failed:
            failed = True
            raise OSError("Injected release failure after durable resume")
        return original(session, **kwargs)

    monkeypatch.setattr(app.state.sessions, "update", fail_release)
    try:
        with pytest.raises(OSError, match="release failure"):
            await asyncio.to_thread(resolve_plan_question, app, closed)
        first_playbook = recorded_execution_playbook(app.state.sessions.get(sid))
        assert first_playbook is not None and first_playbook.name == "Plan"
        later = datetime.fromisoformat(question.created_at) + timedelta(seconds=1)
        feedback = Message(
            id="newer",
            session_id=sid,
            role="user",
            created_at=later.isoformat(),
            updated_at=later.isoformat(),
            parts=[Part(type="text", text="Keep this accepted feedback")],
        )
        app.state.messages[sid].append(feedback)
        await asyncio.to_thread(resolve_plan_question, app, closed)
        await asyncio.to_thread(resolve_plan_question, app, closed)
        assert app.state.messages[sid] == [feedback]
        assert recorded_execution_playbook(app.state.sessions.get(sid)) == first_playbook
        pending = await asyncio.to_thread(owner.history.pending_mail, sid)
        resumes = [item for item in pending if item.id == "plan-answer:" + question.handle]
        assert len(resumes) == 1
        content = await asyncio.to_thread(owner.history.read_content, resumes[0].content)
        assert isinstance(content, dict)
        metadata = content["metadata"]
        assert isinstance(metadata, dict)
        approved_plan = metadata["approved_plan"]
        assert isinstance(approved_plan, dict)
        assert approved_plan["artifact_ref"] == artifact
        assert app.state.sessions.get(sid).metadata["execution_gate"] == ""
        assert app.state.sessions.get(sid).message_count == 1
        assert app.state.sessions.get(sid).status == "idle"
        assert owner.model_paused(sid)
        assignment = await asyncio.to_thread(owner.history.latest_assignment, sid)
        assert assignment is not None and assignment.details is not None
        details = AssignmentDetails.model_validate(
            await asyncio.to_thread(owner.history.read_content, assignment.details)
        )
        assert details.context_epoch == question.handle
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_pause_cancel_releases_only_that_assignments_model_hold(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human", human_action=True)
    try:
        await owner.pause(caller, root.handle)
        jobs = tuple(owner._controls.values())
        if jobs:
            await asyncio.gather(*jobs)
        assert owner.model_paused(root.owner.session_id)
        await owner.cancel(caller, root.handle)
        jobs = tuple(owner._controls.values())
        if jobs:
            await asyncio.gather(*jobs)
        task = await asyncio.to_thread(owner.history.task_snapshot, root.handle)
        assert task is not None and task.status == "cancelled" and task.settlement == "confirmed"
        assert not owner.model_paused(root.owner.session_id)
        assert app.state.sessions.get(root.owner.session_id).metadata["agent_paused"] is False
        content = await asyncio.to_thread(
            owner.history.write_content, root.owner.session_id, "Next request"
        )
        incoming = MailItem(
            id="next",
            recipient_session=root.owner.session_id,
            sender_human="human",
            kind="human",
            content=content,
            created_at=now(),
        )
        assert owner.eligible(root.owner.session_id, incoming)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_resume_without_hold_does_not_rewrite_busy_activity_or_mail(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    busy = root.model_copy(update={"activity": "busy"})
    await asyncio.to_thread(owner.history.record_snapshot, busy, observation_id="busy")
    try:
        before = await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id)
        outcome = await owner.resume(
            TaskCaller(root.owner.session_id, "human", human_action=True), root.handle
        )
        current = await asyncio.to_thread(owner.history.task_snapshot, root.handle)
        assert current is not None
        assert outcome["resumed"] is False and current.activity == "busy"
        assert await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id) == before
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_explicit_assignment_deadline_survives_parent_waiter_absence(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    owner.start()
    owner._restoring = True  # Hold inference; a queued assignment still has its explicit budget.
    settled = asyncio.Event()
    remove = None
    try:
        accepted = await owner.spawn(
            TaskCaller(root.owner.session_id, "human"),
            "Bounded queued assignment",
            "worker",
            "deadline",
            start=False,
            execution_deadline=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        )
        handle = accepted["handle"]

        def changed(_revision: int) -> None:
            task = owner.history.task_snapshot(handle)
            if task is not None and task.terminal_at is not None:
                asyncio_loop.call_soon_threadsafe(settled.set)

        asyncio_loop = asyncio.get_running_loop()
        remove = owner.history.listen(changed, handles=(handle,))
        changed(0)
        await asyncio.wait_for(settled.wait(), 5)
        child = await asyncio.to_thread(owner.history.task_snapshot, handle)
        assert child is not None
        assert child.status == "cancelled" and child.settlement == "confirmed"
        assert child.cancellation_requested_at is not None
        parent = owner.history.task_snapshot(root.handle)
        assert parent is not None
        assert parent.cancellation_requested_at is None
    finally:
        if remove is not None:
            remove()
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_uncertain_backend_evidence_is_readable_without_final_collection(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)

    def record() -> TaskSnapshot:
        raw = owner.history.write_content(
            root.owner.session_id,
            {"status": "completed", "result": {"structuredContent": {"value": 42}}},
        )
        task = TaskSnapshot(
            handle="task_external",
            kind="MCP",
            owner=root.owner,
            label="External work",
            description="Inspect selected data",
            description_missing=False,
            invocation_id="external",
            tool="inspect",
            backend=BackendReference(owner="external_mcp", server="data", task_id="remote-1"),
            raw_status="completed",
            status="task_state_uncertain",
            settlement="unknown",
            backend_observation=raw,
            created_at=now(),
            updated_at=now(),
            attention_episode="lost-cleanup",
        )
        owner.history.record_snapshot(task, observation_id="external-unknown")
        assert task.attention_episode is not None
        publish_task_notice(owner.history, task, episode=task.attention_episode)
        return task

    try:
        task = await asyncio.to_thread(record)
        result = await asyncio.to_thread(
            TaskOperations(owner.history, no_cancel).result,
            TaskCaller(root.owner.session_id, "human"),
            task.handle,
        )
        assert result["pending"] is True and result["evidence_is_final"] is False
        assert (
            result["result_kind"] == "backend_evidence"
            and result["result"]["result"]["structuredContent"]["value"] == 42
        )
        current = await asyncio.to_thread(owner.history.task_snapshot, task.handle)
        assert current is not None
        assert (
            current.result is None
            and current.terminal_at is None
            and current.settlement == "unknown"
        )
        assert owner.history.acknowledged_notifications(root.owner.session_id) == frozenset()
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_uncertain_cancellation_retires_waiter_and_late_proof_settles_parent(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    owner.start()
    owner._restoring = True

    def accept_member() -> TaskSnapshot:
        task = TaskSnapshot(
            handle="task_member",
            kind="MCP",
            owner=root.owner,
            invocation_id="member",
            tool="work",
            label="Member",
            status="running",
            created_at=now(),
            updated_at=now(),
        )
        owner.history.record_snapshot(task, observation_id="member-accepted")
        return task

    member = await asyncio.to_thread(accept_member)

    async def cancel_member(task: TaskSnapshot) -> dict[str, Any]:
        uncertain = task.model_copy(
            update={
                "status": "task_state_uncertain",
                "settlement": "unknown",
                "attention_episode": "cleanup-proof-lost",
                "cancellation_requested_at": now(),
            }
        )
        await asyncio.to_thread(
            owner.history.record_snapshot, uncertain, observation_id="member-cancel-request"
        )
        return {"handle": task.handle, "cancellation_requested": True}

    owner.cancel_work = cancel_member
    remove = None
    try:
        await owner.cancel(
            TaskCaller(root.owner.session_id, "human", human_action=True), root.handle
        )
        control = owner._controls.get(root.handle)
        assert control is not None
        await asyncio.wait_for(asyncio.shield(control), 5)
        parent = await asyncio.to_thread(owner.history.task_snapshot, root.handle)
        assert parent is not None
        assert parent.status == "task_state_uncertain" and parent.terminal_at is None
        assert root.handle in owner._cancel_watches
        assert owner.history.acknowledged_notifications(root.owner.session_id) == frozenset()
        settled = asyncio.Event()
        known_cleanup = asyncio.Event()
        loop = asyncio.get_running_loop()

        def parent_changed(_revision: int) -> None:
            current = owner.history.task_snapshot(root.handle)
            if current is not None and current.terminal_at is not None:
                loop.call_soon_threadsafe(settled.set)
            elif (
                current is not None
                and current.status == "active"
                and current.attention_episode is None
            ):
                loop.call_soon_threadsafe(known_cleanup.set)

        remove = owner.history.listen(parent_changed, handles=(root.handle,))
        current_member = await asyncio.to_thread(owner.history.task_snapshot, member.handle)
        assert current_member is not None
        progressing = current_member.model_copy(
            update={"status": "running", "settlement": "cleanup_pending", "attention_episode": None}
        )
        await asyncio.to_thread(
            owner.history.record_snapshot, progressing, observation_id="member-cleanup-visible"
        )
        await asyncio.wait_for(known_cleanup.wait(), 5)
        snapshot = await TaskOperations(owner.history, no_cancel).wait(
            TaskCaller("human-console", "human", human_action=True), root.handle, timeout_s=0
        )
        assert [row["handle"] for row in snapshot["unfinished"]] == [root.handle] and snapshot[
            "outcomes"
        ] == []
        assert snapshot["timed_out"] is True and not settled.is_set()
        proof = current_member.model_copy(
            update={
                "status": "cancelled",
                "raw_status": "cancelled",
                "settlement": "confirmed",
                "terminal_at": now(),
            }
        )
        await asyncio.to_thread(
            owner.history.record_snapshot, proof, observation_id="actual-member-cleanup"
        )
        await asyncio.wait_for(settled.wait(), 5)
        final = await asyncio.to_thread(owner.history.task_snapshot, root.handle)
        assert final is not None
        assert final.status == "cancelled" and final.settlement == "confirmed"
        assert root.handle not in owner._cancel_watches
    finally:
        if remove is not None:
            remove()

        def settle_fixture() -> None:
            current = owner.history.task_snapshot(member.handle)
            if current is not None and current.terminal_at is None:
                owner.history.record_snapshot(
                    current.model_copy(
                        update={
                            "status": "cancelled",
                            "settlement": "confirmed",
                            "terminal_at": now(),
                        }
                    ),
                    observation_id="fixture-cleanup",
                )

        await asyncio.to_thread(settle_fixture)
        await owner.shutdown()
        await owner.recovery.drain()
