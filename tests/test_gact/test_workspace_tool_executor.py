"""Per-active-workspace tool executor: stdio MCPs spawn with cwd=workspace root.

These tests pin the routing/caching contract of ``ClioAgent._active_tool_executor``
without constructing a full agent or spawning subprocesses: a no-op ``ClioAgent``
instance is created with ``__new__`` and only the per-workspace fields are set.
"""

from __future__ import annotations

from clio_agent.agent import ClioAgent
from clio_agent.tools.execution import tool_workspace_context


def _bare_agent() -> ClioAgent:
    """A ClioAgent shell with only the per-workspace executor fields populated."""
    agent = ClioAgent.__new__(ClioAgent)
    agent.tool_executor = object()  # sentinel default executor
    agent._workspace_tool_executors = {}
    return agent


def test_no_workspace_uses_default_executor() -> None:
    agent = _bare_agent()
    # No active workspace bound -> default executor (current behavior).
    assert agent._active_tool_executor() is agent.tool_executor


def test_active_workspace_builds_and_caches_per_root(monkeypatch) -> None:
    agent = _bare_agent()
    built: list[tuple[str | None, bool]] = []

    def fake_build(*, cwd=None, set_catalog=False):
        built.append((cwd, set_catalog))
        return f"gateway:{cwd}"

    def fake_create(gateway):
        return f"executor:{gateway}"

    monkeypatch.setattr(agent, "_build_tool_gateway", fake_build)
    monkeypatch.setattr("clio_agent.agent.create_sync_tool_executor", fake_create)

    with tool_workspace_context("/ws/alpha"):
        first = agent._active_tool_executor()
        second = agent._active_tool_executor()  # same workspace -> cached

    assert first == "executor:gateway:/ws/alpha"
    assert second is first
    # Built exactly once for this workspace, with cwd=root and no catalog reset.
    assert built == [("/ws/alpha", False)]
    assert set(agent._workspace_tool_executors) == {"/ws/alpha"}

    # A different workspace spawns its own executor (its own stdio MCPs).
    with tool_workspace_context("/ws/beta"):
        other = agent._active_tool_executor()
    assert other == "executor:gateway:/ws/beta"
    assert built == [("/ws/alpha", False), ("/ws/beta", False)]
    assert set(agent._workspace_tool_executors) == {"/ws/alpha", "/ws/beta"}


def test_blank_workspace_root_falls_back_to_default(monkeypatch) -> None:
    agent = _bare_agent()

    def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("blank workspace must not build a gateway")

    monkeypatch.setattr(agent, "_build_tool_gateway", boom)

    with tool_workspace_context("   "):
        assert agent._active_tool_executor() is agent.tool_executor
