"""Per-active-workspace tool executor: stdio MCPs spawn with cwd=workspace root.

These tests pin the routing/caching contract of ``ClioAgent._active_tool_executor``
without constructing a full agent or spawning subprocesses: a no-op ``ClioAgent``
instance is created with ``__new__`` and only the per-workspace fields are set.
"""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.agent import ClioAgent
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.workspace_root import canonical_workspace_root


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

    def fake_build(*, cwd=None, set_catalog=False, server_set=""):
        built.append((cwd, set_catalog))
        return f"gateway:{cwd}"

    def fake_create(gateway, **kwargs):
        # #932 wiring pin: workspace executors must receive the preload seam.
        assert "preloaded_tools" in kwargs and "namespace_servers" in kwargs
        return f"executor:{gateway}"

    monkeypatch.setattr(agent, "_build_tool_gateway", fake_build)
    monkeypatch.setattr("clio_agent.agent.create_sync_tool_executor", fake_create)

    # Roots are filed under ``canonical_workspace_root``, so a root reaches the
    # gateway/registry in ONE spelling no matter how the workspace stored it.
    alpha = canonical_workspace_root("/ws/alpha")
    beta = canonical_workspace_root("/ws/beta")

    with tool_workspace_context("/ws/alpha"):
        first = agent._active_tool_executor()
        second = agent._active_tool_executor()  # same workspace -> cached

    assert first == f"executor:gateway:{alpha}"
    assert second is first
    # Built exactly once for this workspace, with cwd=root and no catalog reset.
    assert built == [(alpha, False)]
    assert set(agent._workspace_tool_executors) == {alpha}

    # ...and a second spelling of the SAME root reuses that executor rather than
    # spawning a second fleet over the same stdio MCPs.
    with tool_workspace_context("/ws/alpha/"):
        assert agent._active_tool_executor() is first
    assert built == [(alpha, False)]

    # A different workspace spawns its own executor (its own stdio MCPs).
    with tool_workspace_context("/ws/beta"):
        other = agent._active_tool_executor()
    assert other == f"executor:gateway:{beta}"
    assert built == [(alpha, False), (beta, False)]
    assert set(agent._workspace_tool_executors) == {alpha, beta}


def test_workspace_executor_preserves_user_service_declarations_without_plugin_servers(
    monkeypatch,
) -> None:
    """User MCP services remain mountable in ordinary workspace sessions."""

    agent = _bare_agent()
    agent._tool_definitions = {}
    gateway = SimpleNamespace()
    web_spec = SimpleNamespace(
        transport="stdio",
        command="clio-kit",
        args=("mcp-server", "web"),
        env={},
        always_load=True,
    )
    executor = SimpleNamespace(_async_executor=SimpleNamespace())

    monkeypatch.setattr(agent, "_build_tool_gateway", lambda **_kwargs: gateway)
    monkeypatch.setattr("clio_agent.agent.namespace_specs", lambda _gateway: {"web": web_spec})
    monkeypatch.setattr("clio_agent.agent.namespace_proxies", lambda _gateway: {})
    monkeypatch.setattr(
        "clio_agent.agent.create_sync_tool_executor", lambda _gateway, **_kwargs: executor
    )
    monkeypatch.setattr("clio_agent.tools.listing_cache.load_listing", lambda *_a, **_kw: None)

    with tool_workspace_context("/ws/plain"):
        resolved = agent._active_tool_executor()

    assert resolved is executor
    assert resolved._clio_namespace_specs == {"web": web_spec}
    assert resolved._async_executor._clio_namespace_specs == {"web": web_spec}


def test_blank_workspace_root_falls_back_to_default(monkeypatch) -> None:
    agent = _bare_agent()

    def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("blank workspace must not build a gateway")

    monkeypatch.setattr(agent, "_build_tool_gateway", boom)

    with tool_workspace_context("   "):
        assert agent._active_tool_executor() is agent.tool_executor


