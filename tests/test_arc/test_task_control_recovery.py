"""Applied approvals and cancelled launch/control inputs use their original Core owner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import CoreReference, TaskSnapshot
from clio_agent.gact.permission_gate import resolve_permission
from clio_agent.gact.question_surface import resolve_plan_question
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_hook_approval_contracts import prepared
from tests.test_arc.test_task_retirement_contracts import idle


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["thread", "temporary_loop"])
async def test_recovery_outlives_its_request_loop_and_runs_on_the_application_loop(
    arc: ARCMemory, tmp_path: Path, origin: str
) -> None:
    """Closing a request must not cancel the application's recording recovery."""
    _app, owner, root = await create_environment(arc, tmp_path)
    loop = asyncio.get_running_loop()
    registered = asyncio.Event()
    observed: list[asyncio.AbstractEventLoop] = []
    saved: list[CoreReference] = []

    async def repair() -> None:
        observed.append(asyncio.get_running_loop())
        saved.append(
            await owner.history.io.run(
                owner.history.write_content, root.owner.session_id, {"repaired": origin}
            )
        )

    def submit() -> None:
        async def request() -> None:
            owner.recovery.defer("request-owned-recording", repair)

        if origin == "temporary_loop":
            asyncio.run(request())
        else:
            owner.recovery.defer("request-owned-recording", repair)
        loop.call_soon_threadsafe(registered.set)

    try:
        await asyncio.to_thread(submit)
        await asyncio.wait_for(registered.wait(), 2)
        await asyncio.wait_for(owner.recovery.drain(), 10)
        assert observed == [loop]
        assert len(saved) == 1
        assert await owner.history.io.run(owner.history.read_content, saved[0]) == {
            "repaired": origin
        }
        assert not owner.recovery.pending
        assert owner.recovery._worker is not None and owner.recovery._worker.done()
        assert not owner.recovery._worker.cancelled()
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_applied_stop_completion_repairs_only_its_original_gate_after_restore(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, owner, root, _, handle = await prepared(arc, tmp_path, monkeypatch, event="Stop")
    update, failed = app.state.sessions.update, False

    def fail_release(sid: str, **kwargs: Any) -> Any:
        nonlocal failed
        if kwargs.get("metadata_patch", {}).get("execution_gate") == "" and not failed:
            failed = True
            raise OSError("Injected failure after terminal completion")
        return update(sid, **kwargs)

    monkeypatch.setattr(app.state.sessions, "update", fail_release)
    try:
        await asyncio.to_thread(resolve_permission, app, handle, "allow")
        jobs = tuple(owner._controls.values())
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        assert failed
        terminal = owner.history.task_snapshot(root.handle)
        assert (
            terminal is not None and terminal.status == "completed" and terminal.result is not None
        )
        assert await asyncio.to_thread(owner.history.read_content, terminal.result) == {
            "answer": "Approved exact output"
        }
        assert app.state.sessions.get(root.owner.session_id).metadata["execution_gate"] == handle
        frozen = await asyncio.to_thread(app.state.questions.resolution, handle)
        restored = TaskHistory(arc)
        await asyncio.to_thread(restored.restore)
        app.state.task_history = owner.history = restored
        app.state.questions = QuestionCommands(restored)
        task = restored.task_snapshot(handle)
        assert task is not None
        await asyncio.to_thread(resolve_plan_question, app, task)
        await asyncio.to_thread(resolve_plan_question, app, task)
        assert app.state.sessions.get(root.owner.session_id).metadata["execution_gate"] == ""
        assert restored.task_snapshot(root.handle) == terminal
        latest = restored.latest_assignment(root.owner.session_id)
        assert latest is not None and latest.handle == root.handle
        assert await asyncio.to_thread(app.state.questions.resolution, handle) == frozen
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_pause_resume_then_cancel_clears_own_question_gate_and_resume_instruction(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, owner, root, _, question = await prepared(arc, tmp_path, monkeypatch)
    caller, sid = (
        TaskCaller(root.owner.session_id, "human", human_action=True),
        root.owner.session_id,
    )

    async def cancel_question(task: TaskSnapshot) -> dict[str, Any]:
        settled = await asyncio.to_thread(app.state.questions.cancel, task.handle)
        return {"handle": settled.handle, "status": settled.status, "cancellation_requested": False}

    owner.cancel_work = cancel_question
    operations = TaskOperations(owner.history, lambda task, actor: owner.cancel(actor, task.handle))
    try:
        assert (await owner.pause(caller, root.handle))["pause_requested"]
        job = owner._controls.get("pause:" + root.handle)
        if job is not None:
            await job
        assert owner.history.task_snapshot(root.handle).activity == "paused"
        assert (await owner.resume(caller, root.handle))["resumed"]
        assert app.state.sessions.get(sid).metadata["agent_paused_assignment"] == ""
        assert app.state.sessions.get(sid).metadata["execution_gate"] == question
        resume = next(
            item
            for item in await asyncio.to_thread(owner.history.pending_mail, sid)
            if item.id.startswith("resume:")
        )
        assert (await owner.cancel(caller, root.handle))["cancellation_requested"]
        settled = await operations.wait(caller, root.handle)
        assert settled["outcomes"][0]["status"] == "cancelled" and not settled["pending"]
        assert owner.history.task_snapshot(question).status == "cancelled"
        assert app.state.sessions.get(sid).metadata["execution_gate"] == ""
        assert owner.history.mail_withdrawn(sid, resume.id)
        assert resume.id not in owner.history.acknowledged_notifications(sid)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_cancelled_queued_child_does_not_relaunch_from_its_retained_delegation(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    owner.execution_gate = lambda _sid: "human-review"
    owner.start()
    try:
        child = await owner.spawn(
            TaskCaller(root.owner.session_id, "human"),
            "Queued assignment",
            "writer",
            "queued-child",
        )
        sid, handle = child["session_id"], child["handle"]
        delegation = next(
            item
            for item in await asyncio.to_thread(owner.history.pending_mail, sid)
            if item.kind == "delegation"
        )
        caller = TaskCaller(root.owner.session_id, "human")
        operations = TaskOperations(
            owner.history, lambda task, actor: owner.cancel(actor, task.handle)
        )
        await owner.cancel(caller, handle)
        assert (await operations.wait(caller, handle))["outcomes"][0]["status"] == "cancelled"
        await idle(owner, sid)
        await idle(owner, root.owner.session_id)
        checked = asyncio.Event()
        real_admit = owner.runner.admit

        async def admit(session: str) -> Any:
            result = await real_admit(session)
            if session == sid:
                assert result is None
                checked.set()
            return result

        owner.runner.admit = admit
        owner.execution_gate = lambda _sid: None
        owner.runner.signal(sid)
        await asyncio.wait_for(checked.wait(), 10)
        latest = owner.history.latest_assignment(sid)
        assert latest is not None and latest.handle == handle
        assert owner.history.mail_withdrawn(sid, delegation.id)
        assert delegation.id not in owner.history.acknowledged_notifications(sid)
        assert any(
            item.handle == handle
            for item in await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id)
        )
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "restart"])
async def test_deferred_peer_control_rechecks_revoked_rights_before_input(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """A grant revoked after initial authorization cannot release or restart a peer."""
    from clio_agent.arc.task_types import CoordinationGrant
    from clio_agent.gact.native_work import now
    from clio_agent.gact.task_access import TaskAccessDenied

    app, owner, root = await create_environment(arc, tmp_path)
    history, sid = owner.history, root.owner.session_id
    human = TaskCaller(sid, "human")
    if action == "resume":
        await owner.pause(human, root.handle)
    else:
        await owner.cancel(human, root.handle)
        cancellation = owner._controls.get(root.handle)
        if cancellation is not None:
            await asyncio.wait_for(asyncio.shield(cancellation), 10)
        await asyncio.sleep(0)
        assert history.task_snapshot(root.handle).terminal_at is not None

    def grant_peer() -> tuple[str, CoordinationGrant]:
        peer = app.state.sessions.create(workspace_id="workspace", title="Peer controller")
        grant = CoordinationGrant(
            id="peer-control",
            granting_human="human",
            recipient_session=peer.id,
            target_session=sid,
            rights=("control",),
            created_at=now(),
        )
        history.record_grant(grant, observation_id="grant-control")
        return peer.id, grant

    peer, grant = await history.io.run(grant_peer)
    entered, release = asyncio.Event(), asyncio.Event()
    original = history.io.run

    async def hold(function: Any, *args: Any, **kwargs: Any) -> Any:
        name = getattr(function, "__qualname__", "")
        if (
            action == "resume"
            and name.endswith("resume.<locals>.inject")
            or action == "restart"
            and name.endswith("restart_settled.<locals>.create")
        ):
            entered.set()
            await release.wait()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(history.io, "run", hold)
    operation = asyncio.create_task(getattr(owner, action)(TaskCaller(peer, "human"), root.handle))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        continuation = (
            owner._controls.get("restart:" + root.handle) if action == "restart" else None
        )
        await original(
            history.record_grant,
            grant.model_copy(update={"revoked_at": now()}),
            observation_id="revoke-control",
        )
        release.set()
        if action == "resume":
            with pytest.raises(TaskAccessDenied):
                await operation
            assert owner.model_paused(sid)
        else:
            assert (await operation)["restart_requested"]
            assert continuation is not None
            with pytest.raises(TaskAccessDenied):
                await asyncio.wait_for(asyncio.shield(continuation), 10)
            assert history.latest_assignment(sid).handle == root.handle
        pending_mail = await original(history.pending_mail, sid)
        assert not [item for item in pending_mail if item.id.startswith(("resume:", "restart:"))]
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_an_agent_wait_ends_when_its_conversation_is_stopped(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    """Stop retires a turn only once its in-flight call returns: a wait_tasks blocked on
    running work must return when its own assignment is stopped, leaving the work running."""
    from uuid import uuid4

    app, owner, root = await create_environment(arc, tmp_path)
    owner.execution_gate = lambda _sid: "human-review"
    owner.start()
    try:
        caller = TaskCaller(root.owner.session_id, "human")
        child = await owner.spawn(caller, "Queued assignment", "writer", "queued-child")
        operations = TaskOperations(
            owner.history, lambda task, actor: owner.cancel(actor, task.handle)
        )
        waiting = asyncio.create_task(operations.wait(caller, child["handle"]))
        await asyncio.sleep(0.2)
        assert not waiting.done()

        current = owner.history.task_snapshot(root.handle)
        await asyncio.to_thread(
            owner.history.record_snapshot,
            current.model_copy(update={"activity": "stopping"}),
            observation_id=uuid4().hex,
        )
        result = await asyncio.wait_for(waiting, 5)

        assert result["interrupted"] == "stopped"
        assert result["pending"] == [child["handle"]]
        assert owner.history.task_snapshot(child["handle"]).terminal_at is None
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
