"""Connector follow-ups (CF): one spec for a joined connector (CV-N3), and a closed
fleet's retained connections retired with it (CV-N2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import listing, materialize, state
from clio_agent.gact import connector_rebind
from clio_agent.plugins import servers
from clio_agent.tools import fleet_close_listeners

PLUGIN_ROW = {
    "fixture-secret": {
        "spec": {"type": "clio-connection", "connection": "fixture-secret"},
        "root": "/plugins/fixture-secret-skills",
        "plugin": "fixture-secret-skills",
    }
}


@pytest.fixture
def conversation_set(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, dict[str, Any]]:
    """The conversation's server set with the connector's plugin in it."""

    monkeypatch.setattr(servers, "plugin_data_dir", lambda plugin: tmp_path / plugin)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    state.update("fixture-secret", values={"where": "this-machine"})
    found = state.all_entries()["fixture-secret"]
    saved = materialize.declaration(found)
    key = servers.register_server_set(PLUGIN_ROW, connections={"fixture-secret": saved})
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution.session_server_set", lambda _app, _sid: key
    )
    return key, saved


class _Fleet:
    """A fleet executor's spec stamp and tool table, without a server behind it."""

    def __init__(self) -> None:
        self._clio_namespace_specs: dict[str, Any] = {}
        self._clio_mounted_server_sets: set[str] = {"servers-turn"}
        self.tools: dict[str, Any] = {}

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        self.tools.update(tools)


def _agent() -> Any:
    """``_build_tool_gateway`` as the fleet calls it, over the registered server sets."""

    from fastmcp import FastMCP

    from clio_agent.search.web_mcp_default import load_agent_mcp_servers
    from clio_agent.tools.gateway import build_gateway

    def gateway(*, cwd: str | None = None, server_set: str = "", **_: Any) -> Any:
        declared = servers.server_set_declarations(server_set)
        specs = load_agent_mcp_servers(cwd=cwd, plugin_servers=declared)
        return build_gateway(specs, cwd=cwd, proxy_factory=lambda spec: FastMCP(name="stub"))

    return SimpleNamespace(_build_tool_gateway=gateway)


def _join(fleet: _Fleet, saved: dict[str, Any], spec: Any, monkeypatch: Any) -> None:
    from mcp.types import Tool

    def install(executor: Any, namespace: str, spec: Any, root: str) -> None:
        executor._clio_namespace_specs[namespace] = spec

    monkeypatch.setattr(connector_rebind, "_install", install)
    listing.remember("fixture-secret", [Tool(name="ping", inputSchema={"type": "object"})])
    connector_rebind.join_namespace(None, fleet, "fixture-secret", saved, spec=spec)


def test_the_conversation_spec_is_the_one_its_server_set_declares(
    conversation_set: tuple[str, dict[str, Any]],
) -> None:
    spec = connector_rebind.conversation_spec(None, "sess_a", "fixture-secret", "/ws")
    assert spec is not None and spec.source == "plugin:fixture-secret-skills"
    assert spec.env["PLUGIN_ROOT"] == "/plugins/fixture-secret-skills"
    assert spec.env["PLUGIN_DATA"].endswith("fixture-secret-skills")
    assert spec.env["FIXTURE_WHERE"] == "this-machine"


