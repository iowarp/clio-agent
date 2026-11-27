"""A saved MCP configuration change reaches a resident fleet server by server.

Enabling one connector moves the configuration revision. Only the namespaces whose
declaration moved restart; every other server keeps running with its state.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.tools import fleet_close_listeners, fleet_reconcile, workspace_fleet
from clio_agent.tools.mcp_config import spec_from_declaration


def _spec(name: str, command: str) -> Any:
    # A launcher on PATH, so a real proxy mounts; the argument tells declarations apart.
    return spec_from_declaration(
        name, {"command": sys.executable, "args": [command]}, source="user"
    )


class _Inner:
    """The async executor's routing table and tool table, without servers behind them."""

    def __init__(self, specs: dict[str, Any]) -> None:
        self._clio_namespace_specs = specs
        self._namespace_servers: dict[str, Any] = {ns: object() for ns in specs}
        self._clio_namespace_direct_factories: dict[str, Any] = {}
        self._mcp_tools: dict[str, Any] = {f"{ns}_ping": object() for ns in specs}

    @property
    def tools_revision(self) -> int:
        return self.__dict__.get("_clio_tools_revision", 0)


class _Fleet:
    """A resident fleet stamped the way ``stamp_fresh_fleet`` stamps one."""

    def __init__(self, specs: dict[str, Any], *, epoch: int = 1) -> None:
        self._clio_namespace_specs = specs
        self._clio_mounted_server_sets: set[str] = set()
        self._clio_mcp_configuration_revision = epoch
        self._async_executor = _Inner(specs)
        self.reset: list[str] = []
        self.closed = False

    def reset_namespace(self, namespace: str) -> None:
        self.reset.append(namespace)

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        self._async_executor._mcp_tools.update(tools)

    def close(self) -> None:
        self.closed = True


def _agent(declared: dict[str, Any], *, epoch: int = 2) -> Any:
    from fastmcp import FastMCP

    from clio_agent.tools.gateway import build_gateway

    def gateway(*, cwd: str | None = None, server_set: str = "", **_: Any) -> Any:
        return build_gateway(declared, cwd=cwd, proxy_factory=lambda spec: FastMCP(name="stub"))

    return SimpleNamespace(
        _build_tool_gateway=gateway,
        _mcp_configuration_revision=epoch,
        _workspace_reaper=None,
        _tool_definitions={},
        tool_executor=None,
    )


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        "clio_agent.runtime.trace.event", lambda _c, fmt, *a, **_k: seen.append(fmt % a)
    )
    return seen


