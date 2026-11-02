"""The ablation switches: deferral off lists every tool; withheld tools are not built.

The evaluation turns one feature off at a time (plan, "Design: evaluation"): deferral
(``tools.deferral``), ``run_code`` and the working-state tools (``tools.withheld``).
Each switch records a typed reason, never silently.
"""

from __future__ import annotations

import pytest

from clio_agent.gact.agents.deferred_tools import SEARCH_TOOLS, plan_disclosure
from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.types import AgentDef
from tests.test_gact.test_tool_disclosure import _resolution, _tools


def test_deferral_off_lists_a_declared_deferred_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: drop the ``deferral_enabled`` check and the home tools are deferred."""
    tools = _tools(SEARCH_TOOLS, "home_light_set", "home_light_get")
    resolution = _resolution(home={"defer": True, "plugin": "core"})
    assert plan_disclosure(resolution, tools).deferred  # on by default
    monkeypatch.setenv("CLIO_TOOLS_DEFERRAL", "false")
    assert plan_disclosure(resolution, tools).deferred == frozenset()


def test_a_withheld_tool_is_not_built_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: skip ``configured_withheld`` and write_todos is built."""
    agent = AgentDef(id="main", title="Main", tools=["view_image", "write_todos"])
    monkeypatch.setenv("CLIO_TOOLS_WITHHELD", "write_todos")
    available, _gateway, withheld = build_native_tools(
        agent, agent.tools, {}, supports_vision=True
    )
    assert list(available) == ["view_image"]
    assert withheld == {"write_todos": "withheld_by_config"}