def test_a_joined_connector_is_not_a_changed_spec_on_the_next_resolve(
    conversation_set: tuple[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.tools.fleet_server_merge import (
        merge_server_set_namespaces,
        server_set_spec_changed,
    )

    key, saved = conversation_set
    events: list[str] = []
    monkeypatch.setattr(
        "clio_agent.tools.fleet_server_merge.trace.event",
        lambda _c, fmt, *a: events.append(fmt % a),
    )
    agent = _agent()
    # Before (CV-N3): the join built its spec from the saved connection alone.
    before = _Fleet()
    _join(before, saved, None, monkeypatch)
    assert server_set_spec_changed(agent, before, "/ws", key) is True
    assert "workspace_fleet_namespace_spec_changed" in " ".join(events)
    # Now: the conversation's declared spec, the one the fleet mounts.
    events.clear()
    fleet = _Fleet()
    declared = connector_rebind.conversation_spec(None, "sess_a", "fixture-secret", "/ws")
    _join(fleet, saved, declared, monkeypatch)
    assert fleet.tools and fleet._clio_namespace_specs["fixture-secret"] is declared
    assert server_set_spec_changed(agent, fleet, "/ws", key) is False
    # A second conversation of the workspace joins the same way: nothing is reinstalled,
    # so the server (and every view it serves) stays.
    installs: list[str] = []
    monkeypatch.setattr(connector_rebind, "_install", lambda _e, ns, _s, _r: installs.append(ns))
    again = connector_rebind.conversation_spec(None, "sess_b", "fixture-secret", "/ws")
    connector_rebind.join_namespace(None, fleet, "fixture-secret", saved, spec=again)
    assert installs == []
    report = merge_server_set_namespaces(
        fleet, agent._build_tool_gateway(cwd="/ws", server_set=key), server_set=key, root="/ws"
    )
    assert report["already"] == ["fixture-secret"] and report["conflicts"] == []
    assert not any("spec_changed" in e or "spec_conflict" in e for e in events)


def test_an_undeclared_namespace_is_typed_and_the_join_uses_the_connection(
    conversation_set: tuple[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(connector_rebind.trace, "event", lambda _c, fmt, *a: events.append(fmt % a))
    assert connector_rebind.conversation_spec(None, "sess_a", "fixture-plain", "/ws") is None
    assert connector_rebind.conversation_spec(None, "", "fixture-secret", "/ws") is None
    said = " ".join(events)
    assert "connector_join_spec_undeclared namespace=fixture-plain" in said
    assert "reason=not_in_server_set" in said and "reason=no_conversation" in said


def test_join_running_turn_joins_with_the_conversation_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import connector_offer as offer
    from clio_agent.gact import connectors_service as service
    from clio_agent.gact.agents import clio_react
    from clio_agent.gact.mcp_app_tools import AppToolRefresher

    executor = SimpleNamespace(to_dspy_tools=lambda: [], tools_revision=0)
    program = SimpleNamespace(_clio_tool_refresh=AppToolRefresher(None))
    monkeypatch.setattr(clio_react, "active_loop", lambda: SimpleNamespace(agent=program))
    monkeypatch.setattr(service, "saved_connection", lambda cid: {"command": "x"})
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._active_base_agent_tool_executor", lambda _a: executor
    )
    monkeypatch.setattr(offer, "_session_id", lambda: "sess_a")
    monkeypatch.setattr("clio_agent.gact.connector_values.session_overlay", lambda _app, _sid: {})
    declared = object()
    asked: list[tuple[str, str]] = []

    def spec(_app: Any, sid: str, namespace: str, root: str) -> Any:
        asked.append((sid, namespace))
        return declared

    def join(_agent: Any, got: Any, namespace: str, _decl: Any, *, spec: Any = None) -> dict:
        assert got is executor and spec is declared
        return {f"{namespace}_ping": object()}

    monkeypatch.setattr(connector_rebind, "conversation_spec", spec)
    monkeypatch.setattr(connector_rebind, "join_namespace", join)
    app = SimpleNamespace(state=SimpleNamespace(agent=None))
    assert offer.join_running_turn(app, "fixture-plain") == (["fixture-plain_ping"], "")
    assert asked == [("sess_a", "fixture-plain")]


# ------------------------------------------------------------------- CV-N2


def test_listeners_hear_dropped_proxies_and_a_failing_one_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        fleet_close_listeners.trace, "event", lambda _c, fmt, *a: events.append(fmt % a)
    )
    heard: list[tuple[str, tuple[Any, ...], str]] = []
    proxy = object()
    fleet = SimpleNamespace(_async_executor=SimpleNamespace(_namespace_servers={"pv": proxy}))

    def broken(*_: Any) -> None:
        raise RuntimeError("boom")

    remove_broken = fleet_close_listeners.listen(broken)
    remove = fleet_close_listeners.listen(lambda *a: heard.append(a))
    try:
        fleet_close_listeners.fleet_closed("/ws|cv=abc", fleet, "idle_ttl")
        fleet_close_listeners.proxies_dropped("/ws", (None,), "namespace_rebound")
    finally:
        remove_broken()
        remove()
    assert heard == [("/ws|cv=abc", (proxy,), "idle_ttl")]
    assert "fleet_close_listener_failed root=/ws|cv=abc reason=idle_ttl" in " ".join(events)
    fleet_close_listeners.fleet_closed("/ws", fleet, "idle_ttl")
    assert len(heard) == 1  # removed


def test_the_reaper_tells_listeners_which_fleet_it_closed() -> None:
    import threading

    from clio_agent.tools.reaper import WorkspaceExecutorReaper

    proxy = object()

    class Fleet:
        busy = False
        _async_executor = SimpleNamespace(_namespace_servers={"pv": proxy})

        def idle_for(self) -> float:
            return 999.0

        def close(self) -> None:
            pass

    heard: list[tuple[str, tuple[Any, ...], str]] = []
    remove = fleet_close_listeners.listen(lambda *a: heard.append(a))
    try:
        registry: dict[str, Any] = {"/ws": Fleet()}
        reaper = WorkspaceExecutorReaper(
            registry, threading.Lock(), ttl_s=1.0, channel_closer=lambda _r: 0
        )
        assert reaper.reap_once() == ["/ws"]
    finally:
        remove()
    assert heard == [("/ws", (proxy,), "idle_ttl")]


def test_a_closed_fleets_retained_connection_is_retired() -> None:
    from clio_agent.gact import external_task_connections as connections

    proxy, other = object(), object()
    closed: list[str] = []

    def host(owner: object, *, busy: bool = False) -> Any:
        key = ("pv", "scope", f"in-process:{id(owner):x}")
        return SimpleNamespace(
            cache_key=key, rpcs={1} if busy else set(), calls={}, drivers={}, superseded=False
        )

    idle, busy, kept = host(proxy), host(proxy, busy=True), host(other)
    busy.cache_key = ("pv", "scope2", idle.cache_key[2])

    async def run() -> int:
        async def retire(_self: Any, h: Any, *, shutdown: bool = False) -> None:
            closed.append(h.cache_key[1])

        class Tasks:  # what watch_fleets reads of ExternalTasks
            connections = {idle.cache_key: idle, busy.cache_key: busy, kept.cache_key: kept}
            _closing = False

        tasks = Tasks()
        original = connections.retire_connection
        connections.retire_connection = retire  # type: ignore[assignment]
        try:
            loop = asyncio.get_running_loop()
            connections.watch_fleets(tasks)  # type: ignore[arg-type]
            done = asyncio.Event()

            def notify() -> None:
                fleet_close_listeners.proxies_dropped("/ws|cv=abc", (proxy,), "idle_ttl")
                loop.call_soon_threadsafe(done.set)

            await asyncio.to_thread(notify)
            await done.wait()
            for _ in range(20):
                await asyncio.sleep(0)
            return await connections.retire_owned(tasks, set(), "/ws", "x")  # type: ignore[arg-type]
        finally:
            connections.retire_connection = original  # type: ignore[assignment]
            tasks.__dict__.get("_unwatch_fleets", lambda: None)()

    assert asyncio.run(run()) == 0
    assert closed == ["scope"]  # the idle one, now
    assert busy.superseded is True  # the busy one retires when its calls end
    assert kept.superseded is False
