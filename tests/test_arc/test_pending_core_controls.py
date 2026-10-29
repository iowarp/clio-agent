"""A pending confirmed writer cannot prevent verified owner controls."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import BackendReference, TaskOwner, TaskSnapshot
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks
from clio_agent.gact.human_controls import intervene
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_history import TaskHistory
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_native_cancel_recovery import native_owner


@asynccontextmanager
async def pending_writer(history: TaskHistory) -> AsyncIterator[None]:
    """Hold an actual writer boundary until the test releases its pending I/O."""
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def write() -> None:
        with history.serialized():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "The pending writer was not released"

    writing = asyncio.create_task(asyncio.to_thread(write))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        yield
    finally:
        release.set()
        await asyncio.wait_for(writing, 5)


@pytest.mark.asyncio
async def test_native_cancel_reaches_bound_owner_while_core_writer_is_pending(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept control, close admission and deliver RPC before confirming recording."""
    native, recovery = await native_owner(arc, monkeypatch)
    original = native.history.task_snapshot("task_cancel")
    assert original is not None
    backend = BackendReference(
        owner="native", server="shell", session="native-session", task_id="original-task"
    )
    reference = await asyncio.to_thread(
        native.history.write_content, "owner", {"original_task_id": backend.task_id}
    )
    await asyncio.to_thread(
        native.history.record_snapshot,
        original.model_copy(update={"backend": backend, "submission": reference}),
        observation_id="identity",
    )
    sent = asyncio.Event()

    async def request_cancel() -> None:
        sent.set()

    async def forbidden_storage_lookup(_task: Any) -> bool:
        pytest.fail("Shell cancellation must not wait for storage I/O")

    native._calls[original.handle] = SimpleNamespace(
        submission=SimpleNamespace(context=reference),
        task=SimpleNamespace(task_id=backend.task_id),
        request_cancel=request_cancel,
    )
    native.provider_cancel = forbidden_storage_lookup
    try:
        async with pending_writer(native.history):
            acknowledged = await asyncio.wait_for(
                native.cancel(original.handle, caller=TaskCaller("owner", "human")), 2
            )
            assert sent.is_set()
            assert acknowledged["cancellation_requested"] is True
            assert acknowledged["recording"] == "unconfirmed"
            assert native.admission_closed(original.handle)
            current = native.history.task_snapshot(original.handle)
            assert current is not None and current.terminal_at is None
            assert current.cancellation_requested_at is None
        await recovery.drain()
        confirmed = native.history.task_snapshot(original.handle)
        assert confirmed is not None and confirmed.cancellation_requested_at is not None
        assert confirmed.terminal_at is None  # An RPC acknowledgement is not physical settlement.
    finally:
        await recovery.drain()


