"""Released spawn tools use one preparation path and the conversation owner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context
from clio_agent.gact.session_records import PageQuery
from clio_agent.gact.task_declarations import build_spawn_runtime_tools
from tests.test_arc.test_conversation_owner_contracts import create_environment


def record_child_preparation(owner: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every child agent the conversation owner is asked to prepare."""
    prepared: list[str] = []
    original = owner.prepare_child

    def prepare(parent: str, agent: str, scope: dict[str, Any] | None, model: str = "") -> Any:
        prepared.append(agent)
        return original(parent, agent, scope, model)

    monkeypatch.setattr(owner, "prepare_child", prepare)
    return prepared


@pytest.mark.asyncio
async def test_single_and_batch_spawn_share_child_preparation_and_preserve_names(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    prepared = record_child_preparation(owner, monkeypatch)
    tools = {
        tool.name: tool for tool in build_spawn_runtime_tools(None, SimpleNamespace(fanout={}))
    }
    schema = tools["spawn_agents_parallel"].format_as_litellm_function_call()["function"][
        "parameters"
    ]
    assert "spawns" in schema["properties"] and "assignments" not in schema["properties"]
    item = schema["properties"]["spawns"]["items"]
    assert {"agent", "task", "input_task_ids", "strategy"} <= item["properties"].keys()
    assert "blueprint_id" not in item["properties"]
    token, sid_token = context.set_app(app), context.set_session_id(root.owner.session_id)
    try:
        single = await tools["spawn_agent_task"].func(
            agent="writer", task="Write original", placement="local"
        )
        assert single["task_id"] == single["handle"]
        batch = await tools["spawn_agents_parallel"].func(
            spawns=[
                {"agent": "critic", "task": "Critique original"},
                {"agent": "critic", "task": "Invalid evidence", "input_task_ids": ["missing"]},
                {"agent": "", "task": "No target"},
                {"agent": "writer", "task": "Continue independent work"},
            ]
        )
        assert len(batch["spawned"]) == 4
        assert batch["spawned"] == batch["outcomes"]
        assert batch["outcomes"][1]["index"] == 1 and "error" in batch["outcomes"][1]
        assert batch["outcomes"][2]["index"] == 2
        assert "agent must name" in batch["outcomes"][2]["error"]
        assert batch["outcomes"][3]["accepted"] is True
        # Invalid evidence and an empty target fail before any child is prepared.
        assert prepared == ["writer", "critic", "writer"]
        children = {
            row["session_id"]: app.state.sessions.get(row["session_id"]).agent["id"]
            for row in (single, batch["outcomes"][0], batch["outcomes"][3])
        }
        assert sorted(children.values()) == ["critic", "writer", "writer"]
        snapshots = await asyncio.to_thread(owner.history.session_handles, root.owner.session_id)
        assert root.handle in snapshots
        assert all(row["task_id"] == row["handle"] for row in batch["spawned"] if "handle" in row)
        before = len(app.state.sessions.select(PageQuery(archived=None)))
        with pytest.raises(ValueError, match="relay placement"):
            await tools["spawn_agent_task"].func(agent="writer", task="Remote", placement="relay")
        assert len(app.state.sessions.select(PageQuery(archived=None))) == before
    finally:
        context.reset(sid_token)
        context.reset(token)
        await owner.recovery.drain()