def test_workspace_gateway_reads_that_workspace_mcp_yaml(tmp_path, monkeypatch) -> None:
    """#1300: a workspace's mcp.yaml reaches its gateway when the server runs elsewhere."""
    workspace = tmp_path / "workspace"
    shared = workspace / ".clio-agent" / "shared"
    shared.mkdir(parents=True)
    (shared / "mcp.yaml").write_text(
        "mcp_servers:\n  ws_only:\n    command: [ws-only-server]\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "server-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    agent = _bare_agent()
    monkeypatch.setattr(agent, "_discover_plugin_servers", lambda server_set="": {})
    seen: dict[str, object] = {}

    def fake_build_gateway(specs, *, cwd=None):
        seen.update(specs=dict(specs), cwd=cwd)
        return "gateway"

    monkeypatch.setattr("clio_agent.agent.build_gateway", fake_build_gateway)

    assert agent._build_tool_gateway(cwd=str(workspace)) == "gateway"
    assert "ws_only" in seen["specs"]
    assert seen["cwd"] == str(workspace)


class _Fleet(SimpleNamespace):
    """A resident fleet double that records whether it was closed."""

    def close(self) -> None:
        self.closed = True


def _two_snapshot_agent(monkeypatch, *, defer: bool):
    """An agent whose server sets ``old`` and ``new`` declare ``docs`` differently."""

    agent = _bare_agent()
    agent._tool_definitions = {}
    agent._workspace_reaper = SimpleNamespace(
        defer_restart_if_active=lambda _root, _executor, _leases: defer,
        note_resolved=lambda *_a, **_kw: None,
    )
    old = SimpleNamespace(transport="stdio", command="/plugins/docs/1.0/run", args=(), env={})
    new = SimpleNamespace(transport="stdio", command="/plugins/docs/1.1/run", args=(), env={})
    specs = {"old": {"docs": old}, "new": {"docs": new}, "same": {"docs": old}}
    built: list[_Fleet] = []

    def specs_of(gateway):
        return specs[gateway.server_set]

    def create(_gateway, **_kwargs):
        built.append(_Fleet(closed=False, _async_executor=SimpleNamespace(_namespace_servers={})))
        return built[-1]

    monkeypatch.setattr(
        agent, "_build_tool_gateway", lambda **kw: SimpleNamespace(server_set=kw["server_set"])
    )
    for owner in ("clio_agent.agent", "clio_agent.tools.fleet_server_merge"):
        monkeypatch.setattr(f"{owner}.namespace_specs", specs_of)
        monkeypatch.setattr(f"{owner}.namespace_proxies", lambda _gateway: {})
    monkeypatch.setattr(
        "clio_agent.tools.fleet_server_merge.namespace_direct_factories", lambda _gateway: {}
    )
    monkeypatch.setattr("clio_agent.agent.create_sync_tool_executor", create)
    monkeypatch.setattr("clio_agent.tools.listing_cache.load_listing", lambda *_a, **_kw: None)
    return agent, built, old, new


def _resolve(agent, server_set: str):
    from clio_agent.tools.execution import tool_server_set_context

    with tool_workspace_context("/ws/paper"), tool_server_set_context(server_set):
        return agent._active_tool_executor()


def test_an_updated_plugin_snapshot_rebuilds_an_idle_fleet(monkeypatch) -> None:
    """DS40: a new session on an updated plugin gets the new snapshot, not the first mounted."""

    agent, built, _old, new = _two_snapshot_agent(monkeypatch, defer=False)
    first = _resolve(agent, "old")
    events: list[str] = []
    monkeypatch.setattr(
        "clio_agent.tools.fleet_server_merge.trace.event",
        lambda _cat, fmt, *args: events.append(fmt % args),
    )

    second = _resolve(agent, "new")

    assert second is not first and first.closed
    assert second._clio_namespace_specs == {"docs": new}
    assert len(built) == 2
    assert any("workspace_fleet_namespace_spec_changed" in e and "docs" in e for e in events)


def test_an_updated_plugin_snapshot_waits_for_a_live_turn(monkeypatch) -> None:
    """A leased fleet is never closed under a turn; the reaper rebuilds it at idle."""

    agent, built, old, _new = _two_snapshot_agent(monkeypatch, defer=True)
    first = _resolve(agent, "old")

    second = _resolve(agent, "new")

    assert second is first and not first.closed
    assert first._clio_namespace_specs == {"docs": old}
    assert len(built) == 1


def test_a_server_set_declaring_the_same_spec_merges_without_a_rebuild(monkeypatch) -> None:
    agent, built, old, _new = _two_snapshot_agent(monkeypatch, defer=False)
    first = _resolve(agent, "old")

    second = _resolve(agent, "same")

    assert second is first and not first.closed
    assert first._clio_mounted_server_sets == {"old", "same"}
    assert first._clio_namespace_specs == {"docs": old}
    assert len(built) == 1
