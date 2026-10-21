"""Released nullable spawn arguments cross the real short FastMCP boundary."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import SubmissionContext
from clio_agent.gact import context
from clio_agent.gact.builtin_boundary import BuiltinBoundary
from clio_agent.gact.conversations import Conversations
from clio_agent.gact.task_declarations import build_spawn_runtime_tools
from tests.test_arc.conftest import core_arc as core_arc
from tests.test_arc.test_conversation_owner_contracts import create_environment


@asynccontextmanager
async def _spawn_boundary(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[
    tuple[
        Conversations,
        BuiltinBoundary,
        dict[str, Any],
        contextvars.Context,
        list[str],
    ]
]:
    """Use actual Core, MCP custody and conversation acceptance; substitute child resolution."""
    app, owner, root = await create_environment(arc, tmp_path)
    app.state.mcp_app_loop = asyncio.get_running_loop()
    app.state.workspaces = SimpleNamespace(
        get=lambda _wid: SimpleNamespace(root_path=str(tmp_path))
    )
    resolved: list[str] = []
    prepare_child = owner.prepare_child

    def prepare(parent: str, agent: str, scope: dict[str, Any] | None) -> Any:
        assert parent == root.owner.session_id
        resolved.append(agent)
        return prepare_child(parent, agent, scope)

    def authorize(saved: SubmissionContext) -> None:
        assert saved.owner.assignment_id == root.handle
        current = owner.history.task_snapshot(root.handle)
        assert current is not None
        owner.require_admission(current)

    monkeypatch.setattr(owner, "prepare_child", prepare)
    tools = {
        tool.name: tool for tool in build_spawn_runtime_tools(None, SimpleNamespace(fanout={}))
    }
    app_token = context.set_app(app)
    sid_token = context.set_session_id(root.owner.session_id)
    try:
        async with BuiltinBoundary(app, authorize) as boundary:
            yield owner, boundary, tools, contextvars.copy_context(), resolved
    finally:
        context.reset(sid_token)
        context.reset(app_token)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "optional",
    [
        {"placement": None},
        {"input_task_ids": None},
        {"strategy": None},
        {"placement": None, "input_task_ids": None, "strategy": None},
    ],
    ids=["placement", "inputs", "strategy", "all"],
)
async def test_model_can_submit_nullable_spawn_options(
    arc: ARCMemory, tmp_path: Path, optional: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate public nullable input and confirm original assignment custody before inference."""
    async with _spawn_boundary(arc, tmp_path, monkeypatch) as (
        owner,
        boundary,
        tools,
        captured,
        resolved,
    ):
        result = await boundary.call(
            tools["spawn_agent_task"],
            {"agent": "data_expert", "task": "Inspect owned data", **optional},
            captured,
        )
        assert result["accepted"] is True and result["kind"] == "Subagent"
        assert result["task_id"] == result["handle"]
        current = await asyncio.to_thread(owner.history.task_snapshot, result["handle"])
        assert current is not None and current.description == "Inspect owned data"
        assert current.terminal_at is None and current.activity == "queued"
        child = owner.sessions.get(result["session_id"])
        assert child is not None and child.agent["id"] == "data_expert"
        assert child.creation_hook_state == "pending"
        assert resolved == ["data_expert"]
        assert await asyncio.to_thread(owner.history.task_handles, kind="Subagent") == {
            current.handle
        }


@pytest.mark.asyncio
async def test_model_can_inherit_batch_placement(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch's null placement uses the same owner and independently stored handles."""
    async with _spawn_boundary(arc, tmp_path, monkeypatch) as (
        owner,
        boundary,
        tools,
        captured,
        resolved,
    ):
        result = await boundary.call(
            tools["spawn_agents_parallel"],
            {"spawns": [{"agent": "data_expert", "task": "Inspect owned data"}], "placement": None},
            captured,
        )
        assert result["outcomes"] == result["spawned"]
        child = result["spawned"][0]
        assert child["accepted"] is True and child["task_id"] == child["handle"]
        current = await asyncio.to_thread(owner.history.task_snapshot, child["handle"])
        assert current is not None and current.description == "Inspect owned data"
        assert resolved == ["data_expert"]
        assert len(await asyncio.to_thread(owner.history.task_handles, kind="Subagent")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [{"task": None}, {"agent": 1}, {"input_task_ids": "task_x"}, {"strategy": "best_of_n"}],
    ids=["task", "agent", "inputs", "strategy"],
)
async def test_invalid_spawn_arguments_are_rejected_before_submission(
    arc: ARCMemory, tmp_path: Path, invalid: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual model-facing schema rejects invalid types before target discovery or acceptance."""
    async with _spawn_boundary(arc, tmp_path, monkeypatch) as (
        owner,
        boundary,
        tools,
        captured,
        resolved,
    ):
        with pytest.raises(ValidationError):
            await boundary.call(
                tools["spawn_agent_task"],
                {"agent": "data_expert", "task": "Inspect owned data", **invalid},
                captured,
            )
        assert resolved == []
        assert not await asyncio.to_thread(owner.history.task_handles, kind="Subagent")


@pytest.mark.asyncio
async def test_empty_placement_remains_an_unaccepted_error(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty placement does not become an accepted operation or inherit the null default."""
    async with _spawn_boundary(arc, tmp_path, monkeypatch) as (
        owner,
        boundary,
        tools,
        captured,
        resolved,
    ):
        with pytest.raises(ValueError, match="placement must"):
            await boundary.call(
                tools["spawn_agent_task"],
                {"agent": "data_expert", "task": "Inspect owned data", "placement": ""},
                captured,
            )
        assert resolved == []
        assert not await asyncio.to_thread(owner.history.task_handles, kind="Subagent")
