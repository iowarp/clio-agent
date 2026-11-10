"""Real Core and native SDK execution; no model or platform acceptance is implied."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp.utilities.tasks import TaskConfig

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import SubmissionContext, TaskOwner, TaskSnapshot
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.native_tools import NativeTools
from clio_agent.gact.native_work import WorkEvidence
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_operations import TaskOperations

STAMP = "2026-10-14T04:00:00Z"


def owning_assignment(history: Any) -> TaskOwner:
    """Create the actual parent record required by native submission custody."""
    owner = TaskOwner(human_id="human", session_id="owner", assignment_id="task_parent")
    history.record_snapshot(
        TaskSnapshot(
            handle="task_parent",
            kind="Session",
            owner=owner,
            label="Native boundary test",
            tool="conversation",
            invocation_id="human-assignment",
            created_at=STAMP,
            updated_at=STAMP,
            status="active",
        ),
        observation_id="parent",
    )
    return owner.model_copy(update={"parent_handle": owner.assignment_id})


def configure_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep native integration cases independent of the user's configured backend."""
    for kind in ("SHELL", "STORAGE"):
        monkeypatch.setenv(f"CLIO_TASKS_{kind}_URL", "memory://")
        monkeypatch.setenv(f"CLIO_TASKS_{kind}_QUEUE", "")
        monkeypatch.setenv(f"CLIO_TASKS_{kind}_CONCURRENCY", "1")


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real native roots and Core must settle before the case exits.
async def test_native_handle_and_storage_start_while_shell_root_is_occupied(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.task_history import TaskHistory

    configure_roots(monkeypatch)
    history = TaskHistory(arc)
    owner = await asyncio.to_thread(owning_assignment, history)
    recovery = CoreRecovery(history)
    entered, release, storage_started = (asyncio.Event() for _ in range(3))

    def admit(task: TaskSnapshot) -> None:
        if task.cancellation_requested_at or native.admission_closed(task.handle):
            raise ValueError("Admission closed")

    native = NativeTools(history, lambda _context: None, recovery, admission=admit)

    @native.roots["shell"].server.tool(name="controlled", task=TaskConfig(mode="required"))
    async def controlled(_clio_submission: str) -> dict[str, str]:
        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, str]:
            retained = await asyncio.to_thread(history.task_snapshot, context.handle)
            assert retained is not None and retained.submission is not None
            assert await asyncio.to_thread(
                history.read_content, retained.submission
            ) == context.model_dump(mode="json")
            evidence.acquiring_effects()
            entered.set()
            try:
                await release.wait()
                return {"value": "finished"}
            finally:
                evidence.confirm_cleanup()

        return await native.work["shell"].run(_clio_submission, "controlled", {}, body)

    @native.roots["storage"].server.tool(name="independent", task=TaskConfig(mode="required"))
    async def independent(_clio_submission: str) -> dict[str, str]:
        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, str]:
            storage_started.set()
            return {"value": "storage started"}

        return await native.work["storage"].run(_clio_submission, "independent", {}, body)

    async with native:
        try:
            receipt = await native.submit(
                "shell",
                "controlled",
                {},
                kind="Shell",
                owner=owner,
                invocation_id="shell-first",
                workspace=str(tmp_path),
                permissions={},
                confinement={},
                attribution={},
                execution_limits={},
                description="Hold one native worker",
            )
            await entered.wait()
            assert receipt["accepted"] is True
            running = await asyncio.to_thread(history.task_snapshot, receipt["handle"])
            assert running is not None and running.terminal_at is None
            assert running.activity == "executing"
            independent_receipt = await native.submit(
                "storage",
                "independent",
                {},
                kind="Indexing",
                owner=owner,
                invocation_id="storage-second",
                workspace=str(tmp_path),
                permissions={},
                confinement={},
                attribution={},
                execution_limits={},
                description="Independent storage work",
            )
            await storage_started.wait()
            independent_result = await native.await_settlement(independent_receipt["handle"])
            assert independent_result.status == "completed"
            assert not release.is_set()
            release.set()
            settled = await native.await_settlement(receipt["handle"])
            assert settled.status == "completed" and settled.settlement == "confirmed"
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # A native cancellation acknowledgement is not the cleanup receipt.
async def test_native_cancel_waits_for_body_cleanup(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.task_history import TaskHistory

    configure_roots(monkeypatch)
    history = TaskHistory(arc)
    owner = await asyncio.to_thread(owning_assignment, history)
    recovery = CoreRecovery(history)
    entered, cleaning, release, exited = (asyncio.Event() for _ in range(4))

    def admit(task: TaskSnapshot) -> None:
        if task.cancellation_requested_at or native.admission_closed(task.handle):
            raise ValueError("Admission closed")

    native = NativeTools(history, lambda _context: None, recovery, admission=admit)

    @native.roots["shell"].server.tool(name="cleanup", task=TaskConfig(mode="required"))
    async def cleanup(_clio_submission: str) -> dict[str, str]:
        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, str]:
            evidence.acquiring_effects()
            entered.set()
            try:
                await asyncio.Event().wait()
                return {"value": "unexpected"}
            finally:
                cleaning.set()
                await release.wait()
                evidence.confirm_cleanup()
                exited.set()

        return await native.work["shell"].run(_clio_submission, "cleanup", {}, body)

    async def cancel_owner(task: TaskSnapshot, caller: TaskCaller) -> dict[str, Any]:
        return await native.cancel(task.handle)

    operations = TaskOperations(history, cancel_owner)
    async with native:
        try:
            receipt = await native.submit(
                "shell",
                "cleanup",
                {},
                kind="Shell",
                owner=owner,
                invocation_id="cleanup-case",
                workspace=str(tmp_path),
                permissions={},
                confinement={},
                attribution={},
                execution_limits={},
                description="Await real cleanup",
            )
            await entered.wait()
            acknowledgement = await native.cancel(receipt["handle"])
            assert acknowledgement["cancellation_requested"] is True
            await cleaning.wait()
            snapshot = await operations.wait(
                TaskCaller("owner", "human"), receipt["handle"], timeout_s=0
            )
            assert snapshot["pending"] == [receipt["handle"]]
            assert not exited.is_set()
            release.set()
            settled = await native.await_settlement(receipt["handle"])
            assert exited.is_set()
            assert settled.status == "cancelled" and settled.settlement == "confirmed"
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real native roots and Core must settle before the case exits.
async def test_native_work_sizes_its_result_to_the_room_recorded_at_its_call(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native shell runs apart from the agent loop that called it, so it cannot measure
    the caller's context window; the room measured at the call is recorded in the
    submission's execution limits and pinned for the body. SABOTAGE: drop
    ``pinned_room(room)`` in ``NativeWork.run`` and the body measures an unknown window."""
    from clio_agent.gact.task_history import TaskHistory
    from clio_agent.tools.result_room import ResultRoom, result_room, room_limits

    configure_roots(monkeypatch)
    history = TaskHistory(arc)
    owner = await asyncio.to_thread(owning_assignment, history)
    native = NativeTools(
        history, lambda _context: None, CoreRecovery(history), admission=lambda _task: None
    )
    seen: list[ResultRoom] = []

    @native.roots["shell"].server.tool(name="sized", task=TaskConfig(mode="required"))
    async def sized(_clio_submission: str) -> dict[str, str]:
        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, str]:
            evidence.acquiring_effects()
            seen.append(result_room())
            evidence.confirm_cleanup()
            return {"value": "sized"}

        return await native.work["shell"].run(_clio_submission, "sized", {}, body)

    caller = ResultRoom(tokens=8_000, chars=32_000, reason="window_share", window=64_000)
    async with native:
        for invocation, limits in (("recorded", room_limits(caller)), ("unrecorded", {})):
            receipt = await native.submit(
                "shell",
                "sized",
                {},
                kind="Shell",
                owner=owner,
                invocation_id=invocation,
                workspace=str(tmp_path),
                permissions={},
                confinement={},
                attribution={},
                execution_limits=limits,
                description="Size a result",
            )
            settled = await native.await_settlement(receipt["handle"])
            assert settled.status == "completed"
    assert seen[0] == caller
    assert seen[1].reason == "tool_result_window_unknown"
