"""On-demand tool mounting at the expert-tool resolve seam (#1237 Gap 2a).

Owner ruling (2026-08-20): a declared tool whose server has not mounted yet
is never a hard failure at resolve time -- the resolve triggers (or joins)
the server's mount and, once it lands, the tool is used for real. A failed
attempt is never a cached terminal state: the next resolve/call re-attempts.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.agents.builders import _resolve_declared_tools_with_on_demand_mount
from clio_agent.gact.runtime.globals import _UnsupportedSessionAgent
from clio_agent.tools.mcp_config import MCPServerSpec


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeExecutor:
    """Minimal stand-in for SyncMCPToolExecutor: declared namespace specs +
    a mergeable live tool table, exactly the surface
    ``_resolve_declared_tools_with_on_demand_mount`` reads/writes."""

    def __init__(self, declared_specs: dict[str, MCPServerSpec], preloaded: dict[str, Any]) -> None:
        self._clio_namespace_specs = declared_specs
        self._mcp_tools: dict[str, Any] = dict(preloaded)
        self.prepared_namespaces: set[str] = set()

    def to_dspy_tools(self) -> list[Any]:
        return [_FakeTool(name) for name in self._mcp_tools]

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        del namespace
        self._mcp_tools.update(tools)

    def prepare_namespace(self, namespace: str) -> None:
        assert namespace in self._clio_namespace_specs
        self.prepared_namespaces.add(namespace)

    def is_namespace_prepared(self, namespace: str) -> bool:
        return namespace in self.prepared_namespaces


def _spec(name: str) -> MCPServerSpec:
    return MCPServerSpec(name=name, transport="stdio", command="fake-launcher", args=())


class TestOnDemandMount:
    def test_declared_but_unmounted_tool_is_mounted_on_demand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = _FakeExecutor(declared_specs={"geo": _spec("geo")}, preloaded={})
        monkeypatch.setattr(
            "clio_agent.tools.mcp_discovery.ensure_namespace",
            lambda ns, spec: {"geo_geocode": _FakeTool("geo_geocode")},
        )

        available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
            executor, ["geo_geocode"]
        )

        assert "geo_geocode" in available
        assert mount_failures == {}
        assert "geo_geocode" in executor._mcp_tools, "the merge must reach the LIVE table"

    def test_undeclared_namespace_is_never_mounted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = _FakeExecutor(declared_specs={}, preloaded={})
        calls: list[str] = []
        monkeypatch.setattr(
            "clio_agent.tools.mcp_discovery.ensure_namespace",
            lambda ns, spec: calls.append(ns) or {},
        )

        available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
            executor, ["ghost_tool"]
        )

        assert calls == [], "an undeclared namespace must never trigger a mount attempt"
        assert "ghost_tool" not in available
        assert mount_failures == {}

    def test_transient_mount_retries_inside_first_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = _FakeExecutor(declared_specs={"geo": _spec("geo")}, preloaded={})
        attempts: list[int] = []

        def _fake_ensure(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionRefusedError("first attempt: transient")
            return {"geo_geocode": _FakeTool("geo_geocode")}

        monkeypatch.setattr("clio_agent.tools.mcp_discovery.ensure_namespace", _fake_ensure)
        monkeypatch.setattr("clio_agent.gact.mcp_readiness.time.sleep", lambda _delay: None)

        available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
            executor, ["geo_geocode"]
        )
        assert "geo_geocode" in available
        assert mount_failures == {}
        assert len(attempts) == 2

    def test_terminal_mount_failure_is_named_without_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = _FakeExecutor(declared_specs={"geo": _spec("geo")}, preloaded={})
        attempts: list[int] = []

        def _missing_launcher(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
            del namespace, spec
            attempts.append(1)
            raise FileNotFoundError("fake-launcher")

        monkeypatch.setattr("clio_agent.tools.mcp_discovery.ensure_namespace", _missing_launcher)

        available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
            executor, ["geo_geocode"]
        )

        assert "geo_geocode" not in available
        assert mount_failures["geo"]
        assert attempts == [1]

    def test_cached_listing_still_prepares_the_workspace_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo")},
            preloaded={"geo_geocode": _FakeTool("geo_geocode")},
        )
        calls: list[str] = []
        monkeypatch.setattr(
            "clio_agent.tools.mcp_discovery.ensure_namespace",
            lambda ns, spec: calls.append(ns) or {},
        )

        available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
            executor, ["geo_geocode"]
        )

        assert calls == ["geo"], "a listing cache hit is not a persistent workspace connection"
        assert executor.prepared_namespaces == {"geo"}
        assert "geo_geocode" in available
        assert mount_failures == {}

    def test_prepared_tool_never_retriggers_mount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo")},
            preloaded={"geo_geocode": _FakeTool("geo_geocode")},
        )
        executor.prepared_namespaces.add("geo")
        calls: list[str] = []
        monkeypatch.setattr(
            "clio_agent.tools.mcp_discovery.ensure_namespace",
            lambda ns, spec: calls.append(ns) or {},
        )

        available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
            executor, ["geo_geocode"]
        )

        assert calls == []
        assert "geo_geocode" in available
        assert mount_failures == {}


class TestUnsupportedSessionAgentMountFailures:
    def test_carries_mount_failures_and_defaults_empty(self) -> None:
        exc = _UnsupportedSessionAgent(
            "geo-expert",
            reason="custom_agent_tools_unavailable",
            tools=["geo_geocode"],
            mount_failures={"geo": "launcher_cache_lock_timeout"},
        )
        assert exc.mount_failures == {"geo": "launcher_cache_lock_timeout"}

        bare = _UnsupportedSessionAgent("x", reason="unknown_or_non_executable_agent")
        assert bare.mount_failures == {}
