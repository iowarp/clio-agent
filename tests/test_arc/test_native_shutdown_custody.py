"""Shutdown cancels only the native work this process took custody of (B2, R1).

Open work in a conversation the obligations lane has not recovered yet stays open in
clio-core and is recovered at the next start, as after a crash. Shutdown must not
read every open task to find it.
"""

from __future__ import annotations

import asyncio

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    BackendReference,
    SubmissionContext,
    TaskOwner,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.native_tools import NativeTools
from clio_agent.gact.native_work import now
from clio_agent.gact.task_history import TaskHistory


async def _owner_with_submitted_shell(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> tuple[NativeTools, list[str]]:
    for root_kind in ("SHELL", "STORAGE"):
        monkeypatch.setenv(f"CLIO_TASKS_{root_kind}_URL", "memory://")
        monkeypatch.setenv(f"CLIO_TASKS_{root_kind}_QUEUE", "")
    history = TaskHistory(arc)
    owner = TaskOwner(human_id="human", session_id="owner", assignment_id="assignment_owner")
    context = SubmissionContext(
        reference="internal",
        handle="task_shell",
        owner=owner,
        invocation_id="command",
        tool="bash",
        argument_digest=argument_digest({}),
        arguments={},
        workspace="workspace",
        permissions={},
        confinement={},
        attribution={},
        execution_limits={},
        backend=BackendReference(owner="native", server="shell", session="sdk-session"),
    )
    reference = await asyncio.to_thread(
        history.write_content, "owner", context.model_dump(mode="json")
    )
    stamp = now()
    await asyncio.to_thread(
        history.record_snapshot,
        TaskSnapshot(
            handle="task_shell",
            kind="Shell",
            owner=owner,
            label="Command",
            tool="bash",
            invocation_id="command",
            created_at=stamp,
            updated_at=stamp,
            submission=reference,
            activity="submitting",
        ),
        observation_id="submitted",
    )
    native = NativeTools(
        history, lambda _submission: None, CoreRecovery(history), admission=lambda _task: None
    )
    cancelled: list[str] = []

    async def cancel(handle: str) -> dict[str, bool]:
        cancelled.append(handle)
        return {"cancellation_requested": True}

    monkeypatch.setattr(native, "cancel", cancel)
    return native, cancelled


@pytest.mark.asyncio
async def test_shutdown_leaves_open_work_it_never_took_custody_of(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, cancelled = await _owner_with_submitted_shell(arc, monkeypatch)
    await native.request_shutdown()
    assert cancelled == []
    state = await asyncio.to_thread(native.history.task_snapshot, "task_shell")
    assert state is not None and state.terminal_at is None


@pytest.mark.asyncio
async def test_shutdown_cancels_submitted_work_in_its_custody(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, cancelled = await _owner_with_submitted_shell(arc, monkeypatch)
    native._custody.add("task_shell")
    await native.request_shutdown()
    assert cancelled == ["task_shell"]