@pytest.mark.asyncio
async def test_pause_and_human_notice_do_not_wait_for_pending_core_writer(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Pause a verified child now; retain attributed parent mail for reconciliation."""
    app, owner, root = await create_environment(arc, tmp_path)
    app.state.task_recovery = owner.recovery
    child = await owner.spawn(
        TaskCaller(root.owner.session_id, "human"), "Child assignment", "worker", "child"
    )
    try:
        async with pending_writer(owner.history):
            acknowledged = await asyncio.wait_for(intervene(app, child["handle"], "pause"), 2)
            assert acknowledged["pause_requested"] is True
            assert acknowledged["recording"] == "unconfirmed"
            assert owner.model_paused(child["session_id"])
            current = TaskAccess(owner.history, TaskCaller("", "human", human_action=True)).require(
                child["handle"], "control"
            )
            assert current.terminal_at is None
        await owner.recovery.drain()
        notice_id = acknowledged["intervention_id"] + ":parent"
        notice = owner.history.mail_item(root.owner.session_id, notice_id)
        assert notice is not None and notice.sender_human == "human"
        body = await asyncio.to_thread(owner.history.read_content, notice.content)
        assert body["handle"] == child["handle"] and body["action"] == "pause"
        pending = await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id)
        assert [item.id for item in pending] == [notice_id]
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_subtree_control_closes_admission_before_recording(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel requests reach descendants even while a writer cannot confirm intent."""
    app, owner, root = await create_environment(arc, tmp_path)
    descendant = root.model_copy(
        update={
            "handle": "native_descendant",
            "kind": "Shell",
            "owner": root.owner.model_copy(update={"parent_handle": root.handle}),
            "invocation_id": "native-descendant",
        }
    )
    await asyncio.to_thread(owner.history.record_snapshot, descendant, observation_id="descendant")
    dispatched = asyncio.Event()

    async def request_cancel(_task: Any) -> dict[str, Any]:
        dispatched.set()
        return {"cancellation_requested": True, "recording": "unconfirmed"}

    monkeypatch.setattr(owner, "cancel_work", request_cancel)
    try:
        async with pending_writer(owner.history):
            response = await asyncio.wait_for(
                owner.cancel(TaskCaller(root.owner.session_id, "human"), root.handle), 2
            )
            assert response["cancellation_requested"] is True
            assert response["recording"] == "unconfirmed"
            assert root.handle in owner._control_holds
            await asyncio.wait_for(dispatched.wait(), 2)
        await owner.recovery.drain()
        current = owner.history.task_snapshot(root.handle)
        assert current is not None and current.cancellation_requested_at is not None
    finally:
        # Finish this test-only execution owner so production shutdown can settle.
        settled = descendant.model_copy(
            update={
                "terminal_at": now(),
                "status": "cancelled",
                "raw_status": "cancelled",
                "settlement": "confirmed",
            }
        )
        await asyncio.to_thread(owner.history.record_snapshot, settled, observation_id="cleanup")
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_failed_restore_keeps_the_last_confirmed_control_identity(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read of Core that fails part-way never shows the task as gone; a retry finds it.

    A load that loses Core visibility raises; it does not leave a half-built tree
    (or, before the index build, a half-built replay) that would erase the task or
    its control rights.
    """
    native, recovery = await native_owner(arc, monkeypatch)
    history = native.history
    original = history.task_snapshot("task_cancel")
    assert original is not None
    segments = history.arc._live.iter_session_event_segments
    failures = iter([True])

    def interrupted_load(session_id: str) -> Iterator[Any]:
        stream = iter(segments(session_id))
        if next(failures, False):
            yield next(stream)
            raise OSError("Load visibility lost after its first segment")
        yield from stream

    monkeypatch.setattr(history.arc._live, "iter_session_event_segments", interrupted_load)

    def restore_then_read() -> Any:
        # Before the one-time index build, restore itself replays; after it, the
        # first read loads the tree. Either way the injected loss surfaces here.
        history.restore()
        return history.task_snapshot(original.handle)

    with pytest.raises(OSError, match="Load visibility lost"):
        await asyncio.to_thread(restore_then_read)
    assert await asyncio.to_thread(history.task_snapshot, original.handle) == original
    assert history.resolve_handle(original.handle) == original.handle
    assert (
        TaskAccess(history, TaskCaller("owner", "human")).require(original.handle, "control")
        == original
    )
    await recovery.drain()


@pytest.mark.asyncio
async def test_external_cancel_reaches_verified_sdk_owner_while_writer_is_pending(
    arc: ARCMemory,
) -> None:
    """A blocked writer cannot delay an authorized external cancellation RPC."""
    history = TaskHistory(arc)
    recovery = CoreRecovery(history)
    external = ExternalTasks(
        SimpleNamespace(state=SimpleNamespace(task_history=history, task_recovery=recovery))
    )
    stamp = now()
    backend = BackendReference(
        owner="external_mcp",
        server="owned-server",
        session="original-session",
        task_id="original-task",
        auth_scope="verified-scope",
        transport="StreamableHttpTransport",
    )
    current = TaskSnapshot(
        handle="task_external_cancel",
        kind="MCP",
        owner=TaskOwner(human_id="human", session_id="owner", assignment_id="assignment"),
        backend=backend,
        tool="controlled",
        label="External work",
        invocation_id="external-cancel",
        created_at=stamp,
        updated_at=stamp,
        status="running",
        raw_status="working",
    )
    await history.io.run(history.record_snapshot, current, observation_id="accepted")
    delivered = asyncio.Event()

    async def request_cancel() -> None:
        delivered.set()

    connection = ExternalConnection(backend.model_copy(update={"task_id": ""}))
    connection.calls[backend.task_id] = SimpleNamespace(
        submission=SimpleNamespace(handle=current.handle),
        task=SimpleNamespace(task_id=backend.task_id),
        request_cancel=request_cancel,
    )
    external.connections["verified"] = connection
    try:
        async with pending_writer(history):
            acknowledgement = await asyncio.wait_for(
                external.cancel(current.handle, caller=TaskCaller("owner", "human")), 2
            )
            assert delivered.is_set() and acknowledgement["cancellation_requested"]
            assert acknowledgement["recording"] == "unconfirmed"
            assert acknowledgement["settlement"] == "external_acknowledged"
            assert history.task_snapshot(current.handle) == current
        await recovery.drain()
        confirmed = history.task_snapshot(current.handle)
        assert confirmed is not None and confirmed.cancellation_requested_at is not None
        assert confirmed.backend == backend and confirmed.terminal_at is None
        assert confirmed.raw_status == "working"  # The RPC is not a cleanup receipt.
    finally:
        await recovery.drain()
        await history.io.close()
