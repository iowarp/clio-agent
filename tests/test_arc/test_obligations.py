"""The obligations lane: only conversations with open work wait, and they resume in order.

Startup holds exactly the conversations named by open records, recovers them in the
background oldest first (a conversation someone is waiting on first), and releases them
to run their owed work no more than ``concurrency`` at a time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import TaskOwner, TaskSnapshot
from clio_agent.gact.conversation_recovery import open_work_sessions
from clio_agent.gact.native_work import now
from clio_agent.gact.obligations import Obligations
from tests.test_arc.test_conversation_owner_contracts import create_environment


def _open_assignment(owner: Any, label: str) -> str:
    """A second conversation with an open assignment, recorded like the first."""
    session = owner.sessions.create(workspace_id="workspace", agent={"id": "main"})
    stamp = now()
    owner.history.record_snapshot(
        TaskSnapshot(
            handle=f"task_{label}",
            kind="Session",
            tool="conversation",
            owner=TaskOwner(human_id="human", session_id=session.id, assignment_id=f"task_{label}"),
            label=label,
            description=f"{label} assignment",
            description_missing=False,
            invocation_id=f"{label}-assignment",
            created_at=stamp,
            updated_at=stamp,
            status="active",
        ),
        observation_id=label,
    )
    return session.id


async def _environment(arc: ARCMemory, tmp_path: Path) -> tuple[Any, Any, str, str]:
    app, owner, root = await create_environment(arc, tmp_path)
    second = await asyncio.to_thread(_open_assignment, owner, "second")
    owner._restoring = True  # as at boot, before the lane holds
    return app, owner, root.owner.session_id, second


@pytest.mark.asyncio
async def test_only_conversations_with_open_work_are_held(arc: ARCMemory, tmp_path: Path) -> None:
    _app, owner, first, second = await _environment(arc, tmp_path)
    idle = await asyncio.to_thread(
        lambda: owner.sessions.create(workspace_id="workspace", agent={"id": "main"}).id
    )
    try:
        lane = Obligations(owner)
        await lane.hold()
        assert owner._recovering == {first, second}
        assert idle not in owner._recovering
        assert owner._restoring is False  # everyone else is admitted at once
        assert owner.obligations is lane
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_open_work_is_listed_from_records_without_loading_conversations(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, owner, first, second = await _environment(arc, tmp_path)
    try:

        def no_tree(session_id: str) -> Any:
            raise AssertionError(f"loaded the tree of {session_id}")

        monkeypatch.setattr(owner.history, "_session_state", no_tree)
        listed = await asyncio.to_thread(open_work_sessions, owner.history)
        assert listed == [first, second]  # oldest first
    finally:
        monkeypatch.undo()
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_a_conversation_someone_waits_on_is_recovered_first(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, owner, first, second = await _environment(arc, tmp_path)
    recovered: list[str] = []

    async def restore_session(session_id: str) -> None:
        recovered.append(session_id)

    monkeypatch.setattr(owner, "restore_session", restore_session)
    try:
        lane = Obligations(owner)
        await lane.hold()
        lane.wanted(second)
        await asyncio.sleep(0)  # the hint crosses to the owner loop
        await lane.run()
        assert recovered == [second, first]
        assert owner._recovering == set()
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_owed_work_resumes_within_the_concurrency_bound(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, owner, first, second = await _environment(arc, tmp_path)
    running, peak, resumed = 0, 0, []

    async def restore_session(session_id: str) -> None:
        return None

    async def run_pending(session_id: str) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        assert session_id not in owner._recovering  # released before it runs
        await asyncio.sleep(0.05)
        resumed.append(session_id)
        running -= 1

    monkeypatch.setattr(owner, "restore_session", restore_session)
    monkeypatch.setattr(owner.history, "pending_mail", lambda session_id: ("owed",))
    monkeypatch.setattr(owner.runner, "run_pending", run_pending)
    try:
        lane = Obligations(owner, concurrency=1)
        await lane.hold()
        await lane.run()
        assert sorted(resumed) == sorted([first, second])
        assert peak == 1
    finally:
        monkeypatch.undo()
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_the_real_recovery_runs_per_conversation(arc: ARCMemory, tmp_path: Path) -> None:
    """End to end on real records: each held conversation is recovered and released."""
    _app, owner, first, second = await _environment(arc, tmp_path)
    try:
        lane = Obligations(owner)
        await lane.hold()
        await lane.run()
        assert lane.recovered == 2
        assert owner._recovering == set()
        assert lane.snapshot() == {"recovered": 2, "queued": 0, "running": 0}
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
