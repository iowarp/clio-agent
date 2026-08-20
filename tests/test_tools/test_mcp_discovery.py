"""Concurrent, non-readiness-blocking MCP namespace discovery (#1232 pt 2).

Pins the bounded-concurrent discovery pass and the background healer:

* a dead namespace degrades TYPED and immediately, never blocking a sibling;
* the pass's wall time is bounded by the SLOWEST namespace, never the SUM
  (the exact serial-cost-multiplication bug #1232 pt 2 fixes);
* a degraded namespace heals on the background re-probe and calls back with
  its tools + the typed heal event.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from clio_agent.errors import MCP_NAMESPACE_DISCOVERY_TIMEOUT, MCP_NAMESPACE_DISCOVERY_UNREACHABLE
from clio_agent.tools import mcp_discovery
from clio_agent.tools.mcp_config import MCPServerSpec


def _spec(name: str) -> MCPServerSpec:
    return MCPServerSpec(name=name, transport="stdio", command="fake-launcher", args=())


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def model_copy(self, *, update: dict[str, Any]) -> "_FakeTool":
        return _FakeTool(update.get("name", self.name))


def _patch_namespace_timeout(monkeypatch: pytest.MonkeyPatch, timeout_s: float) -> None:
    monkeypatch.setattr(mcp_discovery, "_namespace_attempt_timeout_s", lambda _ns: timeout_s)


def test_fast_namespace_lists_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
        return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(monkeypatch, 5.0)

    result = mcp_discovery.discover_declared_tools_bounded({"good": _spec("good")})
    assert "good_tool" in result.tools
    assert result.degraded == {}


def test_dead_namespace_degrades_typed_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
        raise ConnectionRefusedError("dead namespace")

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(monkeypatch, 5.0)

    result = mcp_discovery.discover_declared_tools_bounded({"dead": _spec("dead")})
    assert result.tools == {}
    assert result.degraded == {"dead": MCP_NAMESPACE_DISCOVERY_UNREACHABLE}


def test_one_slow_namespace_never_blocks_a_fast_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: revert to serial-with-no-timeout and this test times out the suite."""

    def _fake_list(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
        if namespace == "slow":
            time.sleep(10)  # never completes within the test's bound
        return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(monkeypatch, 0.3)

    started = time.monotonic()
    result = mcp_discovery.discover_declared_tools_bounded(
        {"slow": _spec("slow"), "fast": _spec("fast")}, concurrency=8
    )
    elapsed = time.monotonic() - started

    assert "fast_tool" in result.tools
    assert result.degraded.get("slow") == MCP_NAMESPACE_DISCOVERY_TIMEOUT
    # Bounded by the DEADLINE, not the sleep duration -- proves the pass moved
    # on rather than waiting for the slow namespace's thread to finish.
    assert elapsed < 2.0, f"pass took {elapsed:.2f}s -- one slow namespace blocked the rest"


def test_three_dead_namespaces_cost_the_max_not_the_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    """The concurrency fix in numbers: 3 namespaces each needing ~0.3s degrade in
    ~0.3s total (concurrent), not ~0.9s (serial) -- the exact "three dead
    namespaces -> minutes of boot" shape, scaled down for a fast unit test."""

    def _fake_list(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
        time.sleep(0.3)
        raise ConnectionRefusedError("dead")

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(
        monkeypatch, 5.0
    )  # long enough that the sleep, not the deadline, decides

    specs = {f"dead{i}": _spec(f"dead{i}") for i in range(3)}
    started = time.monotonic()
    result = mcp_discovery.discover_declared_tools_bounded(specs, concurrency=8)
    elapsed = time.monotonic() - started

    assert set(result.degraded) == set(specs)
    assert elapsed < 0.9, f"pass took {elapsed:.2f}s -- namespaces ran serially, not concurrently"


def test_empty_specs_returns_immediately() -> None:
    result = mcp_discovery.discover_declared_tools_bounded({})
    assert result.tools == {} and result.degraded == {}


class TestNamespaceDiscoveryHealer:
    def test_mark_degraded_is_visible_in_pending(self) -> None:
        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict, on_healed=lambda *_a: None, tick_s=1000.0
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        assert healer.pending() == {"dead": MCP_NAMESPACE_DISCOVERY_UNREACHABLE}

    def test_probe_once_heals_and_calls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = _spec("dead")
        healed_calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_list(namespace: str, s: MCPServerSpec) -> dict[str, Any]:
            return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

        monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)

        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=lambda: {"dead": spec},
            on_healed=lambda ns, tools: healed_calls.append((ns, tools)),
            tick_s=1000.0,
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        healed = healer.probe_once()

        assert healed == ["dead"]
        assert healer.pending() == {}
        assert healed_calls == [("dead", {"dead_tool": healed_calls[0][1]["dead_tool"]})]

    def test_probe_once_keeps_a_still_dead_namespace_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _still_dead(namespace: str, s: MCPServerSpec) -> dict[str, Any]:
            raise ConnectionRefusedError("still dead")

        monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _still_dead)

        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=lambda: {"dead": _spec("dead")},
            on_healed=lambda *_a: pytest.fail("must not heal a still-dead namespace"),
            tick_s=1000.0,
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        healed = healer.probe_once()
        assert healed == []
        assert "dead" in healer.pending()

    def test_probe_once_drops_a_namespace_no_longer_declared(self) -> None:
        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict,  # empty -> "dead" is no longer declared
            on_healed=lambda *_a: pytest.fail("must not call back for an undeclared namespace"),
            tick_s=1000.0,
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        healed = healer.probe_once()
        assert healed == []
        assert healer.pending() == {}

    def test_start_stop_runs_and_shuts_down_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ticks = threading.Event()
        monkeypatch.setattr(
            mcp_discovery.NamespaceDiscoveryHealer,
            "probe_once",
            lambda self: (ticks.set(), [])[1],
        )
        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict, on_healed=lambda *_a: None, tick_s=0.05
        )
        healer.start()
        assert ticks.wait(timeout=2.0), "healer thread never ticked"
        healer.stop()
        assert not healer._thread.is_alive()

    def test_request_stop_does_not_block_the_caller(self) -> None:
        """SABOTAGE: a caller on the event loop calling stop() (the blocking join,
        up to tick_s + 5s) instead of request_stop() would freeze the server on
        every periodic relay-catalog refresh (#1232 pt 2 thread-leak fix)."""

        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict, on_healed=lambda *_a: None, tick_s=1000.0
        )
        healer.start()
        started = time.monotonic()
        healer.request_stop()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"request_stop blocked for {elapsed:.2f}s -- must be non-blocking"
        # The thread still exits promptly even though the caller did not wait.
        healer._thread.join(timeout=5.0)
        assert not healer._thread.is_alive()
