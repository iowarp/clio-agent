"""Fork families: waiting on a handle waits on it and on every task forked from it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_forks import fork_family
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_observation_compatibility import forbidden_cancel


def _settle(owner: Any, handle: str, answer: str) -> None:
    task = owner.history.task_snapshot(handle)
    assert task is not None
    stamp = now()
    owner.history.record_snapshot(
        task.model_copy(
            update={
                "status": "completed",
                "terminal_at": stamp,
                "updated_at": stamp,
                "settlement": "confirmed",
                "result": owner.history.write_content(task.owner.session_id, {"answer": answer}),
            }
        ),
        observation_id=f"settle-{handle}",
    )


@pytest.mark.asyncio
async def test_a_wait_on_a_task_covers_a_fork_made_while_it_waits(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """The parent's wait on its child also waits on the child's fork, created after the
    wait began, and lists both outcomes by handle, the fork labelled with ``fork_of``."""

    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    child = await owner.spawn(caller, "Pursue H2", "worker", "child", start=False)
    operations = TaskOperations(owner.history, forbidden_cancel, app=app)
    try:
        waiting = asyncio.create_task(operations.wait(caller, child["handle"]))
        await asyncio.sleep(0.1)
        fork = await owner.spawn(
            caller, "Pursue H4 instead", "worker", "fork-1", start=False, fork_of=child["handle"]
        )
        forked = await asyncio.to_thread(owner.history.task_snapshot, fork["handle"])
        assert forked is not None and forked.fork_of == child["handle"]
        assert forked.owner.parent_handle == root.handle  # beside the original
        await asyncio.to_thread(_settle, owner, child["handle"], "H2 holds")
        await asyncio.sleep(0.2)
        assert not waiting.done()  # the fork is still running
        await asyncio.to_thread(_settle, owner, fork["handle"], "H4 holds")
        waited = await asyncio.wait_for(waiting, 5)
        assert waited["pending"] == []
        rows = {row["handle"]: row for row in waited["outcomes"]}
        assert waited["outcomes"][0]["handle"] == child["handle"]
        assert rows[fork["handle"]]["fork_of"] == child["handle"]
        assert rows[fork["handle"]]["result"]["answer"] == "H4 holds"
        assert "fork_of" not in rows[child["handle"]]
    finally:
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_a_family_is_ordered_by_creation_and_survives_reload(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """A fork of a fork belongs to the original's family; the family is rebuilt from
    the event log, and a fork's origin cannot be rebound under its handle."""

    _app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    child = await owner.spawn(caller, "Step", "worker", "step", start=False)
    first = await owner.spawn(
        caller, "First fork", "worker", "f1", start=False, fork_of=child["handle"]
    )
    second = await owner.spawn(
        caller, "Fork of the fork", "worker", "f2", start=False, fork_of=first["handle"]
    )
    family = await asyncio.to_thread(fork_family, owner.history, child["handle"])
    assert family == (child["handle"], first["handle"], second["handle"])

    reloaded = TaskHistory(arc)
    assert await asyncio.to_thread(fork_family, reloaded, child["handle"]) == family

    task = await asyncio.to_thread(owner.history.task_snapshot, second["handle"])
    with pytest.raises(ValueError, match="identity cannot change"):
        await asyncio.to_thread(
            owner.history.record_snapshot,
            task.model_copy(update={"fork_of": child["handle"]}),
            observation_id="rebind",
        )
    with pytest.raises(ValueError, match="reused with different work"):
        await owner.spawn(
            caller, "Fork of the fork", "worker", "f2", start=False, fork_of=child["handle"]
        )
    await owner.recovery.drain()
