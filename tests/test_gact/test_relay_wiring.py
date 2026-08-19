"""Tests for #1227 D2: relay catalog TTL refresh instead of cache-forever.

Live L3 evidence: the jarvis surface appeared on the relay door after a
dev-mode relay update and stayed invisible to the agent until the clio-agent
process was restarted -- ``relay_tool_surfaces_for_app`` discovered the
catalog exactly ONCE at boot (``app.state.relay_tool_surfaces`` set once,
never invalidated) and the resulting failure surfaced as an opaque
``custom_agent_tools_unavailable`` / ``not_implemented``, a MISLEADING reason
since the truth was catalog staleness, not tool unavailability.

These tests exercise ``refresh_relay_tool_surfaces_if_stale`` directly against
a minimal fake app/agent (no real relay, no real ClioAgent construction cost)
plus one direct test of ``_refresh_agent_relay_tool_surfaces`` proving the
default tool gateway is genuinely rebuilt, not just attribute-stashed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import relay_wiring
from clio_agent.tools.relay_factory import RelayToolSurfaces


class _FakeApp:
    def __init__(self) -> None:
        self.state = SimpleNamespace()


class _SpyAgent:
    """Stand-in for the app.state.agent singleton, recording refresh calls."""

    def __init__(self) -> None:
        self.refresh_calls: list[Any] = []


def _surfaces(tag: str) -> RelayToolSurfaces:
    return RelayToolSurfaces(
        remote_mcp_federation=f"federation-{tag}",
        jarvis_jobs=f"jarvis-{tag}",
        status={"configured": True, "reason": None, "tag": tag},
    )


@pytest.mark.asyncio
async def test_refresh_is_a_noop_within_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAILING-FIRST for #1227 D2: within the TTL, no re-discovery happens at all."""

    app = _FakeApp()
    app.state.relay_tool_surfaces = _surfaces("first")
    app.state.relay_tool_surfaces_discovered_at = 1000.0

    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 1010.0)  # 10s later
    monkeypatch.setattr(relay_wiring, "_relay_tool_surfaces_ttl_seconds", lambda: 300.0)

    async def _fail_discover() -> Any:
        raise AssertionError("must not re-discover within the TTL")

    monkeypatch.setattr(
        "clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _fail_discover
    )

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is app.state.relay_tool_surfaces
    assert result.status["tag"] == "first"


@pytest.mark.asyncio
async def test_refresh_re_discovers_after_ttl_and_pushes_onto_the_live_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST for #1227 D2: past the TTL, the catalog is re-discovered
    and the already-constructed singleton agent picks it up immediately --
    the exact gap that forced a full process restart live."""

    app = _FakeApp()
    app.state.relay_tool_surfaces = _surfaces("stale")
    app.state.relay_tool_surfaces_discovered_at = 1000.0
    agent = _SpyAgent()
    app.state.agent = agent

    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 2000.0)  # far past TTL
    monkeypatch.setattr(relay_wiring, "_relay_tool_surfaces_ttl_seconds", lambda: 300.0)
    monkeypatch.setattr(
        relay_wiring,
        "_refresh_agent_relay_tool_surfaces",
        lambda a, s: a.refresh_calls.append(s),
    )

    fresh = _surfaces("fresh")

    async def _discover() -> Any:
        return fresh

    monkeypatch.setattr("clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _discover)

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is fresh
    assert app.state.relay_tool_surfaces is fresh
    assert app.state.relay_tool_status == {"configured": True, "reason": None, "tag": "fresh"}
    assert agent.refresh_calls == [fresh]


@pytest.mark.asyncio
async def test_refresh_failure_keeps_the_previous_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient re-discovery failure degrades typed, keeping the previous
    (still-working) catalog rather than tearing it down -- no-silent-fallback:
    the failure is logged, never swallowed into a blank state."""

    app = _FakeApp()
    stale = _surfaces("stale")
    app.state.relay_tool_surfaces = stale
    app.state.relay_tool_surfaces_discovered_at = 1000.0

    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 2000.0)
    monkeypatch.setattr(relay_wiring, "_relay_tool_surfaces_ttl_seconds", lambda: 300.0)

    async def _boom() -> Any:
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr("clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _boom)

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is stale
    assert app.state.relay_tool_surfaces is stale


@pytest.mark.asyncio
async def test_first_discovery_stamps_the_ttl_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No prior discovery at all -> one first-time discovery, TTL clock started."""

    app = _FakeApp()
    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 500.0)
    fresh = _surfaces("boot")

    async def _discover() -> Any:
        return fresh

    monkeypatch.setattr("clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _discover)

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is fresh
    assert app.state.relay_tool_surfaces_discovered_at == 500.0


class _FakeGatewayAgent:
    """A ClioAgent stand-in exposing only what ``_refresh_agent_relay_tool_surfaces``
    touches -- no real pack/blueprint discovery or MCP subprocess spawn, which
    a real ``ClioAgent()`` pays for at construction (and would pay AGAIN here,
    doubling an already-expensive-in-CI cost the #932 preload pass exists to
    bound, not multiply)."""

    def __init__(self) -> None:
        self._tool_definitions = None
        self.tool_executor = object()  # the "old" executor identity
        self.gateway_builds = 0

    def _build_tool_gateway(self, *, set_catalog: bool) -> object:
        assert set_catalog is True
        self.gateway_builds += 1
        return object()  # a fresh gateway identity per call


def test_refresh_agent_relay_tool_surfaces_rebuilds_the_default_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_refresh_agent_relay_tool_surfaces`` genuinely rebuilds the default
    gateway/executor -- a NEW object bound to the new federation, not just an
    attribute stash the old (already-baked) executor never re-reads. Lives in
    relay_wiring.py (not a method on ClioAgent itself) so agent.py -- a bare
    runtime HOST, no-accretion rule -- never grows a relay-refresh method."""

    monkeypatch.setattr(relay_wiring, "create_sync_tool_executor", lambda *a, **k: object())
    monkeypatch.setattr(relay_wiring, "namespace_proxies", lambda gw: {})

    agent = _FakeGatewayAgent()
    old_executor = agent.tool_executor
    marker_federation = object()
    surfaces = SimpleNamespace(
        remote_mcp_federation=marker_federation,
        jarvis_jobs=None,
        status={"configured": True, "reason": None},
    )

    relay_wiring._refresh_agent_relay_tool_surfaces(agent, surfaces)

    assert agent._remote_mcp_federation is marker_federation
    assert agent.gateway_builds == 1
    assert agent.tool_executor is not old_executor
