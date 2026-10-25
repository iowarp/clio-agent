"""Cancelled MCP work cannot strand late or already-visible input Questions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import CoordinationGrant, QuestionReply
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskAccessDenied, TaskCaller
from clio_agent.tools.task_client import AcceptedNativeCall
from tests.test_arc.test_external_input_identity_contracts import (
    accepted,
    environment,
    question_ready,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["task", "ancestor"])
async def test_late_input_after_cancellation_returns_confirmed_cancel_without_question(
    arc: ARCMemory, tmp_path: Path, scope: Literal["task", "ancestor"]
) -> None:
    """A stale input-required reply cannot escape the cancellation member snapshot."""
    app, conversations, external, host, params = await environment(arc, tmp_path)
    try:
        handle = await accepted(app, external, host, params)

        def cancel_before_input() -> None:
            history = app.state.task_history
            with history.serialized():
                current = history.task_snapshot(handle)
                if scope == "ancestor":
                    # This is the real owner's pre-persistence admission barrier.
                    conversations._control_holds.add(current.owner.parent_handle)
                else:
                    history.record_snapshot(
                        current.model_copy(update={"cancellation_requested_at": now()}),
                        observation_id="cancel-before-input",
                    )

        await app.state.task_history.io.run(cancel_before_input)
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        answer = await asyncio.wait_for(external._input(host, params, request), 5)
        assert answer.action == "cancel" and answer.content is None
        history = app.state.task_history
        current = await history.io.run(history.task_snapshot, handle)
        details = await history.io.run(external._details, current)
        assert details["answers"]["c-d"] == {"action": "cancel"}
        assert not history.task_handles(kind="Question")
        assert current.terminal_at is None
        assert current.backend.task_id == "job-a-b"
    finally:
        conversations._control_holds.clear()
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_revocation_after_owner_acceptance_does_not_strand_input_recording(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finish accepted owner cleanup without granting another control to a revoked peer."""
    app, conversations, external, host, params = await environment(arc, tmp_path)
    release = asyncio.Event()
    worker: asyncio.Task[Any] | None = None
    grant: CoordinationGrant | None = None
    key = cast(Any, "accepted-input-control")

    def hold_recording(identity: str, operation: Callable[[], object]) -> None:
        async def record() -> None:
            await release.wait()
            await app.state.task_history.io.run(operation)

        conversations.recovery.defer(identity, record)

    async def acknowledge(_call: AcceptedNativeCall) -> None:
        return None

    monkeypatch.setattr(conversations.recovery, "defer_recording", hold_recording)
    monkeypatch.setattr(AcceptedNativeCall, "request_cancel", acknowledge)
    try:
        handle = await accepted(app, external, host, params)
        current = await app.state.task_history.io.run(app.state.task_history.task_snapshot, handle)
        caller = TaskCaller("peer", current.owner.human_id)
        grant = CoordinationGrant(
            id="input-control",
            granting_human=current.owner.human_id,
            recipient_session=caller.session_id,
            target_session=current.owner.session_id,
            target_handle=handle,
            rights=("control",),
            created_at=now(),
        )
        await app.state.task_history.io.run(
            app.state.task_history.record_grant, grant, observation_id="input-control-grant"
        )
        external.connections[key] = host
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        worker = asyncio.create_task(external._input(host, params, request))
        qid = await question_ready(app, handle, "c-d")
        effect = await external.cancel(handle, caller=caller)
        assert effect["cancellation_requested"] is True
        await app.state.task_history.io.run(
            app.state.task_history.record_grant,
            grant.model_copy(update={"revoked_at": now()}),
            observation_id="input-control-revoke",
        )
        with pytest.raises(TaskAccessDenied):
            TaskAccess(app.state.task_history, caller).require(handle, "control")
        release.set()
        answer = await asyncio.wait_for(worker, 5)
        await asyncio.wait_for(conversations.recovery.drain(), 5)
        assert answer.action == "cancel"
        assert app.state.task_history.task_snapshot(qid).status == "cancelled"
        assert not conversations.recovery.is_pending("external-cancel:" + handle)
        assert not conversations.recovery.is_pending("external-cancel-inputs:" + handle)
    finally:
        release.set()
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        external.connections.pop(key, None)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_pending_cancel_recording_closes_late_input_before_intent_checkpoint(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """A pending verified cancel blocks input admission before its timestamp is saved."""
    app, conversations, external, host, params = await environment(arc, tmp_path)
    release = asyncio.Event()
    try:
        handle = await accepted(app, external, host, params)

        async def held_recording() -> None:
            await release.wait()

        external.recovery.defer("external-cancel:" + handle, held_recording)
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        answer = await asyncio.wait_for(external._input(host, params, request), 5)
        current = await app.state.task_history.io.run(app.state.task_history.task_snapshot, handle)
        assert external.recovery.is_pending("external-cancel:" + handle)
        assert current.cancellation_requested_at is None
        assert answer.action == "cancel"
        assert not app.state.task_history.task_handles(kind="Question")
        details = await app.state.task_history.io.run(external._details, current)
        assert details["answers"]["c-d"] == {"action": "cancel"}
    finally:
        release.set()
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_confirmed_question_answer_survives_cancel_before_callback_checkpoint(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve the authoritative answer if callback delivery failed before its copy."""
    from clio_agent.gact import agent_elicitation

    app, conversations, external, host, params = await environment(arc, tmp_path)
    original = agent_elicitation.await_question_input
    confirmed, release = asyncio.Event(), asyncio.Event()
    worker: asyncio.Task[Any] | None = None

    async def hold_checkpoint(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        confirmed.set()
        await release.wait()

    monkeypatch.setattr(agent_elicitation, "await_question_input", hold_checkpoint)
    try:
        handle = await accepted(app, external, host, params)
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        worker = asyncio.create_task(external._input(host, params, request))
        qid = await question_ready(app, handle, "c-d")
        await app.state.task_history.io.run(
            app.state.questions.answer, qid, QuestionReply(metadata={"format": "png"})
        )
        await asyncio.wait_for(confirmed.wait(), 5)
        current = await app.state.task_history.io.run(app.state.task_history.task_snapshot, handle)
        details = await app.state.task_history.io.run(external._details, current)
        assert "c-d" not in details.get("answers", {})
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await app.state.task_history.io.run(
            app.state.task_history.record_snapshot,
            current.model_copy(update={"cancellation_requested_at": now()}),
            observation_id="cancel-between-answer-and-callback",
        )
        release.set()
        replay = await asyncio.wait_for(external._input(host, params, request), 5)
        assert replay.action == "accept" and replay.content == {"format": "png"}
        current = await app.state.task_history.io.run(app.state.task_history.task_snapshot, handle)
        details = await app.state.task_history.io.run(external._details, current)
        assert details["answers"]["c-d"]["content"] == {"format": "png"}
        human = current.owner.human_id
        assert app.state.task_history.task_handles(kind="Question", human_id=human) == {qid}
    finally:
        release.set()
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_external_cancel_closes_existing_input_and_unblocks_sdk_callback(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner dismisses its input without inventing terminal or cleanup evidence."""
    app, conversations, external, host, params = await environment(arc, tmp_path)
    worker: asyncio.Task[Any] | None = None
    calls: list[str] = []

    async def acknowledge(call: AcceptedNativeCall) -> None:
        calls.append(call.task.task_id)

    monkeypatch.setattr(AcceptedNativeCall, "request_cancel", acknowledge)
    key = cast(Any, "input-cancellation-test")
    try:
        handle = await accepted(app, external, host, params)
        external.connections[key] = host
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        worker = asyncio.create_task(external._input(host, params, request))
        qid = await question_ready(app, handle, "c-d")
        effect = await external.cancel(handle)
        assert effect["cancellation_requested"] is True
        answer = await asyncio.wait_for(worker, 5)
        assert answer.action == "cancel"
        current = await app.state.task_history.io.run(app.state.task_history.task_snapshot, handle)
        question = await app.state.task_history.io.run(app.state.task_history.task_snapshot, qid)
        details = await app.state.task_history.io.run(external._details, current)
        assert calls == ["job-a-b"]
        assert question.status == "cancelled" and question.terminal_at is not None
        assert current.cancellation_requested_at is not None
        assert current.terminal_at is None and current.settlement != "confirmed"
        assert details["answers"]["c-d"] == {"action": "cancel"}
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        external.connections.pop(key, None)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_cancellation_preserves_an_already_confirmed_input_answer(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Reply-loss recovery retains the accepted answer rather than rewriting history."""
    app, conversations, external, host, params = await environment(arc, tmp_path)
    worker: asyncio.Task[Any] | None = None
    try:
        handle = await accepted(app, external, host, params)
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        worker = asyncio.create_task(external._input(host, params, request))
        qid = await question_ready(app, handle, "c-d")
        await app.state.task_history.io.run(
            app.state.questions.answer, qid, QuestionReply(metadata={"format": "png"})
        )
        confirmed = await asyncio.wait_for(worker, 5)
        current = await app.state.task_history.io.run(app.state.task_history.task_snapshot, handle)
        await app.state.task_history.io.run(
            app.state.task_history.record_snapshot,
            current.model_copy(update={"cancellation_requested_at": now()}),
            observation_id="cancel-after-answer",
        )
        replay = await asyncio.wait_for(external._input(host, params, request), 5)
        assert replay == confirmed and replay.action == "accept"
        assert replay.content == {"format": "png"}
        history = app.state.task_history
        human = (await history.io.run(history.task_snapshot, qid)).owner.human_id
        assert history.task_handles(kind="Question", human_id=human) == {qid}
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()
