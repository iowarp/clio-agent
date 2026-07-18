"""S4 (#948 / #952): spawn-runtime tools for react mains.

The routing surface that replaces the inline delegate_to_<child> / fanout tools +
the next_expert settle loop. A react main with declared children gets
spawn_agent_task / wait_agent_tasks / check_agent_tasks / spawn_agents_parallel;
a leaf (no children) gets none.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context


class _Agent:
    def forward(self, question: str, session_id: str):
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


class _Def:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.metadata = {"agent_blueprint_id": "bp"}


def _tool_names(app, agent_id: str, declared: set[str], monkeypatch) -> list[str]:
    from clio_agent.gact.agents import spawn_runtime

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": set(declared),
    )
    with _gact_app_context(app), _tool_session_context("sess_x"):
        tools = spawn_runtime.build_spawn_runtime_tools(_Agent(), _Def(agent_id))
    return [getattr(t, "name", "") for t in tools]


def test_react_main_with_children_gets_spawn_tools(tmp_path: Path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        names = _tool_names(app, "main", {"data_expert", "hpc_expert"}, monkeypatch)
        assert set(names) == {
            "spawn_agent_task",
            "wait_agent_tasks",
            "check_agent_tasks",
            "spawn_agents_parallel",
        }


def test_leaf_expert_without_children_gets_no_spawn_tools(tmp_path: Path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        assert _tool_names(app, "leaf_expert", set(), monkeypatch) == []
