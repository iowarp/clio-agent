"""Lazy blueprint-fleet mounting (#1232 pt 1).

Blueprint AGENT.md ``mcp_servers`` must mount on blueprint ACTIVATION
(per-session/workspace), never into the boot-time default gateway:

* an installed-but-inactive blueprint's declared server does NOT appear in
  the boot gateway's specs;
* activating (passing) a blueprint id mounts exactly that blueprint's
  servers, never a DIFFERENT installed blueprint's;
* built-in defaults (fs/shell) are unaffected either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.gact.agent_blueprints import AgentBlueprintDefinition
from clio_agent.tools.execution import get_active_tool_blueprint_id, tool_blueprint_context
from clio_agent.tools.gateway import namespace_specs


def _blueprint(bp_id: str, servers: dict[str, str]) -> AgentBlueprintDefinition:
    root = Path(f"/fake/{bp_id}")
    return AgentBlueprintDefinition(
        id=bp_id,
        version="1.0",
        title=bp_id,
        display_name=bp_id,
        description="",
        scope="user",
        root=root,
        root_path=root,
        metadata={"mcp_servers": servers},
    )


@pytest.fixture
def agent():
    a = ClioAgent()
    try:
        yield a
    finally:
        a.shutdown()


class TestDiscoverPackServers:
    def test_empty_blueprint_id_returns_no_pack_servers(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SABOTAGE: revert to unconditional discovery and this returns the heavy pack too."""

        def _fake_discover():
            return [_blueprint("heavy-pack", {"heavy": "uvx heavy-science-mcp serve"})]

        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", _fake_discover
        )
        assert agent._discover_pack_servers("") == {}
        assert agent._discover_pack_servers() == {}

    def test_active_blueprint_id_returns_only_that_blueprints_servers(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_discover():
            return [
                _blueprint("pack-a", {"a-server": "uvx a-mcp serve"}),
                _blueprint("pack-b", {"b-server": "uvx b-mcp serve"}),
            ]

        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", _fake_discover
        )
        servers = agent._discover_pack_servers("pack-a")
        assert set(servers) == {"pack-a"}
        assert "a-server" in servers["pack-a"]
        # The OTHER installed blueprint's servers never leak in.
        assert "pack-b" not in servers

    def test_unknown_blueprint_id_returns_nothing(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints",
            lambda: [_blueprint("pack-a", {"a-server": "uvx a-mcp serve"})],
        )
        assert agent._discover_pack_servers("not-installed") == {}

    def test_discovery_failure_degrades_to_no_pack_servers(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom():
            raise RuntimeError("discovery unavailable")

        monkeypatch.setattr("clio_agent.gact.agent_blueprints.discover_agent_blueprints", _boom)
        assert agent._discover_pack_servers("pack-a") == {}


class TestBootGatewayNeverMountsPackServers:
    def test_boot_gateway_excludes_an_installed_but_inactive_blueprints_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The acceptance test verbatim: an installed blueprint declaring a
        server does NOT appear in the boot gateway."""

        def _fake_discover():
            return [_blueprint("heavy-pack", {"heavy": "uvx heavy-science-mcp serve"})]

        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", _fake_discover
        )
        agent = ClioAgent()
        try:
            specs = namespace_specs(agent._tool_gateway)
            assert "heavy" not in specs
        finally:
            agent.shutdown()


class TestToolBlueprintContext:
    def test_default_is_empty(self) -> None:
        assert get_active_tool_blueprint_id() == ""

    def test_bind_and_reset(self) -> None:
        with tool_blueprint_context("my-blueprint"):
            assert get_active_tool_blueprint_id() == "my-blueprint"
        assert get_active_tool_blueprint_id() == ""

    def test_none_binds_empty(self) -> None:
        with tool_blueprint_context(None):
            assert get_active_tool_blueprint_id() == ""


class TestNamespaceDiscoveryHealerLifecycle:
    """#1232 pt 2: a stale healer never leaks across a default-gateway rebuild.

    ``gact/relay_wiring.py::_refresh_agent_relay_tool_surfaces`` calls
    ``agent._build_tool_gateway(set_catalog=True)`` on every periodic relay
    catalog refresh (``relay.tool_surfaces_ttl_seconds``) -- a thread leaked
    per refresh would accumulate one daemon thread every few minutes forever
    on a long-running serve.
    """

    def test_rebuilding_the_default_gateway_retires_the_stale_healer(
        self, agent: ClioAgent
    ) -> None:
        first_healer = agent._mcp_namespace_healer
        assert first_healer is not None
        assert first_healer._thread.is_alive()

        agent._tool_gateway = agent._build_tool_gateway(set_catalog=True)

        second_healer = agent._mcp_namespace_healer
        assert second_healer is not first_healer
        # request_stop() is non-blocking; give the stale thread a moment to
        # actually exit (it wakes near-instantly on Event.set()).
        first_healer._thread.join(timeout=5.0)
        assert not first_healer._thread.is_alive(), "stale healer thread leaked"


class TestActiveToolExecutorBlueprintScoping:
    def test_default_executor_used_with_no_active_workspace(self, agent: ClioAgent) -> None:
        assert agent._active_tool_executor() is agent.tool_executor

    def test_workspace_gateway_mounts_only_the_active_blueprint(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Activating a blueprint in a session mounts it (per-workspace)."""

        def _fake_discover():
            return [
                _blueprint("pack-a", {"a-server": "uvx a-mcp serve"}),
                _blueprint("pack-b", {"b-server": "uvx b-mcp serve"}),
            ]

        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", _fake_discover
        )
        # Bounded discovery would otherwise try to spawn "uvx a-mcp" for real;
        # stub it out so this stays a fast, hermetic unit test of the MOUNT
        # decision (which specs land on the gateway), not live connectivity.
        monkeypatch.setattr(
            "clio_agent.agent.discover_declared_tools_bounded",
            lambda specs, **_kw: type(
                "R", (), {"tools": {}, "degraded": dict.fromkeys(specs, "unreachable")}
            )(),
        )
        from clio_agent.tools.execution import tool_workspace_context

        root = str(tmp_path)
        with tool_workspace_context(root), tool_blueprint_context("pack-a"):
            executor = agent._active_tool_executor()
        lock, executors, _leases = agent._workspace_state()
        with lock:
            gateway_key = executors.get(root)
        assert gateway_key is executor
        assert getattr(executor, "_clio_mounted_blueprint_id", None) == "pack-a"

    def test_blueprint_switch_evicts_and_rebuilds(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _fake_discover():
            return [
                _blueprint("pack-a", {"a-server": "uvx a-mcp serve"}),
                _blueprint("pack-b", {"b-server": "uvx b-mcp serve"}),
            ]

        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", _fake_discover
        )
        monkeypatch.setattr(
            "clio_agent.agent.discover_declared_tools_bounded",
            lambda specs, **_kw: type(
                "R", (), {"tools": {}, "degraded": dict.fromkeys(specs, "unreachable")}
            )(),
        )
        from clio_agent.tools.execution import tool_workspace_context

        root = str(tmp_path)
        with tool_workspace_context(root), tool_blueprint_context("pack-a"):
            first = agent._active_tool_executor()
        with tool_workspace_context(root), tool_blueprint_context("pack-b"):
            second = agent._active_tool_executor()

        assert first is not second
        assert getattr(second, "_clio_mounted_blueprint_id", None) == "pack-b"
        assert getattr(first, "closed", False) is True
