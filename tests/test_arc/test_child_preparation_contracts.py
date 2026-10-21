"""Target preparation cannot lock shared tasks or retarget accepted assignments."""

from __future__ import annotations

import asyncio
import threading
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionDefinition
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.spawn_context import prepare_child_session
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_replacement_contracts import forbid_cancel


def _resolved(agent: str, name: str) -> tuple[Any, dict[str, Any]]:
    """A child resolution as ``spawn_context.resolve_child_agent`` returns it."""

    return SimpleNamespace(agent={"id": agent, "name": name}), {"agent_ref": f"clio/{agent}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["none", "cancel", "configuration"])
async def test_held_target_discovery_preserves_shared_controls_and_rechecks_admission(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def resolve(_app: Any, _parent: str, agent: str) -> tuple[Any, dict[str, Any]]:
        calls.append(agent)
        entered.set()
        assert release.wait(10), "Test did not release its owned target preparation"
        return _resolved(agent, "Prepared worker")

    monkeypatch.setattr("clio_agent.gact.spawn_context.resolve_child_agent", resolve)
    owner.prepare_child = partial(prepare_child_session, app)
    submission = asyncio.create_task(
        owner.spawn(caller, "Original assignment", "worker", "held-target")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        operations = TaskOperations(owner.history, forbid_cancel)
        page = await asyncio.wait_for(asyncio.to_thread(operations.query, caller), 2)
        assert [row["handle"] for row in page["tasks"]] == [root.handle]
        commands = QuestionCommands(owner.history)
        question = await asyncio.wait_for(
            asyncio.to_thread(
                commands.create,
                root.owner,
                QuestionDefinition(prompt="Independent input", source="native"),
                invocation_id="during-discovery",
            ),
            2,
        )
        assert (
            await asyncio.wait_for(asyncio.to_thread(commands.cancel, question.handle), 2)
        ).status == "cancelled"
        assert not [row for row in app.state.sessions.list() if row.parent_session_id]
        if mutation == "cancel":
            acknowledgement = await asyncio.wait_for(owner.cancel(caller, root.handle), 2)
            assert acknowledgement["cancellation_requested"] is True
        elif mutation == "configuration":
            await asyncio.to_thread(
                app.state.sessions.update, caller.session_id, model={"provider": "changed"}
            )
        release.set()
        if mutation == "none":
            accepted = await asyncio.wait_for(submission, 5)
            assert accepted["accepted"] is True
            child = app.state.sessions.get(accepted["session_id"])
            assert child is not None and child.title == "Prepared worker"

            def unavailable(*_args: Any, **_kwargs: Any) -> Any:
                pytest.fail("An accepted identity must not rediscover or retarget its agent")

            monkeypatch.setattr("clio_agent.gact.spawn_context.resolve_child_agent", unavailable)
            assert (
                await owner.spawn(caller, "Original assignment", "worker", "held-target")
                == accepted
            )
            assert calls == ["worker"]
        else:
            with pytest.raises(
                ValueError,
                match="active owning assignment|admission is closed|configuration changed",
            ):
                await asyncio.wait_for(submission, 5)
            assert not [row for row in app.state.sessions.list() if row.parent_session_id]
            assert not await asyncio.to_thread(owner.history.task_handles, kind="Subagent")
    finally:
        release.set()
        if not submission.done():
            await asyncio.gather(submission, return_exceptions=True)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_identical_preparations_accept_one_child_without_executing_discovery_under_writer(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    release, entered = threading.Event(), threading.Event()
    lock, count = threading.Lock(), 0

    def resolve(_app: Any, _parent: str, agent: str) -> tuple[Any, dict[str, Any]]:
        nonlocal count
        with lock:
            count += 1
            if count == 2:
                entered.set()
        assert release.wait(10), "Test did not release both preparation owners"
        return _resolved(agent, "One child")

    monkeypatch.setattr("clio_agent.gact.spawn_context.resolve_child_agent", resolve)
    owner.prepare_child = partial(prepare_child_session, app)
    submissions = [
        asyncio.create_task(owner.spawn(caller, "Same work", "worker", "same")) for _ in range(2)
    ]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert not await asyncio.to_thread(owner.history.task_handles, kind="Subagent")
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(*submissions), 5)
        assert first == second
        children = [row for row in app.state.sessions.list() if row.parent_session_id]
        assert [row.id for row in children] == [first["session_id"]]
        assert len(await owner.history.io.run(owner.history.pending_mail, first["session_id"])) == 1
        assert children[0].creation_hook_state == "pending"
    finally:
        release.set()
        await asyncio.gather(*submissions, return_exceptions=True)
        await owner.shutdown()
        await owner.recovery.drain()
