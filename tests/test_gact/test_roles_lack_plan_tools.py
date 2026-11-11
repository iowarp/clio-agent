"""The default read-only roles (``explore``, ``review``, ``planner``) never get the plan tools (TD20).

A role runs as a read-only session (``effects="read"``;
``tests/test_plugins/test_resolve.py::test_a_role_derives_the_callers_plugins_and_runs_read_only``),
whose posture keeps only native tools declared read-only. The plan and todo tools write
working state, so they are built undeclared and the posture withholds them.
"""

from __future__ import annotations

import pytest

from clio_agent.gact.agents import session_tool_posture
from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.types import AgentDef

PLAN_TOOLS = ["plan_enter", "plan_update", "plan_exit", "write_todos"]


def test_a_read_only_session_is_given_no_plan_or_todo_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: keep every tool in the posture filter and the plan tools reach the role."""
    agent = AgentDef(id="explore", title="Explore", tools=[*PLAN_TOOLS, "view_image"])
    available, _gateway, _withheld = build_native_tools(
        agent, agent.tools, {}, supports_vision=True
    )
    assert set(PLAN_TOOLS) <= set(available)  # they exist for a writing agent

    from clio_agent.gact import side_sessions

    monkeypatch.setattr(side_sessions, "is_read_only_side_session", lambda session: True)
    monkeypatch.setattr(session_tool_posture, "_active_session", lambda: object())
    kept = session_tool_posture.restrict_tools_to_session_posture(available.values())
    names = {getattr(tool, "name", "") for tool in kept}
    assert not names & set(PLAN_TOOLS)


def test_a_read_only_session_is_not_offered_a_tool_its_gate_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shell_bash`` reaches the gate, which refuses every call in a read-only session
    (the catalog tags it not read-only): an explore child listed it and spent up to five
    calls learning that (prompt1 P4). A gate-routed tool the catalog reads as read-only,
    or does not know, stays for the gate to decide. SABOTAGE: drop ``_catalog_refuses``
    and shell_bash is offered."""
    from types import SimpleNamespace

    from clio_agent.gact import side_sessions
    from clio_agent.gact.agents.tool_instrumentation import TOOL_OBSERVED_ATTR

    def routed(name: str) -> SimpleNamespace:
        def func() -> None:
            return None

        setattr(func, TOOL_OBSERVED_ATTR, True)
        return SimpleNamespace(name=name, func=func)

    from clio_agent.runtime import sandbox

    monkeypatch.setattr(side_sessions, "is_read_only_side_session", lambda session: True)
    monkeypatch.setattr(session_tool_posture, "_active_session", lambda: object())
    tools = [routed("shell_bash"), routed("fs_write_file"), routed("fs_read_file"),
             routed("ext_lookup")]  # fmt: skip
    monkeypatch.setattr(sandbox, "fence_enforced", lambda: False)  # an unfenced host
    kept = session_tool_posture.restrict_tools_to_session_posture(tools)
    assert [tool.name for tool in kept] == ["fs_read_file", "ext_lookup"]
    # Fenced, the shell is offered (it writes only scratch and temp); writes are not.
    monkeypatch.setattr(sandbox, "fence_enforced", lambda: True)
    kept = session_tool_posture.restrict_tools_to_session_posture(tools)
    assert [tool.name for tool in kept] == ["shell_bash", "fs_read_file", "ext_lookup"]
