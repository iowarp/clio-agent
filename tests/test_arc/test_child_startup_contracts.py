"""Child startup belongs to its exclusive session worker, not the shared task writer."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionDefinition
from clio_agent.gact.hooks.wire import HookCancelled, HookOutcome
from clio_agent.gact.provenance_wiring import install_session_lifecycle_observer
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.runtime.globals import _emit_semantic_event
from clio_agent.gact.semantic_events import semantic_event_from_events_content
from clio_agent.gact.session_records import PageQuery
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.child_worker_helpers import execute_child
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_replacement_contracts import forbid_cancel
from tests.test_arc.test_task_retirement_contracts import idle


@pytest.mark.asyncio
async def test_simultaneous_first_submissions_share_one_child_mail_and_startup(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    hooks: list[str] = []
    processed = asyncio.Event()

    def hook(*, session_id: str, **_kwargs: Any) -> None:
        hooks.append(session_id)

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", hook)
    try:
        first, second = await asyncio.gather(
            owner.spawn(caller, "Same original assignment", "worker", "simultaneous"),
            owner.spawn(caller, "Same original assignment", "worker", "simultaneous"),
        )
        assert first == second and first["accepted"] is True
        children = [
            row
            for row in app.state.sessions.select(PageQuery(archived=None))
            if row.parent_session_id
        ]
        assert [row.id for row in children] == [first["session_id"]]
        assert (
            len((await asyncio.to_thread(owner.history.pending_mail, first["session_id"]))) == 1
            and not hooks
        )

        async def model(segment: Any) -> Any:
            try:
                return await execute_child(app, segment, lambda: {"answer": "one execution"})
            finally:
                processed.set()

        owner.run_segment = model
        await owner.pause(TaskCaller(caller.session_id, "human", human_action=True), root.handle)
        for job in tuple(owner._controls.values()):
            await job
        owner.start()
        owner.recovery.on_ready = None
        owner.runner.signal(first["session_id"])
        await asyncio.wait_for(processed.wait(), 10)
        await idle(owner, first["session_id"])
        assert hooks == [first["session_id"]]
        assert not (await asyncio.to_thread(owner.history.pending_mail, first["session_id"]))
        assert (
            await owner.spawn(caller, "Same original assignment", "worker", "simultaneous") == first
        )
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_slow_startup_does_not_block_task_reads_questions_or_same_identity_reuse(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    entered, release, processed = threading.Event(), threading.Event(), asyncio.Event()
    threads: list[str] = []
    models: list[str] = []

    def hook(**_kwargs: Any) -> None:
        threads.append(threading.current_thread().name)
        entered.set()
        assert release.wait(10), "Test did not release its owned startup hook"

    async def model(segment: Any) -> Any:
        def action() -> dict[str, Any]:
            models.append(segment.session_id)
            return {}

        try:
            return await execute_child(app, segment, action)
        finally:
            processed.set()

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", hook)
    accepted = await owner.spawn(caller, "Slow startup", "worker", "slow-start")
    child_id = accepted["session_id"]
    assert not entered.is_set() and not models
    owner.run_segment = model
    await owner.pause(TaskCaller(caller.session_id, "human", human_action=True), root.handle)
    for job in tuple(owner._controls.values()):
        await job
    owner.start()
    owner.recovery.on_ready = None
    try:
        owner.runner.signal(child_id)
        assert await asyncio.to_thread(entered.wait, 5)
        assert not models
        assert threads == ["clio-session-" + child_id]
        operations = TaskOperations(owner.history, forbid_cancel)
        page = await asyncio.wait_for(asyncio.to_thread(operations.query, caller), 2)
        assert accepted["handle"] in {task["handle"] for task in page["tasks"]}
        commands = QuestionCommands(owner.history)
        question = await asyncio.wait_for(
            asyncio.to_thread(
                commands.create,
                root.owner,
                QuestionDefinition(prompt="Unrelated question", source="native"),
                invocation_id="while-startup",
            ),
            2,
        )
        cancelled = await asyncio.wait_for(asyncio.to_thread(commands.cancel, question.handle), 2)
        assert cancelled.raw_status == "cancelled" and cancelled.terminal_at is not None
        reused = await asyncio.wait_for(
            owner.spawn(caller, "Slow startup", "worker", "slow-start"), 2
        )
        assert reused == accepted and len(threads) == 1
        with pytest.raises(RuntimeError, match="already running"):
            await asyncio.wait_for(asyncio.to_thread(app.state.sessions.initialize, child_id), 2)
        release.set()
        await asyncio.wait_for(processed.wait(), 10)
        await idle(owner, child_id)
        assert models == [child_id]
        completed = await asyncio.to_thread(owner.history.task_snapshot, accepted["handle"])
        assert completed is not None and completed.status == "completed"
    finally:
        release.set()
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_cancelling_startup_settles_its_worker_before_terminal_publication(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    entered, cancelling, release = threading.Event(), threading.Event(), threading.Event()
    models: list[str] = []

    def hook(*, cancel_event: threading.Event, **_kwargs: Any) -> None:
        entered.set()
        assert cancel_event.wait(10), "Cancellation did not reach the startup owner"
        cancelling.set()
        assert release.wait(10), "Test did not release its cleanup receipt"
        raise HookCancelled("owned-startup", invocation_started=True, cleanup_confirmed=True)

    async def model(segment: Any) -> Any:
        def action() -> dict[str, Any]:
            models.append("called")
            return {}

        return await execute_child(app, segment, action)

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", hook)
    accepted = await owner.spawn(caller, "Cancellation during startup", "worker", "cancel-start")
    owner.run_segment = model
    await owner.pause(TaskCaller(caller.session_id, "human", human_action=True), root.handle)
    for job in tuple(owner._controls.values()):
        await job
    owner.start()
    owner.recovery.on_ready = None
    try:
        owner.runner.signal(accepted["session_id"])
        assert await asyncio.to_thread(entered.wait, 5)
        acknowledgement = await owner.cancel(caller, accepted["handle"])
        assert acknowledgement["cancellation_requested"] is True
        assert await asyncio.to_thread(cancelling.wait, 5)
        # The cancellation effect reaches the held hook before its deferred
        # Core stamp; this wait confirms recording, not physical settlement.
        if acknowledgement.get("recording") == "unconfirmed":
            await asyncio.wait_for(owner.recovery.drain(), 5)
        current = await asyncio.to_thread(owner.history.task_snapshot, accepted["handle"])
        assert (
            current is not None
            and current.cancellation_requested_at
            and current.terminal_at is None
        )
        assert owner.runner.handle(accepted["session_id"]) is not None
        assert not models
        with pytest.raises(ValueError, match="admission is closed"):
            await owner.spawn(
                TaskCaller(accepted["session_id"], "human"), "Escape", "worker", "late"
            )
        release.set()
        await idle(owner, accepted["session_id"])
        for job in tuple(owner._controls.values()):
            await job
        final = await asyncio.to_thread(owner.history.task_snapshot, accepted["handle"])
        assert (
            final is not None
            and final.status == "cancelled"
            and final.terminal_at
            and final.settlement == "confirmed"
        )
        slot = owner.runner.handle(accepted["session_id"])
        assert slot is None or not slot.turn_id, "Cancelled startup cannot admit another segment"
        assert not models
        latest = await asyncio.to_thread(owner.history.latest_assignment, accepted["session_id"])
        assert latest is not None and latest.handle == accepted["handle"]
        retained = await asyncio.to_thread(app.state.sessions.initialize, accepted["session_id"])
        assert retained.creation_hook_state == "interrupted" and not models
    finally:
        release.set()
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("reconstruct", [False, True])
@pytest.mark.parametrize("chunk_capacity", [1, 1000])
async def test_pre_hook_provenance_recovery_reuses_the_confirmed_creation_event(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_commit: bool,
    reconstruct: bool,
    chunk_capacity: int,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    accepted = await owner.spawn(caller, "Original assignment", "worker", "provenance-fault")
    child_id = accepted["session_id"]
    # Exercise the production lane writer's configured capacity without creating
    # another Core owner. Preparation already can precede session.created.
    monkeypatch.setattr(arc, "_events_chunk_segments", chunk_capacity)
    await asyncio.to_thread(_emit_semantic_event, app, child_id, "turn.preparation", payload={})
    original = app.state.sessions._observe_lifecycle
    fired = False
    hooks: list[str] = []
    creation_fanout: list[str] = []

    def semantic_hook(event: Any, *, session_id: str, **_kwargs: Any) -> HookOutcome:
        if event.get("event_type") == "session.created":
            creation_fanout.append(session_id)
        return HookOutcome()

    def observe(event_type: str, session: Any) -> None:
        nonlocal fired
        if not fired and not after_commit:
            fired = True
            raise OSError("Creation observation unavailable")
        original(event_type, session)
        if not fired:
            fired = True
            raise OSError("Creation observation reply lost")

    def hook(*, session_id: str, **_kwargs: Any) -> None:
        hooks.append(session_id)

    monkeypatch.setattr(app.state.sessions, "_observe_lifecycle", observe)
    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", hook)
    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_semantic_event", semantic_hook)
    try:
        with pytest.raises(OSError, match="unavailable|reply lost"):
            await asyncio.to_thread(app.state.sessions.initialize, child_id)
        assert fired and not hooks
        if reconstruct:
            await asyncio.to_thread(arc.release_session, child_id)
            app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
            owner.sessions = app.state.sessions
            install_session_lifecycle_observer(app)
        await asyncio.to_thread(app.state.sessions.initialize, child_id)
        await asyncio.to_thread(app.state.sessions.initialize, child_id)
        assert hooks == [child_id]
        rows = await asyncio.to_thread(
            lambda: list(arc._live.iter_session_event_segments(child_id))
        )
        events = [
            semantic_event_from_events_content(row.content, session_id=child_id)
            for row in rows
            if row.kind == "semantic_event"
        ]
        assert sum(event.event_type == "session.created" for event in events) == 1
        assert creation_fanout == [child_id]
        if chunk_capacity == 1:
            scopes = await asyncio.to_thread(arc._live.events_scopes, child_id)
            assert len(scopes) > 1
            first = await asyncio.to_thread(arc.list_segments, child_id, "_events")
            assert not any(
                semantic_event_from_events_content(row.content, session_id=child_id).event_type
                == "session.created"
                for row in first
                if row.kind == "semantic_event"
            )
        assert (
            await owner.spawn(caller, "Original assignment", "worker", "provenance-fault")
            == accepted
        )
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_trusted_inline_initialization_rejects_loop_thread_before_any_row_or_effect(
    tmp_path: Path,
) -> None:
    store = SessionStore(path=tmp_path / "sessions.json")
    with pytest.raises(RuntimeError, match="off the event loop"):
        store.create(workspace_id="workspace", creation_id="trusted", creation_digest="a" * 64)
    assert store.ids() == [] and not (tmp_path / "sessions.json").exists()


@pytest.mark.asyncio
async def test_partial_child_reuse_preserves_human_approval_mode_and_original_metadata(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    original, failed = owner.history.record_snapshot, False

    def record(snapshot: Any, **kwargs: Any) -> None:
        nonlocal failed
        if snapshot.kind == "Subagent" and not failed:
            failed = True
            raise OSError("Assignment not yet confirmed")
        original(snapshot, **kwargs)

    monkeypatch.setattr(owner.history, "record_snapshot", record)
    metadata = {"original": "retained"}
    try:
        with pytest.raises(OSError, match="not yet confirmed"):
            await owner.spawn(
                caller,
                "Original",
                "worker",
                "partial-profile",
                approval_profile="read-only-side",
                session_metadata=metadata,
            )
        child = next(
            row
            for row in app.state.sessions.select(PageQuery(archived=None))
            if row.parent_session_id
        )
        assert child.approval_profile == "read-only-side" and child.approval_mode == "ask"
        await asyncio.to_thread(app.state.sessions.update, child.id, approval_mode="ai-review")
        await asyncio.to_thread(owner.history.restore)
        accepted = await owner.spawn(
            caller,
            "Original",
            "worker",
            "partial-profile",
            approval_profile="read-only-side",
            session_metadata=metadata,
        )
        assert accepted["session_id"] == child.id
        assert child.approval_mode == "ai-review" and child.metadata["original"] == "retained"
        with pytest.raises(ValueError, match="different work"):
            await owner.spawn(
                caller,
                "Original",
                "worker",
                "partial-profile",
                approval_profile="spotter-watcher",
                session_metadata=metadata,
            )
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
