"""Recording custody and retry boundaries over the production Core/session owners."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import plan_mode, turn, turn_finalize
from clio_agent.gact.native_work import now
from clio_agent.gact.transcript_recording import TranscriptRecordingPending
from tests.test_arc.test_accepted_transcript_repair import retained
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_core_first_transcript import install


@pytest.mark.asyncio
async def test_withdrawal_keeps_a_surfaced_approval_waiting_for_its_human(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Partial approval publication remains an input gate when its output is cleared."""
    from clio_agent.arc.task_types import QuestionDefinition
    from clio_agent.gact.question_surface import prepare_question_gate
    from clio_agent.gact.questions import QuestionCommands

    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    app.state.questions = QuestionCommands(owner.history)
    app.state.questions.before_post = lambda task, definition: prepare_question_gate(
        app, task, definition
    )
    stop = threading.Event()
    app.state.cancel_events = {sid: stop}
    retries: list[str] = []

    def exercise() -> None:
        question = app.state.questions.create(
            root.owner,
            QuestionDefinition(
                prompt="Approve this retained plan?",
                source="plan_exit",
                metadata={
                    "plan_approval": {
                        "assignment": root.handle,
                        "context_epoch": "",
                        "request": {"summary": "Original plan"},
                    }
                },
            ),
            invocation_id="surfaced-plan",
            handle="surfaced-plan",
        )

        def update_retry(status: str, **_kwargs: Any) -> None:
            retries.append(status)

        turn_finalize.settle_withdrawn_turn(app, sid, stop, update_retry_attempt=update_retry)
        session = app.state.sessions.get(sid)
        assert (
            session.status == "waiting_user"
            and session.metadata["execution_gate"] == question.handle
        )
        assert owner.history.task_snapshot(question.handle) == question
        assert question.raw_status == "pending" and question.terminal_at is None
        assert retries == ["cancelled"] and sid not in app.state.cancel_events

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["goal", "plan"])
async def test_withdrawn_output_retires_without_goal_execution_or_plan_approval(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The callers must distinguish withdrawal from successful publication."""
    from clio_agent.gact import turn_finalize_goal
    from clio_agent.gact.questions import QuestionCommands

    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    app.state.questions = QuestionCommands(owner.history)
    app.state.cancel_events = {}
    retired: list[str] = []
    state: Any = SimpleNamespace(
        app=app,
        sid=sid,
        user_msg=retained(sid, "request", 10),
        error_info=None,
        turn_id="withdrawn-turn",
        trace_id="withdrawn-trace",
        turn_cancel_event=threading.Event(),
    )
    monkeypatch.setattr(
        "clio_agent.gact.turn_stream.settle_turn_transcript", lambda item: retired.append(item.sid)
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Withdrawn output cannot drive further execution")

    try:
        if path == "goal":
            monkeypatch.setattr(
                turn_finalize_goal, "finalize_turn", lambda *_args, **_kwargs: False
            )
            monkeypatch.setattr(turn_finalize_goal, "dispatch_goal_at_finalize", forbidden)
            monkeypatch.setattr("clio_agent.gact.autonomy_output.observe_control_output", forbidden)
            await turn_finalize_goal.finalize_turn_async(
                state,
                None,
                drain_observed_tool_calls=lambda rows: rows,
                update_retry_attempt=lambda *_args, **_kwargs: None,
            )
        else:
            plan = tmp_path / "review.md"
            plan.write_text("# Plan\nInspect the owned inputs.\n", encoding="utf-8")
            await asyncio.to_thread(
                app.state.sessions.update,
                sid,
                mode="plan",
                status="running",
                metadata_patch={
                    "pending_plan_exit": {"summary": "Inspect inputs", "plan_file": str(plan)}
                },
            )
            monkeypatch.setattr(
                "clio_agent.gact.plan_reuse.save_approved_plan", lambda *_args, **_kwargs: ""
            )

            def withdraw_output(_state: Any, *, after_withdrawn: Any) -> None:
                after_withdrawn()

            monkeypatch.setattr(
                "clio_agent.gact.paused_transcript.persist_paused_transcript", withdraw_output
            )
            monkeypatch.setattr(app.state.questions, "create", forbidden)
            assert await asyncio.to_thread(plan_mode.maybe_pause_for_plan_exit, state)
            assert app.state.sessions.get(sid).metadata["pending_plan_exit"] == {}
            assert app.state.sessions.get(sid).status == "idle"
        assert retired == [sid]
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_stop_of_plan_recording_wait_does_not_enter_normal_finalization(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop retires the waiter while the original immutable output remains owned."""
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    message = retained(sid, "original-paused-output", 10, role="assistant")
    release = threading.Event()
    publications: list[str] = []

    def confirm() -> bool:
        if not release.is_set():
            raise OSError("Recording remains unavailable")
        return True

    pending = TranscriptRecordingPending(message, confirm, OSError("Core unavailable"))
    pending.after_confirmed = lambda: publications.append(message.id)

    def pause(_state: Any, **_kwargs: Any) -> bool:
        raise pending

    async def off_loop(_state: Any, action: Any) -> Any:
        return await asyncio.to_thread(action)

    monkeypatch.setattr(plan_mode, "maybe_pause_for_plan_exit", pause)
    monkeypatch.setattr(turn, "_run_turn_setup_off_loop", off_loop)
    state: Any = SimpleNamespace(app=app, sid=sid, pred=SimpleNamespace(answer="Original output"))
    waiter = asyncio.create_task(
        turn._apply_turn_prediction(state, lambda rows: rows, lambda: None)
    )
    try:
        while not owner.recovery.pending:
            await asyncio.sleep(0)
        waiter.cancel()
        assert await waiter is False
        assert publications == [] and owner._model_recording
        release.set()
        await owner.recovery.drain()
        assert publications == [message.id] and not owner._model_recording
    finally:
        release.set()
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
async def test_deferred_plan_approval_cannot_attach_to_cancelled_or_successor_assignment(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, superseded: bool
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    request = {"summary": "Original plan", "invocation_id": "original-invocation"}
    monkeypatch.setattr("clio_agent.gact.turn_stream.settle_turn_transcript", lambda _state: None)

    def exercise() -> None:
        app.state.sessions.update(sid, metadata_patch={"pending_plan_exit": request})
        changed = root.model_copy(
            update={"status": "cancelled", "terminal_at": now(), "cancellation_requested_at": now()}
        )
        owner.history.record_snapshot(changed, observation_id="human-cancel")
        if superseded:
            owner.history.record_snapshot(
                root.model_copy(
                    update={"handle": "successor", "created_at": now(), "updated_at": now()}
                ),
                observation_id="successor",
            )
        state: Any = SimpleNamespace(app=app, sid=sid)
        for _ in range(2):
            plan_mode.complete_plan_exit_pause(
                state,
                request,
                None,
                "original-paused-output",
                assignment_handle=root.handle,
                context_epoch="original-context",
            )
        assert app.state.sessions.get(sid).metadata["pending_plan_exit"] == {}
        for handle in owner.history.task_handles():
            task = owner.history.task_snapshot(handle)
            assert task is not None and task.kind != "Question"

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_publication_retry_preserves_usage_after_lost_flush_and_never_replays_hooks(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A projection write may land before its error; hook failures may have effects."""
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    install(app, tmp_path)
    message = retained(sid, "recorded-final-output", 10, role="assistant")
    hooks: list[str] = []
    retries: list[str] = []
    app.state.cancel_events = {}
    monkeypatch.setattr(turn_finalize, "settle_turn_transcript", lambda _state: None)
    monkeypatch.setattr(turn_finalize, "context_usage_metadata_patch", lambda *_args: {})
    monkeypatch.setattr(
        turn_finalize, "publish_turn_settled_status", lambda *_args, **_kwargs: None
    )
    for target in (
        "clio_agent.gact.spotter_watcher.on_turn_finalized",
        "clio_agent.gact.hooks.stop_loop.dispatch_stop_at_finalize",
        "clio_agent.gact.autonomous_loop.dispatch_loop_at_finalize",
        "clio_agent.gact.replanning.dispatch_stall_monitor_at_finalize",
    ):
        monkeypatch.setattr(target, lambda *_args, **_kwargs: None)

    def effectful_hook(*_args: Any, **_kwargs: Any) -> None:
        hooks.append("visible effect")
        raise OSError("Hook failed after its side effect")

    monkeypatch.setattr("clio_agent.gact.hooks.fire_post_tool_batch", effectful_hook)

    def exercise() -> None:
        from clio_agent.gact.part_atom_minter import persist_finalized_message

        persist_finalized_message(app, sid, message)
        app.state.sessions.update(sid, add_tokens_input=3, add_tokens_output=5, add_cost_usd=0.1)
        session = app.state.sessions.get(sid)
        state: Any = SimpleNamespace(
            app=app,
            sid=sid,
            bus=app.state.bus,
            sess=session,
            user_msg=message,
            turn_id="original-turn",
            trace_id="original-trace",
            error_info=None,
            selected_agent="main",
            invocation_agent_id="",
            turn_tokens={"input": 11, "output": 7},
            turn_cost=0.2,
            turn_cost_known=True,
            cancelled_turn=False,
            publication_steps=set(),
            publication_usage=None,
            publication_model_usage=None,
            agent_runtime={},
            tools_called=[{"name": "shell.bash"}],
            pred=None,
            answer_text="Done",
            turn_cancel_event=threading.Event(),
        )
        persist = app.state.sessions._persist
        failed = False

        def lost_flush_reply(session: Any) -> None:
            nonlocal failed
            persist(session)
            if not failed:
                failed = True
                raise OSError("Projection was saved; its reply was lost")

        monkeypatch.setattr(app.state.sessions, "_persist", lost_flush_reply)
        payload = {"message_id": message.id, "stop_reason": "end_turn"}

        def update(status: str, **_kwargs: Any) -> None:
            retries.append(status)

        with pytest.raises(OSError, match="reply was lost"):
            turn_finalize.publish_finalized_turn(
                state, message, payload, update_retry_attempt=update
            )
        assert not hooks
        turn_finalize.publish_finalized_turn(state, message, payload, update_retry_attempt=update)
        turn_finalize.publish_finalized_turn(state, message, payload, update_retry_attempt=update)
        session = app.state.sessions.get(sid)
        assert (session.tokens_input, session.tokens_output) == (14, 12)
        assert session.cost_usd == pytest.approx(0.3)
        assert hooks == ["visible effect"] and retries == ["completed"]
        completions = [
            event
            for event in app.state.bus.session_events_since(sid)
            if event.type == "message.completed"
        ]
        assert len(completions) == 1 and completions[0].payload["message_id"] == message.id

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
