"""Reusable child addresses across confirmed and uncertain pre-launch Core writes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import AssignmentDetails, TaskSnapshot
from clio_agent.gact.provenance_wiring import install_session_lifecycle_observer
from clio_agent.gact.semantic_events import semantic_event_from_events_content
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.task_access import TaskCaller
from tests.test_arc.child_worker_helpers import execute_child
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_retirement_contracts import idle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["session_flush", "before_task", "after_task"],
)
@pytest.mark.parametrize("reconstruct", [False, True])
async def test_spawn_retry_reuses_the_child_address_without_replaying_creation_hooks(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, reconstruct: bool
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    original_snapshot, original_persist = owner.history.record_snapshot, app.state.sessions._persist
    fired = False
    hooks: list[str] = []

    def hook(*, session_id: str, **_kwargs: Any) -> None:
        hooks.append(session_id)

    def persist(session: Any) -> None:
        nonlocal fired
        if fault == "session_flush" and not fired:
            fired = True
            raise OSError("session projection flush unavailable")
        original_persist(session)

    def record(snapshot: TaskSnapshot, **kwargs: Any) -> None:
        nonlocal fired
        selected = snapshot.kind == "Subagent" and not fired and fault != "session_flush"
        if selected and fault == "before_task":
            fired = True
            raise OSError("task header unavailable before confirmation")
        original_snapshot(snapshot, **kwargs)
        if selected and fault == "after_task":
            fired = True
            raise OSError("task header reply lost after confirmation")

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", hook)
    monkeypatch.setattr(app.state.sessions, "_persist", persist)
    monkeypatch.setattr(owner.history, "record_snapshot", record)
    try:
        with pytest.raises(OSError, match="unavailable|reply lost"):
            await owner.spawn(caller, "Original assignment", "worker", "original-invocation")
        assert fired and owner.history.requires_recovery
        children = [
            child
            for child in await asyncio.to_thread(app.state.sessions.list)
            if child.parent_session_id == root.owner.session_id
        ]
        assert len(children) == 1
        child_id = children[0].id
        assert len(children[0].creation_digest) == 64
        assert "creation_digest" not in children[0].to_wire()
        assert not (await asyncio.to_thread(owner.history.pending_mail, child_id))

        def recover() -> None:
            owner.history.restore()
            if reconstruct:
                app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
                owner.sessions = app.state.sessions
                install_session_lifecycle_observer(app)

        await asyncio.to_thread(recover)
        if fault == "before_task" or (fault == "session_flush" and not reconstruct):
            with pytest.raises(ValueError, match="different work"):
                await owner.spawn(caller, "Changed assignment", "worker", "original-invocation")
        accepted = await owner.spawn(caller, "Original assignment", "worker", "original-invocation")
        assert accepted["session_id"] == child_id and accepted["accepted"] is True
        assert (
            await owner.spawn(caller, "Original assignment", "worker", "original-invocation")
            == accepted
        )
        current = await asyncio.to_thread(owner.history.task_snapshot, accepted["handle"])
        assert current is not None and current.description == "Original assignment"
        assert current.terminal_at is None and current.activity == "queued"
        children = [
            child
            for child in await asyncio.to_thread(app.state.sessions.list)
            if child.parent_session_id == root.owner.session_id
        ]
        assert [child.id for child in children] == [child_id]
        mail = await asyncio.to_thread(owner.history.pending_mail, child_id)
        assert len(mail) == 1 and mail[0].kind == "delegation" and mail[0].handle == current.handle
        assert not hooks, "Acceptance must not run SessionStart on the shared task writer"

        processed = asyncio.Event()

        async def model(segment: Any) -> Any:
            try:
                return await execute_child(app, segment, lambda: {"answer": "Initialized once"})
            finally:
                processed.set()

        owner.run_segment = model
        await owner.pause(TaskCaller(caller.session_id, "human", human_action=True), root.handle)
        for job in tuple(owner._controls.values()):
            await job
        owner.start()
        owner.recovery.on_ready = None
        owner.runner.signal(child_id)
        await asyncio.wait_for(processed.wait(), 10)
        await idle(owner, child_id)
        assert hooks == [child_id]
        creation = await asyncio.to_thread(arc.list_segments, child_id, "_events")
        events = [
            semantic_event_from_events_content(row.content, session_id=child_id)
            for row in creation
            if row.kind == "semantic_event"
        ]
        assert len([event for event in events if event.event_type == "session.created"]) == 1
        child = app.state.sessions.get(child_id)
        assert child is not None and child.creation_lifecycle_confirmed
        assert child.creation_hook_state == "completed"
        assert not {"creation_lifecycle_confirmed", "creation_hook_state"} & child.to_wire().keys()
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["hook_effect", "hook_receipt"])
@pytest.mark.parametrize("reconstruct", [False, True])
async def test_unconfirmed_hook_effects_are_not_replayed_by_session_reuse(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    reconstruct: bool,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    effect = tmp_path / "hook-effect.txt"
    original_persist = app.state.sessions._persist
    fired = False

    def hook(**_kwargs: Any) -> None:
        effect.write_text((effect.read_text() if effect.exists() else "") + "effect\n")
        if fault == "hook_effect":
            raise OSError("Hook audit unavailable after the visible effect")

    def persist(session: Any) -> None:
        nonlocal fired
        if (
            fault == "hook_receipt"
            and not fired
            and session.parent_session_id
            and session.creation_hook_state == "completed"
        ):
            fired = True
            raise OSError("Hook completion receipt unavailable")
        original_persist(session)

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", hook)
    # A configured hook (the fake above) runs, so creation records its "started" receipt.
    monkeypatch.setattr("clio_agent.gact.hooks.session_start_may_run_hooks", lambda _sid: True)
    monkeypatch.setattr(app.state.sessions, "_persist", persist)
    try:
        accepted = await owner.spawn(caller, "Original assignment", "worker", "hook-fault")
        assert accepted["accepted"] is True and not effect.exists()
        models: list[str] = []
        processed = asyncio.Event()

        async def model(segment: Any) -> Any:
            def action() -> dict[str, Any]:
                models.append("ran")
                return {}

            try:
                return await execute_child(app, segment, action)
            finally:
                processed.set()

        owner.run_segment = model
        await owner.pause(TaskCaller(caller.session_id, "human", human_action=True), root.handle)
        for job in tuple(owner._controls.values()):
            await job
        owner.start()
        owner.recovery.on_ready = None
        owner.runner.signal(accepted["session_id"])
        await asyncio.wait_for(processed.wait(), 10)
        await idle(owner, accepted["session_id"])
        assert effect.read_text() == "effect\n" and not models
        outcome = await asyncio.to_thread(owner.history.task_snapshot, accepted["handle"])
        assert outcome is not None and outcome.details is not None
        if fault == "hook_effect":
            assert outcome.status == "task_state_uncertain"
            assert outcome.terminal_at is None and outcome.result is None
            assert outcome.settlement == "unknown" and outcome.attention_episode is not None
        else:
            assert outcome.status == "failed" and outcome.result is not None
        details = AssignmentDetails.model_validate(
            await asyncio.to_thread(owner.history.read_content, outcome.details)
        )
        assert details.last_output is not None
        result = await asyncio.to_thread(owner.history.read_content, details.last_output)
        assert "unavailable" in str(result)
        assert any(
            item.handle == outcome.handle
            for item in (await asyncio.to_thread(owner.history.pending_mail, caller.session_id))
        )
        children = [
            child
            for child in app.state.sessions.list()
            if child.parent_session_id == root.owner.session_id
        ]
        assert len(children) == 1
        child_id = children[0].id

        def recover() -> None:
            owner.history.restore()
            if reconstruct:
                app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
                owner.sessions = app.state.sessions
                install_session_lifecycle_observer(app)

        await asyncio.to_thread(recover)
        reused = await owner.spawn(caller, "Original assignment", "worker", "hook-fault")
        assert reused == accepted, "A historical handle cannot start another model or hook"
        if fault == "hook_receipt" and not reconstruct:
            await asyncio.to_thread(app.state.sessions.initialize, child_id)
        else:
            with pytest.raises(RuntimeError, match="effects are unconfirmed"):
                await asyncio.to_thread(app.state.sessions.initialize, child_id)
            pending = await asyncio.to_thread(owner.history.pending_mail, child_id)
            assert len(pending) == 1 and pending[0].delivery == "held"
            assert pending[0].held_reason == "execution_failed_before_input_commit"
            assert await asyncio.to_thread(owner.history.latest_assignment, child_id) == outcome
        assert effect.read_text() == "effect\n"
        assert (
            len(
                [
                    child
                    for child in app.state.sessions.list()
                    if child.parent_session_id == root.owner.session_id
                ]
            )
            == 1
        )
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