@pytest.fixture
def dropped(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    seen: list[tuple[Any, str]] = []

    def record(root: str, proxies: Any, reason: str) -> None:
        seen.extend((proxy, reason) for proxy in proxies)

    monkeypatch.setattr(fleet_close_listeners, "proxies_dropped", record)
    return seen


def test_an_unchanged_server_keeps_running_when_another_is_added(
    events: list[str], dropped: list[tuple[Any, str]], tmp_path: Path
) -> None:
    kept = _spec("alpha", "alpha-server")
    fleet = _Fleet({"alpha": kept})
    alpha_proxy = fleet._async_executor._namespace_servers["alpha"]
    agent = _agent({"alpha": kept, "beta": _spec("beta", "beta-server")})

    report = fleet_reconcile.reconcile_configuration(agent, fleet, str(tmp_path), 2)

    assert report == {"changed": [], "removed": [], "added": ["beta"], "kept": 1}
    assert fleet._async_executor._namespace_servers["alpha"] is alpha_proxy
    assert "beta" in fleet._async_executor._namespace_servers
    assert list(fleet._clio_namespace_specs["beta"].args) == ["beta-server"]
    assert fleet.reset == [] and dropped == [] and not fleet.closed
    assert fleet._clio_mcp_configuration_revision == 2
    assert any("workspace_fleet_configuration_reconciled" in e and "kept=1" in e for e in events)


def test_only_the_changed_server_is_reinstalled(
    events: list[str], dropped: list[tuple[Any, str]], tmp_path: Path
) -> None:
    fleet = _Fleet({"alpha": _spec("alpha", "alpha-server"), "beta": _spec("beta", "old")})
    servers = fleet._async_executor._namespace_servers
    alpha_proxy, beta_proxy = servers["alpha"], servers["beta"]
    agent = _agent({"alpha": _spec("alpha", "alpha-server"), "beta": _spec("beta", "new")})

    report = fleet_reconcile.reconcile_configuration(agent, fleet, str(tmp_path), 2)

    assert report["changed"] == ["beta"] and report["kept"] == 1
    assert servers["alpha"] is alpha_proxy and servers["beta"] is not beta_proxy
    assert list(fleet._clio_namespace_specs["beta"].args) == ["new"]
    assert fleet.reset == ["beta"]
    assert dropped == [(beta_proxy, "namespace_rebound")]


def test_a_server_no_longer_declared_is_stopped_and_unrouted(
    events: list[str], dropped: list[tuple[Any, str]], tmp_path: Path
) -> None:
    fleet = _Fleet({"alpha": _spec("alpha", "alpha-server"), "beta": _spec("beta", "b")})
    inner = fleet._async_executor
    inner._mcp_tools["beta-two_ping"] = object()
    beta_proxy = inner._namespace_servers["beta"]
    agent = _agent({"alpha": _spec("alpha", "alpha-server")})

    report = fleet_reconcile.reconcile_configuration(agent, fleet, str(tmp_path), 2)

    assert report["removed"] == ["beta"] and report["kept"] == 1
    assert "beta" not in inner._namespace_servers and "beta" not in fleet._clio_namespace_specs
    assert set(inner._mcp_tools) == {"alpha_ping", "beta-two_ping"}
    assert inner.tools_revision == 1
    assert fleet.reset == ["beta"] and dropped == [(beta_proxy, "namespace_removed")]


def _resolve(agent: Any, fleet: _Fleet, root: Path) -> tuple[Any, dict[str, Any]]:
    from clio_agent.plugins.servers import server_set_variant
    from clio_agent.tools.execution import tool_workspace_context
    from clio_agent.tools.fleet_key import fleet_key
    from clio_agent.tools.workspace_root import canonical_workspace_root

    key = fleet_key(canonical_workspace_root(str(root)), server_set_variant(""))
    executors: dict[str, Any] = {key: fleet}
    agent._workspace_state = lambda: (threading.Lock(), executors, {})
    with tool_workspace_context(root):
        return workspace_fleet.resolve_workspace_executor(agent), executors


def test_a_configuration_move_keeps_the_resident_fleet(
    events: list[str], dropped: list[tuple[Any, str]], tmp_path: Path
) -> None:
    kept = _spec("alpha", "alpha-server")
    fleet = _Fleet({"alpha": kept})
    agent = _agent({"alpha": kept, "beta": _spec("beta", "beta-server")})

    resolved, _ = _resolve(agent, fleet, tmp_path)

    assert resolved is fleet and not fleet.closed
    assert fleet._clio_mcp_configuration_revision == 2
    assert "beta" in fleet._clio_namespace_specs
    assert not any("revision_evict" in e for e in events)


def test_a_failed_reconcile_raises_and_is_retried_on_the_next_resolve(
    events: list[str], dropped: list[tuple[Any, str]], tmp_path: Path
) -> None:
    kept = _spec("alpha", "alpha-server")
    fleet = _Fleet({"alpha": kept})
    agent = _agent({"alpha": kept, "beta": _spec("beta", "beta-server")})
    working = agent._build_tool_gateway

    def refuse(**_k: Any) -> Any:
        raise RuntimeError("declaration unreadable")

    agent._build_tool_gateway = refuse
    with pytest.raises(RuntimeError, match="declaration unreadable"):
        _resolve(agent, fleet, tmp_path)
    assert fleet._clio_mcp_configuration_revision == 1 and not fleet.closed

    agent._build_tool_gateway = working
    resolved, _ = _resolve(agent, fleet, tmp_path)
    assert resolved is fleet and fleet._clio_mcp_configuration_revision == 2
    assert "beta" in fleet._clio_namespace_specs
