"""Conversation-owned cancellation and spawn contracts against real Core."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    AssignmentDetails,
    BackendReference,
    MailItem,
    MailKind,
    QuestionDefinition,
    QuestionReply,
    TaskAlias,
    TaskOwner,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.gact import context
from clio_agent.gact.agents.clio_react_record import StepRecorder
from clio_agent.gact.agents.resolution import (
    AGENT_RESOLUTION_KEY,
    SUBAGENT_KEY,
    session_resolution,
)
from clio_agent.gact.conversations import ConversationOutcome, Conversations, ConversationSegment
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.events import EventBus
from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks
from clio_agent.gact.native_work import now
from clio_agent.gact.provenance_wiring import install_session_lifecycle_observer
from clio_agent.gact.question_events import QuestionEvents
from clio_agent.gact.question_surface import answer_question
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.semantic_events import NoopSemanticTraceBackend, SemanticEventSink
from clio_agent.gact.session_input import withdraw_message_inputs
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.spawn_context import CreatePreparedChild
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.turn_runner import TurnRunner
from clio_agent.plugins.service import PluginService
from clio_agent.tools.task_client import TaskSubmission, restore_native_call


@pytest.mark.asyncio
async def test_spawn_strategy_is_validated_before_child_creation_and_immutable(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    before = len(app.state.sessions.list())
    invalid = await owner.spawn(
        caller, "Draft", "writer", "bad", strategy={"variant": "best_of_n", "n": 2}
    )
    assert invalid["accepted"] is False and invalid["reason"] == "invalid_strategy"
    assert len(app.state.sessions.list()) == before
    strategy = {"variant": "refine", "n": 2, "rubric": "Clear answer", "judge": "user"}
    accepted = await owner.spawn(
        caller, "Original child assignment", "writer", "good", strategy=strategy
    )
    child = app.state.sessions.get(accepted["session_id"])
    queued = owner.history.task_snapshot(accepted["handle"])
    assert queued is not None and queued.activity == "queued"
    assert child is not None and child.metadata["variant_strategy"]["judge"] == "user"
    assert child.metadata["variant_strategy"]["reward"]["instructions"] == "Clear answer"
    assert (
        await owner.spawn(caller, "Original child assignment", "writer", "good", strategy=strategy)
        == accepted
    )
    with pytest.raises(ValueError, match="different strategy"):
        await owner.spawn(
            caller,
            "Original child assignment",
            "writer",
            "good",
            strategy={**strategy, "rubric": "Different"},
        )
    assert len(app.state.sessions.list()) == before + 1
    await owner.recovery.drain()


async def create_environment(
    arc: ARCMemory, tmp_path: Path
) -> tuple[Any, Conversations, TaskSnapshot]:
    """Build finite store data off-loop and async resources on the active loop."""

    def records() -> tuple[Any, TaskSnapshot]:
        sessions = SessionStore(path=tmp_path / "sessions.json")
        session = sessions.create(workspace_id="workspace", agent={"id": "main"})
        history, stamp = TaskHistory(arc), now()
        root = TaskSnapshot(
            handle="task_root",
            kind="Session",
            tool="conversation",
            owner=TaskOwner(human_id="human", session_id=session.id, assignment_id="task_root"),
            label="Parent",
            description="Original parent assignment",
            description_missing=False,
            invocation_id="human-assignment",
            created_at=stamp,
            updated_at=stamp,
            status="active",
        )
        history.record_snapshot(root, observation_id="root")
        return SimpleNamespace(
            state=SimpleNamespace(sessions=sessions, task_history=history, human_principal="human")
        ), root

    app, root = await asyncio.to_thread(records)
    app.state.arc, app.state.bus, app.state.messages = arc, EventBus(), {}
    app.state.questions = QuestionCommands(app.state.task_history)
    # Core recovery's readiness restores question views too, as the server wires it.
    app.state.question_events = QuestionEvents(app)
    # A turn resolves its session's plugin agent (the hermetic default marketplace).
    app.state.plugin_service = PluginService()
    # Pin the root now, so preparing a child later only reads the pin.
    await asyncio.to_thread(session_resolution, app, root.owner.session_id)
    app.state.semantic_event_sink = SemanticEventSink(
        bus=app.state.bus, trace_backend=NoopSemanticTraceBackend()
    )
    arc.set_highway_sink(app.state.semantic_event_sink.emit)
    install_session_lifecycle_observer(app)

    async def unavailable_model(segment: ConversationSegment) -> ConversationOutcome:
        pytest.fail(f"No inference in this test: {segment.session_id}")

    async def cancel_work(task: TaskSnapshot) -> dict[str, Any]:
        pytest.fail(f"Unexpected cancellation dispatch: {task.handle}")

    def prepare_child(
        parent: str,
        agent: str,
        scope: dict[str, Any] | None,
        model: str = "",
    ) -> CreatePreparedChild:
        del model
        # Pinned like a defined subagent, so no later first use re-pins the child to
        # the default agent and rewrites its agent id.
        pin = {
            AGENT_RESOLUTION_KEY: session_resolution(app, parent).digest,
            SUBAGENT_KEY: agent,
        }

        def create(
            creation_id: str,
            creation_digest: str,
            initial_metadata: dict[str, Any],
            approval_profile: str | None,
        ) -> Any:
            return app.state.sessions.create(
                workspace_id="workspace",
                title=str(initial_metadata.get("subagent_name") or ""),
                parent_session_id=parent,
                agent={"id": agent},
                # As spawn_context.prepare_child_session records it: the spawn's target
                # survives the child's own agent pin.
                metadata={**(scope or {}), **initial_metadata, **pin, "spawn_target": agent},
                approval_profile=approval_profile or "",
                creation_id=creation_id,
                creation_digest=creation_digest,
                defer_initialization=True,
            )

        return create

    async def admit(sid: str) -> tuple[str, Any] | None:
        return await app.state.conversations._admit(sid)

    app.state.turn_runner = TurnRunner(app, admit)
    app.state.turn_runner.bind_loop(asyncio.get_running_loop())
    owner = Conversations(
        app,
        app.state.task_history,
        CoreRecovery(app.state.task_history),
        unavailable_model,
        cancel_work,
        prepare_child,
        lambda _sid: None,
    )
    app.state.conversations = owner
    await owner.restore()
    return app, owner, root


@pytest.mark.asyncio
async def test_unconfirmed_cancellation_holds_existing_mail_before_admission(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control effects close admission before their deferred Core write can run."""
    _, owner, root = await create_environment(arc, tmp_path)
    history, sid = owner.history, root.owner.session_id
    deferred: list[Callable[[], object]] = []
    original = owner.recovery.defer_recording

    def delay_recording(identity: str, operation: Callable[[], object]) -> None:
        if identity == "conversation-cancel:" + root.handle:
            deferred.append(operation)
        else:
            original(identity, operation)

    def accept() -> MailItem:
        return history.record_mail(
            MailItem(
                id="human-before-cancel-confirmation",
                recipient_session=sid,
                sender_human="human",
                kind="human",
                content=history.write_content(sid, {"text": "New human assignment"}),
                created_at=now(),
            )
        )

    mail = await history.io.run(accept)
    monkeypatch.setattr(owner.recovery, "defer_recording", delay_recording)
    try:
        acknowledgement = await owner.cancel(
            TaskCaller(sid, "human", human_action=True), root.handle
        )
        assert acknowledgement["cancellation_requested"] is True
        assert acknowledgement["recording"] == "unconfirmed"
        assert len(deferred) == 1
        current = history.task_snapshot(root.handle)
        assert current is not None and current.cancellation_requested_at is None
        assert owner.eligible(sid, mail) is False
        assert await history.io.run(history.pending_mail, sid) == (mail,)
        assert history.acknowledged_notifications(sid) == frozenset()
    finally:
        for operation in deferred:
            await history.io.run(operation)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_human_arrival_during_cancellation_admits_after_settlement(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """A vetoed Core-pending arrival must not require another message to wake it."""
    from dspy.lm15 import Message as ModelMessage

    app, owner, root = await create_environment(arc, tmp_path)
    history = owner.history
    dispatched, received = asyncio.Event(), asyncio.Event()
    sid = root.owner.session_id
    stamp = now()
    child = TaskSnapshot(
        handle="task_settling_child",
        kind="MCP",
        tool="external",
        owner=root.owner.model_copy(update={"parent_handle": root.handle}),
        label="Settling external work",
        invocation_id="child-call",
        created_at=stamp,
        updated_at=stamp,
        status="running",
    )
    await asyncio.to_thread(history.record_snapshot, child, observation_id="child-start")

    async def request_cancel(task: TaskSnapshot) -> dict[str, Any]:
        assert task.handle == child.handle
        dispatched.set()
        return {"handle": task.handle, "cancellation_requested": True}

    observed: list[ConversationSegment] = []

    async def model(segment: ConversationSegment) -> ConversationOutcome:
        def commit_input() -> None:
            token = context.set_app(app)
            try:
                recorder = StepRecorder(arc, sid, "main", expert_id="main")
                recorder.user_message(ModelMessage.user(segment.text))
                recorder.commit_input(0)
            finally:
                context.reset(token)

        await asyncio.to_thread(commit_input)
        observed.append(segment)
        received.set()
        return ConversationOutcome({"answer": segment.text}, disposition="complete")

    owner.cancel_work = request_cancel
    owner.run_segment = model
    owner.start()

    def accept() -> None:
        history.record_mail(
            MailItem(
                id="human-during-cancellation",
                recipient_session=sid,
                sender_human="human",
                kind="human",
                content=history.write_content(sid, "Continue with the new human assignment"),
                created_at=now(),
            )
        )

    def settle_child() -> None:
        current = history.task_snapshot(child.handle)
        if current is not None and current.terminal_at is not None:
            return
        history.record_snapshot(
            child.model_copy(
                update={
                    "status": "cancelled",
                    "raw_status": "cancelled",
                    "settlement": "confirmed",
                    "terminal_at": now(),
                }
            ),
            observation_id="child-cleanup-confirmed",
        )

    try:
        await owner.cancel(TaskCaller(sid, "human", human_action=True), root.handle)
        await dispatched.wait()
        await asyncio.to_thread(accept)
        await asyncio.sleep(0)
        assert not observed
        await asyncio.to_thread(settle_child)
        await received.wait()
        assert len(observed) == 1 and observed[0].text == "Continue with the new human assignment"
        assert observed[0].assignment.handle != root.handle
        current = await asyncio.to_thread(history.task_snapshot, root.handle)
        assert current is not None and current.status == "cancelled" and current.terminal_at
        assert "human-during-cancellation" in history.acknowledged_notifications(sid)
    finally:
        await asyncio.to_thread(settle_child)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_restart_of_failed_parent_settles_only_its_active_descendants(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    """A terminal failed parent cannot restart above live children it already accepted."""
    from tests.test_gact.task_records import record_assignment_fixture, record_delegation_fixture

    app, owner, root = await create_environment(arc, tmp_path)
    history = owner.history
    children = []
    for index in range(3):
        children.append(
            await asyncio.to_thread(
                record_delegation_fixture,
                app,
                root.owner.session_id,
                handle=f"task_restart_child_{index}",
            )
        )
    unrelated_session = await asyncio.to_thread(
        app.state.sessions.create,
        workspace_id="workspace",
        agent={"id": "unrelated"},
        title="Unrelated work",
    )
    unrelated = await asyncio.to_thread(
        record_assignment_fixture, app, unrelated_session.id, handle="task_restart_unrelated"
    )
    stamp = now()
    failed = root.model_copy(
        update={
            "status": "failed",
            "raw_status": "failed",
            "activity": "",
            "settlement": "confirmed",
            "terminal_at": stamp,
            "updated_at": stamp,
            "supported_actions": ("get_result", "message", "restart"),
        }
    )
    await asyncio.to_thread(history.record_snapshot, failed, observation_id="failed-parent")

    try:
        reply = await owner.restart(
            TaskCaller(root.owner.session_id, "human", human_action=True), root.handle
        )
        assert reply["restart_requested"] is True
        restart_job = owner._controls["restart:" + root.handle]
        await asyncio.wait_for(asyncio.shield(restart_job), 5)

        successor = await history.io.run(history.latest_assignment, root.owner.session_id)
        assert successor is not None and successor.handle != root.handle
        successor_details = AssignmentDetails.model_validate(
            await history.io.run(history.read_content, successor.details)
        )
        assert successor_details.predecessor == root.handle
        for child in children:
            settled = await history.io.run(history.task_snapshot, child.handle)
            assert (
                settled is not None
                and settled.terminal_at is not None
                and settled.status == "cancelled"
                and settled.settlement == "confirmed"
            )
        still_active = await history.io.run(history.task_snapshot, unrelated.handle)
        assert still_active is not None and still_active.terminal_at is None
        assert still_active.status == "active"
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_restart_does_not_launch_when_terminal_parent_descendant_settlement_is_unknown(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    """An unknown physical descendant outcome keeps the old lineage and blocks restart."""
    app, owner, root = await create_environment(arc, tmp_path)
    history = owner.history
    stamp = now()
    external = TaskSnapshot(
        handle="task_restart_unknown_external",
        kind="MCP",
        tool="external",
        owner=root.owner.model_copy(
            update={"assignment_id": "task_restart_unknown_external", "parent_handle": root.handle}
        ),
        label="Uncertain external operation",
        invocation_id="restart-unknown-external",
        created_at=stamp,
        updated_at=stamp,
        status="running",
        raw_status="running",
    )
    failed = root.model_copy(
        update={
            "status": "failed",
            "raw_status": "failed",
            "activity": "",
            "settlement": "confirmed",
            "terminal_at": stamp,
            "updated_at": stamp,
            "supported_actions": ("get_result", "message", "restart"),
        }
    )
    await asyncio.to_thread(history.record_snapshot, external, observation_id="unknown-child")
    await asyncio.to_thread(history.record_snapshot, failed, observation_id="failed-parent")

    async def refuse_cancellation(task: TaskSnapshot) -> dict[str, str]:
        assert task.handle == external.handle

        def record_uncertainty() -> None:
            current = history.task_snapshot(task.handle)
            assert current is not None
            history.record_snapshot(
                current.model_copy(
                    update={
                        "status": "task_state_uncertain",
                        "raw_status": "failed",
                        "settlement": "unknown",
                        "terminal_at": now(),
                    }
                ),
                observation_id="external-settlement-unknown",
            )

        await history.io.run(record_uncertainty)
        return {"error": "external cleanup was not confirmed"}

    owner.cancel_work = refuse_cancellation
    try:
        await owner.restart(
            TaskCaller(root.owner.session_id, "human", human_action=True), root.handle
        )
        restart_job = owner._controls["restart:" + root.handle]
        with pytest.raises(ValueError, match="descendant settlement remains unconfirmed"):
            await asyncio.wait_for(asyncio.shield(restart_job), 5)
        latest = await history.io.run(history.latest_assignment, root.owner.session_id)
        assert latest is not None and latest.handle == root.handle
        assert owner._control_holds >= {root.handle}
        unknown = await history.io.run(history.task_snapshot, external.handle)
        assert unknown is not None and unknown.settlement == "unknown"
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_consumed_plain_question_does_not_requeue_healthy_recovery(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """The live shutdown failure was a readiness cycle over an ordinary answered Question."""
    from dspy.lm15 import Message as ModelMessage

    from clio_agent.gact.task_publication import publish_task_notice

    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    questions = QuestionCommands(owner.history)
    app.state.questions = questions
    with owner.runner.session_change(sid):
        owner.start()

        def commit_answer() -> None:
            question = questions.create(
                root.owner.model_copy(update={"parent_handle": root.handle}),
                QuestionDefinition(prompt="Which owned dataset?", source="native"),
                invocation_id="ordinary-question",
            )
            answered = questions.answer(question.handle, QuestionReply(answer="Owned dataset"))
            publish_task_notice(owner.history, answered)
            notices = owner.history.pending_mail(sid)
            assert len(notices) == 1 and notices[0].kind == "question_answer"
            token = context.set_app(app)
            try:
                context.set_turn_identity(
                    app=app,
                    session_id=sid,
                    turn_id="question-input",
                    trace_id="question-input",
                    assignment_handle=root.handle,
                    input_notification_id=notices[0].id,
                )
                recorder = StepRecorder(arc, sid, "main", expert_id="main")
                recorder.user_message(ModelMessage.user("Continue with the answer"))
                recorder.commit_input(0)
                assert owner.history.acknowledged_notifications(sid) == {notices[0].id}
            finally:
                context.reset(token)

        try:
            await asyncio.to_thread(commit_answer)
            await asyncio.sleep(0)
            await owner.recovery.drain()
            assert owner.recovery.pending == ()
            await asyncio.to_thread(owner._restore_input_projections)
            await asyncio.sleep(0)
            await owner.recovery.drain()
            assert owner.recovery.pending == ()
        finally:
            owner.close_admission()
    await owner.shutdown()
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_retained_question_alias_answers_the_authorized_canonical_task(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    """An authorized legacy address must not fail after canonical lookup succeeds."""
    app, owner, root = await create_environment(arc, tmp_path)
    history = app.state.task_history
    app.state.questions = QuestionCommands(history)
    question = await asyncio.to_thread(
        app.state.questions.create,
        root.owner.model_copy(update={"parent_handle": root.handle}),
        QuestionDefinition(prompt="Choose a dataset", source="native"),
        invocation_id="legacy-question-call",
    )
    await asyncio.to_thread(
        history.record_alias,
        TaskAlias(
            alias="legacy-question-id", handle=question.handle, source_identity="legacy-question"
        ),
    )
    try:
        result = await asyncio.to_thread(
            answer_question,
            app,
            TaskCaller(root.owner.session_id, "human", human_action=True),
            "legacy-question-id",
            QuestionReply(answer="Dataset B"),
        )
        assert result.handle == question.handle and result.raw_status == "answered"
        saved = await asyncio.to_thread(history.task_snapshot, question.handle)
        assert saved is not None and saved.result is not None
        reply = QuestionReply.model_validate(
            await asyncio.to_thread(history.read_content, saved.result)
        )
        assert reply.answer == "Dataset B"
        assert await asyncio.to_thread(history.task_snapshot, "legacy-question-id") is None
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_busy_typed_inputs_preserve_media_and_order_through_core_recovery(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    """Commit actual model-input parts, not a JSON summary of accepted human input."""
    from dspy.lm15 import ImagePart, TextPart
    from dspy.lm15 import Message as ModelMessage

    from clio_agent.gact.types import Message, Part

    app, owner, root = await create_environment(arc, tmp_path)
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="

    def exercise() -> None:
        history, sid, stamp = owner.history, root.owner.session_id, now()
        accepted = []
        for identity, text, media in [
            ("input-head", "Initial assignment", False),
            ("input-media", "Inspect this image", True),
            ("input-later", "Then compare the result", False),
        ]:
            message = Message(
                id=identity,
                turn_id=identity,
                session_id=sid,
                role="user",
                created_at=stamp,
                updated_at=stamp,
                parts=[
                    Part(type="text", text=text),
                    *([Part(type="image", data=png, media_type="image/png")] if media else []),
                ],
            )
            content = history.write_content(
                sid,
                {
                    "message_id": message.id,
                    "text": text,
                    "model_text": text,
                    "message": message.model_dump(mode="json"),
                    "metadata": {"internal_probe": "PRIVATE_MARKER"},
                },
            )
            history.record_mail(
                MailItem(
                    id=identity,
                    recipient_session=sid,
                    sender_human="human",
                    kind="human",
                    content=content,
                    created_at=stamp,
                )
            )
            accepted.append(identity)
        token = context.set_app(app)
        try:
            context.set_turn_identity(
                app=app,
                session_id=sid,
                turn_id="input-test",
                trace_id="trace",
                assignment_handle=root.handle,
                input_notification_id=accepted[0],
            )
            context.set_react_run(-1)
            recorder = StepRecorder(arc, sid, "main", expert_id="main")
            recorder.user_message(ModelMessage.user("Initial assignment"))
            recorder.commit_input(0)
            messages = recorder.read_steps()
            texts = ["".join(p.text for p in m.parts if isinstance(p, TextPart)) for m in messages]
            assert len(messages) == 3 and all(m.role == "user" for m in messages)
            assert "Initial assignment" in texts[0] and "Inspect this image" in texts[1]
            assert "Then compare the result" in texts[2] and all(
                "PRIVATE_MARKER" not in t for t in texts
            )
            images = [p for m in messages for p in m.parts if isinstance(p, ImagePart)]
            assert len(images) == 1 and images[0].data == png
            assert set(accepted) <= history.acknowledged_notifications(sid)
            restored = TaskHistory(arc)
            restored.restore()
            app.state.task_history = restored
            recovered = StepRecorder(arc, sid, "main", expert_id="main").read_steps()
            assert recovered == messages and restored.pending_mail(sid) == ()
        finally:
            context.reset(token)

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_rendered_mail_batches_count_the_initial_head_and_retain_overflow(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An indivisible head counts against batching and does not acknowledge its followers."""
    from dspy.lm15 import ImagePart, TextPart
    from dspy.lm15 import Message as ModelMessage

    monkeypatch.setenv("CLIO_MODEL_TOOL_RESULT_CHARS", "120")
    app, owner, root = await create_environment(arc, tmp_path)
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="

    def exercise() -> None:
        sid, history = root.owner.session_id, owner.history
        for identity in ("large-head", "later-human", "final-notice"):
            history.record_mail(
                MailItem(
                    id=identity,
                    recipient_session=sid,
                    sender_human="human",
                    kind="human" if identity != "final-notice" else "control",
                    content=history.write_content(sid, identity),
                    created_at=now(),
                )
            )
        token = context.set_app(app)
        try:
            context.set_turn_identity(
                app=app,
                session_id=sid,
                turn_id="batch-test",
                trace_id="batch-trace",
                assignment_handle=root.handle,
                input_notification_id="large-head",
            )
            recorder = StepRecorder(arc, sid, "main", expert_id="main")
            recorder.user_message(
                ModelMessage(
                    role="user",
                    parts=(TextPart(text="h" * 100), ImagePart(data=png, media_type="image/png")),
                )
            )
            recorder.commit_input(0)
            assert history.acknowledged_notifications(sid) == {"large-head"}
            assert [item.id for item in history.pending_mail(sid)] == [
                "later-human",
                "final-notice",
            ]
            (message,) = recorder.read_steps()
            assert any(isinstance(part, ImagePart) and part.data == png for part in message.parts)
            assert any(
                isinstance(part, TextPart) and part.text == "h" * 100 for part in message.parts
            )
            restored = TaskHistory(arc)
            restored.restore()
            assert [item.id for item in restored.pending_mail(sid)] == [
                "later-human",
                "final-notice",
            ]
            assert restored.acknowledged_notifications(sid) == {"large-head"}
        finally:
            context.reset(token)

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_busy_evidence_uses_accepted_snapshot_and_rechecks_current_access(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Changed evidence is delivered as-of acceptance; loss of workspace access is refused."""
    from clio_agent.gact.context_reference_evidence import find_evidence_reference_snapshot
    from clio_agent.gact.context_references import authorize_context_reference_parts_sync
    from clio_agent.gact.messaging import queued_user_input
    from clio_agent.gact.runtime.globals import _ContextFileAccessError
    from clio_agent.gact.types import Message, Part

    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    app.state.context_frames = {
        sid: [
            {
                "id": "accepted-frame",
                "session_id": sid,
                "status": "completed",
                "items": [{"kind": "context_ref", "source_id": "original-source"}],
            }
        ]
    }

    def exercise() -> None:
        session = app.state.sessions.get(sid)
        selected = find_evidence_reference_snapshot(
            app, session.workspace_id, "context_frame", "accepted-frame"
        )
        assert selected is not None
        parts = authorize_context_reference_parts_sync(
            app,
            session,
            [
                Part(
                    type="context_ref",
                    ref_kind="context_frame",
                    ref_id="accepted-frame",
                    revision=selected[0]["revision"],
                ),
            ],
        )
        message = Message(
            id="evidence-input",
            session_id=sid,
            role="user",
            created_at=now(),
            updated_at=now(),
            parts=parts,
        )
        payload = {
            "message_id": message.id,
            "text": "Review the evidence",
            "message": message.model_dump(mode="json"),
        }
        app.state.context_frames[sid][0]["items"] = [{"source_id": "changed-source"}]
        rendered = queued_user_input(app, sid, payload)
        assert "as-of snapshot; current state has moved" in rendered["text"]
        assert "original-source" in rendered["text"] and "changed-source" not in rendered["text"]
        private = app.state.sessions.create(workspace_id="different-workspace")
        app.state.context_frames[private.id] = app.state.context_frames.pop(sid)
        with pytest.raises(_ContextFileAccessError):
            queued_user_input(app, sid, payload)

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_automatic_queue_preflight_stop_retains_unassigned_work(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh session cannot acquire an assignment through a preflight that Stop suspended."""
    import threading

    from clio_agent.gact import message_submission
    from clio_agent.gact.events import EventBus
    from clio_agent.gact.routes.mailbox import promote, queued
    from clio_agent.gact.routes.session_cancellation import cancel_session_state_async
    from clio_agent.gact.types import PostMessageRequest

    app, owner, _root = await create_environment(arc, tmp_path)
    session = await asyncio.to_thread(app.state.sessions.create, workspace_id="workspace")
    sid, history = session.id, owner.history
    app.state.bus = EventBus()
    app.state.gact_deps = SimpleNamespace()
    request = PostMessageRequest(text="Future work")
    item = await asyncio.to_thread(
        history.record_mail,
        MailItem(
            id="future-input",
            recipient_session=sid,
            kind="composer",
            sender_human="human",
            content=await asyncio.to_thread(
                history.write_content, sid, {"request": request.model_dump(mode="json")}
            ),
            created_at=now(),
            delivery="queued",
            queued_order=1,
        ),
    )
    entered, released = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = message_submission.prepare_references_sync

    def prepare(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(message_submission, "prepare_references_sync", prepare)
    monkeypatch.setattr(
        message_submission,
        "_validate_provider_and_payload",
        lambda *_a, **_k: (
            session,
            None,
            "Future work",
            [],
            [],
        ),
    )
    monkeypatch.setattr(message_submission, "_plan_resource_parts", lambda *_a, **_k: ([], []))
    outcomes = []

    async def admit(_sid: str) -> None:
        outcomes.append(
            await asyncio.to_thread(promote, app, sid, item.id, item.revision, automatic=True)
        )

    runner = TurnRunner(app, admit)
    runner.bind_loop(loop)
    app.state.turn_runner = owner.runner = runner
    try:
        runner.signal(sid)
        await entered.wait()
        assert history.latest_assignment(sid) is None
        effect = await cancel_session_state_async(app, None, sid)
        assert effect["status"] == "stopping" and runner.cancellation(sid).is_set()
        assert app.state.sessions.get(sid).metadata["composer_queue_paused"] is True
        runner.run_when_released(sid, released.set)
        release.set()
        await released.wait()
        await asyncio.gather(*tuple(owner._controls.values()))
        assert outcomes == [{"queued_message_id": item.id, "status": "pending", "suspended": True}]
        assert history.latest_assignment(sid) is None
        assert await asyncio.to_thread(queued, app, sid) == [item]
        assert app.state.sessions.get(sid).status == "idle"
        automatic = item.model_copy(update={"delivery": "eligible", "automatic_promotion": True})
        assert not owner.eligible(sid, automatic)
        assert owner.eligible(sid, automatic.model_copy(update={"automatic_promotion": False}))
    finally:
        release.set()
        await runner.drain()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_dependency_aliases_are_canonical_and_cannot_name_the_active_assignment(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    """Legacy aliases must neither strand a dependency nor bypass the self-wait check."""
    app, owner, root = await create_environment(arc, tmp_path)

    def exercise() -> None:
        history = owner.history
        child = root.model_copy(
            update={
                "handle": "task_dependency",
                "kind": "Shell",
                "owner": root.owner.model_copy(update={"parent_handle": root.handle}),
                "invocation_id": "dependency",
                "status": "running",
            }
        )
        history.record_snapshot(child, observation_id="dependency")
        history.record_alias(
            TaskAlias(alias="old-dependency", handle=child.handle, source_identity="legacy-child")
        )
        history.record_alias(
            TaskAlias(alias="old-root", handle=root.handle, source_identity="legacy-root")
        )
        result = owner.set_dependencies(
            root.owner.session_id, ["old-dependency", child.handle], "all"
        )
        assert result["dependencies"] == [child.handle]
        saved = history.task_snapshot(root.handle)
        assert saved is not None and saved.details is not None
        details = AssignmentDetails.model_validate(history.read_content(saved.details))
        assert details.dependencies == (child.handle,)
        with pytest.raises(ValueError, match="cannot wait for itself"):
            owner.set_dependencies(root.owner.session_id, "old-root", "all")
        assert history.task_snapshot(root.handle) == saved

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_explicit_message_deletion_withdraws_only_uncommitted_human_inputs(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    """Projection deletion cannot leave accepted instructions pending in Core."""
    app, owner, root = await create_environment(arc, tmp_path)

    def exercise() -> None:
        history, sid = owner.history, root.owner.session_id
        inputs: list[tuple[str, MailKind]] = [
            ("deleted-human", "human"),
            ("kept-task", "task_result"),
        ]
        for identity, kind in inputs:
            content = history.write_content(sid, identity)
            history.record_mail(
                MailItem(
                    id=identity, recipient_session=sid, kind=kind, content=content, created_at=now()
                )
            )
        withdraw_message_inputs(app, sid, ["deleted-human", "kept-task"])
        assert [item.id for item in history.pending_mail(sid)] == ["kept-task"]
        restored = TaskHistory(arc)
        restored.restore()
        assert [item.id for item in restored.pending_mail(sid)] == ["kept-task"]
        assert "deleted-human" not in restored.acknowledged_notifications(sid)

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("server_first", [False, True])
async def test_cancellation_keeps_work_settling_during_worker_retirement(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_status: str,
    server_first: bool,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    history = app.state.task_history
    stamp = now()
    backend = BackendReference(
        owner="external_mcp", server="external", session="original", task_id="external-task"
    )
    submission = TaskSubmission(
        "task_external",
        "external",
        await history.io.run(history.write_content, root.owner.session_id, {"arguments": {}}),
        None,
        None,
        argument_digest({}),
    )
    app.state.task_recovery = owner.recovery
    external = ExternalTasks(app)
    accepted = restore_native_call(
        Client(FastMCP("unused-result-transport")),
        submission,
        task_id=backend.task_id,
        status="working",
        created_at=stamp,
        updated_at=stamp,
    )
    connection = ExternalConnection(backend.model_copy(update={"task_id": ""}))
    active = TaskSnapshot(
        handle="task_external",
        kind="MCP",
        tool="external",
        owner=root.owner.model_copy(update={"parent_handle": root.handle}),
        label="External work",
        invocation_id="external",
        created_at=stamp,
        updated_at=stamp,
        status="running",
        backend=backend,
        submission=submission.context,
    )
    old = active.model_copy(
        update={
            "handle": "task_historical",
            "invocation_id": "old",
            "status": "interrupted",
            "terminal_at": stamp,
            "settlement": "unknown",
            "backend": None,
        }
    )
    await asyncio.to_thread(history.record_snapshot, active, observation_id="active")
    await asyncio.to_thread(history.record_snapshot, old, observation_id="old")
    retiring, release, changed, retired = (asyncio.Event() for _ in range(4))
    cancellation_dispatched = asyncio.Event()
    dispatched: list[str] = []

    async def request_cancellation(task: TaskSnapshot) -> dict[str, Any]:
        assert task.handle == active.handle
        dispatched.append(task.handle)
        cancellation_dispatched.set()
        return {"handle": task.handle, "cancellation_requested": True}

    async def stop(_sid: str) -> None:
        retiring.set()
        await release.wait()
        retired.set()

    monkeypatch.setattr(owner.runner, "stop", stop)
    monkeypatch.setattr(owner, "cancel_work", request_cancellation)
    loop = asyncio.get_running_loop()
    remove = history.listen(
        lambda _revision: loop.call_soon_threadsafe(changed.set), handles=(root.handle,)
    )
    try:
        acknowledgement = await owner.cancel(
            TaskCaller(root.owner.session_id, "human", human_action=True), root.handle
        )
        await asyncio.gather(retiring.wait(), cancellation_dispatched.wait())
        assert dispatched == [active.handle]
        # The owner effect does not await Core; confirm its deferred recording
        # while physical retirement remains held before inspecting the stamp.
        if acknowledgement.get("recording") == "unconfirmed":
            await asyncio.wait_for(owner.recovery.drain(), 5)
        stopping = await asyncio.to_thread(history.task_snapshot, root.handle)
        assert stopping is not None and stopping.terminal_at is None
        assert stopping.cancellation_requested_at is not None
        details = AssignmentDetails.model_validate(
            await asyncio.to_thread(history.read_content, stopping.details)
        )
        assert details.cancellation_members == (active.handle,)
        final = ClientGetTaskResult.model_validate(
            {
                "resultType": "complete",
                "taskId": backend.task_id,
                "status": final_status,
                "createdAt": stamp,
                "lastUpdatedAt": now(),
                "result": None
                if final_status == "cancelled"
                else {
                    "content": [],
                    "structuredContent": {"value": "actual result"},
                    "isError": final_status == "failed",
                },
            }
        )
        if not server_first:
            release.set()
            await retired.wait()
            pending = await history.io.run(history.task_snapshot, root.handle)
            assert pending is not None and pending.terminal_at is None
        await history.io.run(external._record_reply, connection, accepted, final)
        if server_first:
            pending = await history.io.run(history.task_snapshot, root.handle)
            assert pending is not None and pending.terminal_at is None and not retired.is_set()
            release.set()
        while True:
            settled = await history.io.run(history.task_snapshot, root.handle)
            if settled is not None and settled.terminal_at is not None:
                break
            await changed.wait()
            changed.clear()
        assert (
            settled is not None
            and settled.status == "cancelled"
            and settled.settlement == "confirmed"
        )
        member = await history.io.run(history.task_snapshot, active.handle)
        assert member is not None and member.status == final_status
        assert member.settlement == "external_acknowledged" and member.result is not None
        assert (await history.io.run(history.read_content, member.result))["result"] == final.result
        outcome = await history.io.run(history.read_content, settled.result)
        assert outcome["settlement_scope"] == "clio_owned_execution"
        assert outcome["members"] == [
            {
                "handle": active.handle,
                "status": final_status,
                "raw_status": final_status,
                "settlement": "external_acknowledged",
            }
        ]
        assert await asyncio.to_thread(history.task_snapshot, old.handle) == old
        restored = TaskHistory(arc)
        await history.io.run(restored.restore)
        assert restored.task_snapshot(root.handle) == settled
        assert restored.task_snapshot(active.handle) == member
    finally:
        release.set()
        remove()
        for control in tuple(owner._controls.values()):
            control.cancel()
        await asyncio.gather(*tuple(owner._controls.values()), return_exceptions=True)
        await owner.recovery.drain()
