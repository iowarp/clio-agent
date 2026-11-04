"""Owner vocabularies, narrow reads and recovery against the real Core writer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import MailItem, TaskOwner, TaskSnapshot
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.tools.task_client import TaskAwareClient

STAMP = "2026-10-14T23:00:00Z"


def task(handle: str, *, session: str = "owner", parent: str | None = None) -> TaskSnapshot:
    """Construct an observation, without pretending to execute a workload."""
    return TaskSnapshot(
        handle=handle,
        kind="Shell",
        owner=TaskOwner(
            human_id="human", session_id=session, assignment_id=handle, parent_handle=parent
        ),
        label="Retained command",
        tool="shell_bash",
        invocation_id=handle,
        created_at=STAMP,
        updated_at=STAMP,
    )


@pytest.mark.parametrize("field", ["status", "activity", "settlement", "connection_freshness"])
def test_invalid_live_observation_cannot_reach_core(arc: ARCMemory, field: str) -> None:
    history = TaskHistory(arc)
    original = task("task_invalid")
    history.record_snapshot(original, observation_id="valid")
    before = len(list(arc._live.iter_event_contents()))
    with pytest.raises(ValidationError):
        history.record_snapshot(
            original.model_copy(update={field: "unrecognized-owner-state"}),
            observation_id="invalid",
        )
    assert history.task_snapshot(original.handle) == original
    assert len(list(arc._live.iter_event_contents())) == before
    assert not history.requires_recovery


@pytest.mark.parametrize("terminal", [False, True])
def test_unknown_historical_vocabulary_preserves_raw_identity_and_uncertainty(
    terminal: bool,
) -> None:
    original = task("task_historical").model_dump(mode="json")
    original.update(
        raw_status="provider-specific-state",
        status="old-owner-state",
        activity="old-activity",
        settlement="old-settlement",
        connection_freshness="old-freshness",
        terminal_at=STAMP if terminal else None,
    )
    restored = TaskSnapshot.read_history(original)
    assert restored.handle == original["handle"] and restored.owner.session_id == "owner"
    assert restored.raw_status == "provider-specific-state"
    assert restored.status == ("unknown" if terminal else "task_state_uncertain")
    assert restored.activity == "unavailable" and restored.settlement == "historical_unknown"
    assert restored.terminal_at == original["terminal_at"]


@pytest.mark.timeout(120)  # Real Core history/replay beyond 1,000 retained task observations.
def test_exact_and_active_reads_are_bounded_by_candidates_not_retained_history(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch, record_property: Any
) -> None:
    history = TaskHistory(arc)
    root = task("task_root").model_copy(update={"kind": "Session"})
    history.record_snapshot(root, observation_id="root")
    for index in range(1001):
        retained = task(f"task_old_{index}", parent=root.handle).model_copy(
            update={"status": "completed", "terminal_at": STAMP, "settlement": "confirmed"}
        )
        history.record_snapshot(retained, observation_id=f"old_{index}")
    active = task("task_active", parent=root.handle)
    child = task("task_child", session="child", parent=root.handle)
    history.record_snapshot(active, observation_id="active")
    history.record_snapshot(child, observation_id="child")
    restored = TaskHistory(arc)
    restored.restore()
    record_property("startup_replay_seconds", restored.replay_seconds)
    reads = 0
    read_snapshot = restored.task_snapshot

    def count_snapshot(handle: str) -> TaskSnapshot | None:
        nonlocal reads
        reads += 1
        return read_snapshot(handle)

    def no_broad_read(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Narrow task read reconstructed retained history")

    async def no_cancel(_task: TaskSnapshot, _caller: TaskCaller) -> dict[str, Any]:
        pytest.fail("Query attempted execution control")

    monkeypatch.setattr(restored, "task_snapshot", count_snapshot)
    monkeypatch.setattr(restored, "tree_handles", no_broad_read)
    monkeypatch.setattr(arc, "list_segments", no_broad_read)
    operations = TaskOperations(restored, no_cancel)
    caller = TaskCaller("owner", "human")
    exact = operations.query(caller, handle=active.handle)
    exact_reads = reads
    assert exact["total"] == 1 and exact["tasks"][0]["handle"] == active.handle
    assert exact_reads <= 8
    reads = 0
    running = operations.query(caller, active_only=True)
    assert running["total"] == 3
    assert {row["handle"] for row in running["tasks"]} == {root.handle, active.handle, child.handle}
    assert reads <= 20
    record_property("exact_snapshot_reads", exact_reads)
    record_property("active_snapshot_reads", reads)
    child_caller = TaskCaller("child", "human")
    assert operations.query(child_caller, handle=root.handle)["total"] == 0
    with pytest.raises(ValueError, match="unavailable"):
        TaskAccess(restored, child_caller).require(root.handle, "read")


@pytest.mark.asyncio
async def test_readiness_failure_after_last_owner_flush_remains_recoverable(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import task_history as task_history_module

    history = TaskHistory(arc)
    recovery = CoreRecovery(history)
    create = task_history_module.create_scope_once
    fail_next = True
    ready_calls = 0
    flush_calls = 0

    def faulted_create(*args: Any, **kwargs: Any) -> Any:
        # Content is created once, atomically (iowarp-core 2.4), not appended.
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise OSError("readiness content write unavailable")
        return create(*args, **kwargs)

    async def flush() -> bool:
        nonlocal flush_calls
        flush_calls += 1
        return True

    async def ready() -> None:
        nonlocal ready_calls
        ready_calls += 1

        def record() -> None:
            content = history.write_content("owner", {"text": "retained readiness"})
            history.record_mail(
                MailItem(
                    id="ready",
                    recipient_session="owner",
                    kind="hook",
                    content=content,
                    created_at=STAMP,
                )
            )

        await asyncio.to_thread(record)

    monkeypatch.setattr(task_history_module, "create_scope_once", faulted_create)
    recovery.on_ready = ready
    recovery.defer("last-owner-flush", flush)
    await asyncio.wait_for(recovery.drain(), 10)
    assert flush_calls == 1 and ready_calls == 2
    assert not recovery.pending and not history.requires_recovery
    pending = await asyncio.to_thread(history.pending_mail, "owner")
    assert [item.id for item in pending] == ["ready"]


@pytest.mark.asyncio
async def test_transport_health_timeout_retires_probe_without_cancelling_other_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_acceptance(_call: Any) -> Any:
        pytest.fail("Health checking submitted a task")

    client = TaskAwareClient(FastMCP("health contract"), on_task_accepted=reject_acceptance)
    client.health_timeout_s = 0.02
    probe_retired = asyncio.Event()
    release = asyncio.Event()

    async def blocked_probe() -> list[Any]:
        try:
            await asyncio.Event().wait()
            return []
        finally:
            probe_retired.set()

    monkeypatch.setattr(client, "list_tools", blocked_probe)
    independent_work = asyncio.create_task(release.wait())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.check_health(), 2)
        assert probe_retired.is_set() and not independent_work.done()
    finally:
        release.set()
        await independent_work
