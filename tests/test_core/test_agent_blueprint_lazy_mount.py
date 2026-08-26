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
from types import SimpleNamespace

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.gact.agent_blueprints import AgentBlueprintDefinition
from clio_agent.gact.blueprint_activation import blueprint_resolution_reasons
from clio_agent.tools.execution import (
    get_active_tool_blueprint_id,
    get_active_tool_blueprint_path,
    tool_blueprint_context,
)
from clio_agent.tools.gateway import namespace_specs


def _blueprint(bp_id: str, servers: dict[str, object]) -> AgentBlueprintDefinition:
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

        def _fake_discover(**_kwargs: object):
            return [_blueprint("heavy-pack", {"heavy": "uvx heavy-science-mcp serve"})]

        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", _fake_discover
        )
        assert agent._discover_pack_servers("") == {}
        assert agent._discover_pack_servers() == {}

    def test_active_blueprint_id_returns_only_that_blueprints_servers(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_discover(**_kwargs: object):
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

    def test_installed_blueprint_checksum_invalidates_mcp_listing_cache(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blueprint = _blueprint(
            "spotter-ai",
            {
                "spotter": {
                    "command": "uv",
                    "args": ["run", "spotter-mcp"],
                    "env": {"EXISTING": "preserved"},
                }
            },
        )
        blueprint.metadata["install"] = {"checksum": "pack-checksum-v2"}
        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", lambda: [blueprint]
        )

        servers = agent._discover_pack_servers("spotter-ai")

        spec = servers["spotter-ai"]["spotter"]
        assert spec["env"] == {
            "EXISTING": "preserved",
            "CLIO_BLUEPRINT_INSTALL_CHECKSUM": "pack-checksum-v2",
        }

    def test_unknown_blueprint_id_returns_nothing(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints",
            lambda: [_blueprint("pack-a", {"a-server": "uvx a-mcp serve"})],
        )
        assert agent._discover_pack_servers("not-installed") == {}

    def test_workspace_blueprint_is_discovered_from_gateway_cwd(
        self, agent: ClioAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The activated workspace copy, not the server process cwd, owns MCPs."""

        root = tmp_path / ".clio" / "agent-blueprints" / "workspace-pack"
        root.mkdir(parents=True)
        (root / "AGENT.md").write_text(
            "---\nid: workspace-pack\ntitle: Workspace\n"
            "mcp_servers:\n  geo: clio-kit mcp-server geo\n---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprint_refresh.ensure_default_registry_bootstrap",
            lambda **_kwargs: "",
        )

        servers = agent._discover_pack_servers("workspace-pack", cwd=str(tmp_path))

        assert servers == {"workspace-pack": {"geo": "clio-kit mcp-server geo"}}

    def test_discovery_failure_degrades_to_no_pack_servers(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(**_kwargs: object):
            raise RuntimeError("discovery unavailable")

        monkeypatch.setattr("clio_agent.gact.agent_blueprints.discover_agent_blueprints", _boom)
        app = SimpleNamespace(state=SimpleNamespace(sessions={}))
        from clio_agent.gact import context as gact_context

        app_token = gact_context.set_app(app)
        session_token = gact_context.set_session_id("session-1")
        try:
            assert agent._discover_pack_servers("pack-a") == {}
        finally:
            gact_context.reset(session_token)
            gact_context.reset(app_token)

        assert blueprint_resolution_reasons(app, "session-1") == [
            {
                "reason": "installed_blueprint_discovery_failed",
                "category": "capability_unavailable",
                "description": "Installed Agent Blueprint discovery failed.",
                "blueprint_id": "pack-a",
            }
        ]

    def test_explicit_session_path_precedes_cwd_discovery(
        self, agent: ClioAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The explicit session activation is authoritative over a cwd copy."""

        explicit_root = tmp_path / "explicit"
        explicit_root.mkdir()
        explicit_blueprint = explicit_root / "AGENT.md"
        explicit_blueprint.write_text(
            "---\nid: workspace-pack\ntitle: Explicit\n"
            "mcp_servers:\n  explicit: clio-kit mcp-server explicit\n---\n",
            encoding="utf-8",
        )
        cwd_root = tmp_path / "workspace" / ".clio" / "agent-blueprints" / "workspace-pack"
        cwd_root.mkdir(parents=True)
        (cwd_root / "AGENT.md").write_text(
            "---\nid: workspace-pack\ntitle: Workspace\n"
            "mcp_servers:\n  cwd: clio-kit mcp-server cwd\n---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprint_refresh.ensure_default_registry_bootstrap",
            lambda **_kwargs: "",
        )

        with tool_blueprint_context("workspace-pack", explicit_blueprint):
            servers = agent._discover_pack_servers(
                "workspace-pack", cwd=str(tmp_path / "workspace")
            )

        assert servers == {"workspace-pack": {"explicit": "clio-kit mcp-server explicit"}}

    def test_path_activated_blueprint_exposes_its_declared_server(
        self, agent: ClioAgent, tmp_path: Path
    ) -> None:
        blueprint = tmp_path / "AGENT.md"
        blueprint.write_text(
            """---
id: spotter-ai
title: Spotter
version: 1.0.0
root_expert: watcher
blueprint:
  format: agent-blueprint-v1
mcp_servers:
  spotter:
    command: uv
    args: [run, spotter-mcp]
experts:
  - watcher.md
---
""",
            encoding="utf-8",
        )
        (tmp_path / "watcher.md").write_text(
            """---
id: watcher
title: Watcher
tier: 1
module:
  kind: react
signature:
  inputs:
    question: {type: string}
  outputs:
    answer: {type: string}
tools: [spotter_capabilities]
---
""",
            encoding="utf-8",
        )

        with tool_blueprint_context("spotter-ai", blueprint):
            servers = agent._discover_pack_servers("spotter-ai")

        assert set(servers["spotter-ai"]) == {"spotter"}

    def test_session_metadata_recovers_path_when_tool_context_has_no_path(
        self, agent: ClioAgent, tmp_path: Path
    ) -> None:
        blueprint = tmp_path / "AGENT.md"
        blueprint.write_text(
            """---
id: spotter-ai
title: Spotter
version: 1.0.0
root_expert: watcher
blueprint: {format: agent-blueprint-v1}
mcp_servers:
  spotter: uv run spotter-mcp
experts: [watcher.md]
---
""",
            encoding="utf-8",
        )
        session = SimpleNamespace(metadata={"active_agent_blueprint_path": str(blueprint)})
        app = SimpleNamespace(state=SimpleNamespace(sessions={"session-1": session}))
        from clio_agent.gact import context as gact_context

        app_token = gact_context.set_app(app)
        session_token = gact_context.set_session_id("session-1")
        try:
            with tool_blueprint_context("spotter-ai"):
                servers = agent._discover_pack_servers("spotter-ai")
        finally:
            gact_context.reset(session_token)
            gact_context.reset(app_token)

        assert set(servers["spotter-ai"]) == {"spotter"}


class TestBootGatewayNeverMountsPackServers:
    def test_boot_gateway_excludes_an_installed_but_inactive_blueprints_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The acceptance test verbatim: an installed blueprint declaring a
        server does NOT appear in the boot gateway."""

        def _fake_discover(**_kwargs: object):
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
        with tool_blueprint_context("my-blueprint", "/packs/my-blueprint/AGENT.md"):
            assert get_active_tool_blueprint_id() == "my-blueprint"
            assert get_active_tool_blueprint_path() == "/packs/my-blueprint/AGENT.md"
        assert get_active_tool_blueprint_id() == ""
        assert get_active_tool_blueprint_path() == ""

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

    def test_blueprint_activation_mounts_zero_mcp_servers(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#1237 Gap 1 (owner ruling 2026-08-20): blueprint activation must NOT
        cold-spawn any declared server. Pre-fix, _active_tool_executor's first
        resolve ran discover_declared_tools_bounded synchronously over every
        declared namespace; this asserts that call is GONE from the resolve
        path entirely -- a spy on it must never fire, and the resident
        executor's namespace-spec map (the on-demand-mount seam) must still
        carry both declared namespaces even though neither was ever listed."""

        def _fake_discover(**_kwargs: object):
            return [
                _blueprint("pack-a", {"geo": "uvx geo-mcp serve", "pandas": "uvx pandas-mcp serve"})
            ]

        monkeypatch.setattr(
            "clio_agent.gact.agent_blueprints.discover_agent_blueprints", _fake_discover
        )
        spy_calls: list[object] = []
        monkeypatch.setattr(
            "clio_agent.agent.discover_declared_tools_bounded",
            lambda specs, **_kw: spy_calls.append(specs)
            or type("R", (), {"tools": {}, "degraded": {}})(),
        )
        monkeypatch.setattr("clio_agent.tools.listing_cache.load_listing", lambda *a, **kw: None)
        from clio_agent.tools.execution import tool_workspace_context

        root = str(tmp_path)
        with tool_workspace_context(root), tool_blueprint_context("pack-a"):
            executor = agent._active_tool_executor()

        assert spy_calls == [], (
            "discover_declared_tools_bounded fired at activation -- blueprint "
            "activation must mount NOTHING eagerly (#1237)"
        )
        declared_specs = getattr(executor, "_clio_namespace_specs", {})
        assert set(declared_specs) == {"geo", "pandas"}
        # Neither namespace's tools were preloaded (no cache hit, no live pass).
        names = executor.get_tool_names()
        assert not any(n.startswith("geo_") for n in names)
        assert not any(n.startswith("pandas_") for n in names)

    def test_workspace_gateway_mounts_only_the_active_blueprint(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Activating a blueprint in a session mounts it (per-workspace)."""

        def _fake_discover(**_kwargs: object):
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

    def test_second_blueprint_merges_into_resident_fleet_without_evicting(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The spotter regression (shared-root workload + watcher): a resolve
        under a SECOND blueprint must merge into the resident fleet, never
        close it — #1232's close-and-rebuild killed the other session's live
        turn (``SyncMCPToolExecutor is closed`` mid-campaign, 2026-08-21)."""

        def _fake_discover(**_kwargs: object):
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

        # SAME resident fleet, alive, carrying BOTH blueprints' namespaces.
        assert first is second
        assert getattr(first, "closed", False) is False
        specs = getattr(first, "_clio_namespace_specs", {})
        assert set(specs) == {"a-server", "b-server"}
        assert getattr(first, "_clio_mounted_blueprint_ids", set()) == {"pack-a", "pack-b"}
        # The async twin routes the merged namespace too (lazy proxy present).
        inner = getattr(first, "_async_executor", None)
        assert inner is not None
        assert "b-server" in inner._namespace_servers
        # Re-resolving under EITHER blueprint keeps the fleet (no thrash).
        with tool_workspace_context(root), tool_blueprint_context("pack-a"):
            third = agent._active_tool_executor()
        assert third is first
        assert getattr(first, "closed", False) is False

    def test_deactivated_blueprint_reuses_resident_fleet_without_evicting(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``blueprint_id == \"\"`` declares no servers: the resident fleet is
        reused as-is (reachability is the agent build's ACL, not residency)."""

        def _fake_discover(**_kwargs: object):
            return [_blueprint("pack-a", {"a-server": "uvx a-mcp serve"})]

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
        with tool_workspace_context(root), tool_blueprint_context(""):
            second = agent._active_tool_executor()

        assert second is first
        assert getattr(first, "closed", False) is False

    def test_namespace_collision_keeps_first_mounted_spec(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two blueprints declaring the SAME namespace with different commands:
        the first mounted spec wins; the conflict is a typed report entry,
        never a silent override of a live namespace."""

        def _fake_discover(**_kwargs: object):
            return [
                _blueprint("pack-a", {"shared": "uvx a-mcp serve"}),
                _blueprint("pack-b", {"shared": "uvx b-mcp serve", "b-only": "uvx b2 serve"}),
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
        spec_before = getattr(first, "_clio_namespace_specs", {}).get("shared")
        with tool_workspace_context(root), tool_blueprint_context("pack-b"):
            second = agent._active_tool_executor()

        assert second is first
        specs = getattr(first, "_clio_namespace_specs", {})
        assert specs.get("shared") is spec_before, "collision must keep the FIRST mounted spec"
        assert "b-only" in specs, "non-colliding namespaces still merge"

    def test_federation_epoch_bump_defers_eviction_under_a_live_turn_lease(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#1244 rerun evidence: a federation-epoch invalidation landing while
        the root is turn-leased must NOT close the resident fleet — the live
        turn keeps its consistent snapshot and the restart defers to the
        reaper's idle pass. Pre-fix this closed inline (no busy/lease check),
        killing the in-flight campaign turn."""

        def _fake_discover(**_kwargs: object):
            return [_blueprint("pack-a", {"a-server": "uvx a-mcp serve"})]

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
            agent._relay_federation_epoch = getattr(agent, "_relay_federation_epoch", 0) + 1
            with agent.lease_workspace_fleet(root):
                second = agent._active_tool_executor()

        assert second is first, "a leased root must keep serving its resident fleet"
        assert getattr(first, "closed", False) is False
        assert root in agent._workspace_reaper._pending_restarts, (
            "the invalidation must be flagged for the reaper's idle pass, not dropped"
        )

    def test_federation_epoch_bump_evicts_resident_executor(
        self, agent: ClioAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FAILING-FIRST for #1236 (the run-15/17 brick): a resident workspace
        executor minted while the relay federation was ABSENT must be evicted
        on its next resolve after a successful federation refresh — pre-fix,
        the refresh rebuilt only the DEFAULT executor and the workspace's
        resident one kept serving a toolless snapshot, so every custom-agent
        ACL bricked custom_agent_tools_unavailable with federation=present."""

        def _fake_discover(**_kwargs: object):
            return [_blueprint("pack-a", {"a-server": "uvx a-mcp serve"})]

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

        # The federation refresh path bumps the epoch (relay_wiring does this
        # after re-seeding _tool_definitions).
        agent._relay_federation_epoch = getattr(agent, "_relay_federation_epoch", 0) + 1

        with tool_workspace_context(root), tool_blueprint_context("pack-a"):
            second = agent._active_tool_executor()

        assert first is not second, "resident executor must be evicted on epoch bump"
        assert getattr(first, "closed", False) is True
        assert getattr(second, "_clio_federation_epoch", None) == agent._relay_federation_epoch

        # Stable epoch -> the resident executor is reused, no rebuild churn.
        with tool_workspace_context(root), tool_blueprint_context("pack-a"):
            third = agent._active_tool_executor()
        assert third is second
