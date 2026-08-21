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
async def test_degraded_catalog_retries_on_the_short_failure_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST for the run-15 brick: a stored catalog with NO federation
    (the door was down at first discovery) must NOT ride the 300s success TTL
    -- pre-fix, every turn for the whole window returned the dead catalog and
    bricked custom_agent_tools_unavailable even after the door came back."""

    app = _FakeApp()
    app.state.relay_tool_surfaces = RelayToolSurfaces(
        remote_mcp_federation=None,
        jarvis_jobs=None,
        status={"configured": True, "reason": "relay_catalog_discovery_failed"},
    )
    app.state.relay_tool_surfaces_discovered_at = 1000.0
    agent = _SpyAgent()
    app.state.agent = agent

    # 25s later: inside the 300s success TTL, past the 20s failure TTL.
    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 1025.0)
    monkeypatch.setattr(relay_wiring, "_relay_tool_surfaces_ttl_seconds", lambda: 300.0)
    monkeypatch.setattr(
        relay_wiring,
        "_refresh_agent_relay_tool_surfaces",
        lambda a, s: a.refresh_calls.append(s),
    )
    healed = _surfaces("healed")

    async def _discover() -> Any:
        return healed

    monkeypatch.setattr("clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _discover)

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is healed, "a degraded catalog must re-discover on the short clock"
    assert agent.refresh_calls == [healed]


@pytest.mark.asyncio
async def test_degraded_catalog_still_waits_out_the_short_failure_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure TTL is short, not zero: within it, no per-turn hammering of
    a down door -- the dead catalog is returned without a probe."""

    app = _FakeApp()
    app.state.relay_tool_surfaces = RelayToolSurfaces(
        remote_mcp_federation=None,
        jarvis_jobs=None,
        status={"configured": True, "reason": "relay_catalog_discovery_failed"},
    )
    app.state.relay_tool_surfaces_discovered_at = 1000.0

    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 1010.0)  # inside 20s
    monkeypatch.setattr(relay_wiring, "_relay_tool_surfaces_ttl_seconds", lambda: 300.0)

    async def _fail_discover() -> Any:
        raise AssertionError("must not probe within the failure TTL")

    monkeypatch.setattr(
        "clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _fail_discover
    )

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is app.state.relay_tool_surfaces


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
async def test_refresh_without_a_catalog_noops_when_relay_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No catalog + UNCONFIGURED transport -> the per-turn seam does NOTHING.

    The ambient-poison guard (#1229): a stale repo ``.env`` loaded by litellm's
    import-time dotenv configured a half-dead door mid-process, and an
    unconditional first-discovery branch then blocked every child turn of every
    later-built app ~15s (the s7 parity reds). A transport that resolves
    unavailable must never be probed from a turn."""

    from clio_agent.tools.relay_transport import RelayTransportUnavailable

    app = _FakeApp()
    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 500.0)
    monkeypatch.setattr(
        "clio_agent.tools.relay_transport.resolve_relay_transport_config",
        lambda: RelayTransportUnavailable(reason="relay_not_configured", details={}),
    )

    async def _discover() -> Any:
        raise AssertionError("an unconfigured transport must never be probed from a turn")

    monkeypatch.setattr("clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _discover)

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is None
    assert getattr(app.state, "relay_tool_surfaces", None) is None
    assert getattr(app.state, "relay_tool_surfaces_discovered_at", None) is None


async def test_refresh_without_a_catalog_first_discovers_when_relay_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No catalog + CONFIGURED transport -> the seam performs first discovery.

    Under the #1232 lazy boot nothing discovers eagerly, so the first turn is
    the construction moment. Observed live (L3 runs 4-5): with an unconditional
    no-op here, #1229 + #1232 composed into NOBODY discovering and every
    custom-agent ACL bricked typed on custom_agent_tools_unavailable."""

    app = _FakeApp()
    monkeypatch.setattr(relay_wiring.time, "monotonic", lambda: 500.0)
    monkeypatch.setattr(
        "clio_agent.tools.relay_transport.resolve_relay_transport_config",
        lambda: object(),
    )
    fresh = _surfaces("lazy-first")

    async def _discover() -> Any:
        return fresh

    monkeypatch.setattr("clio_agent.tools.relay_transport.discover_relay_tool_surfaces", _discover)

    result = await relay_wiring.refresh_relay_tool_surfaces_if_stale(app)

    assert result is fresh
    assert app.state.relay_tool_surfaces is fresh
    assert app.state.relay_tool_surfaces_discovered_at == 500.0


class _FakeGatewayAgent:
    """A ClioAgent stand-in exposing only what ``_refresh_agent_relay_tool_surfaces``
    touches -- no real pack/blueprint discovery or MCP subprocess spawn, which
    a real ``ClioAgent()`` pays for at construction (and would pay AGAIN here,
    doubling an already-expensive-in-CI cost the #932 preload pass exists to
    bound, not multiply)."""

    def __init__(self) -> None:
        # A real ClioAgent ALWAYS holds a dict here (builtins seeded at
        # construction) -- the late-arrival re-seed updates it in place.
        self._tool_definitions = {"shell_bash": {"name": "shell_bash"}}
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
    monkeypatch.setattr(relay_wiring, "list_relay_tool_definitions", lambda federation: {})

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


def test_refresh_agent_relay_tool_surfaces_reseeds_tool_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST for the run-14 brick: a federation discovered AFTER
    ClioAgent construction must re-seed ``_tool_definitions`` when it is
    pushed onto the live agent -- pre-fix the rebuild passed the stale
    builtins-only dict as ``preloaded_tools``, so the executor kept offering
    four builtins while the diagnostics read federation=present, and every
    custom-agent ACL still bricked custom_agent_tools_unavailable."""

    captured_kwargs: dict = {}

    def _capture_executor(*args, **kwargs) -> object:
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(relay_wiring, "create_sync_tool_executor", _capture_executor)
    monkeypatch.setattr(relay_wiring, "namespace_proxies", lambda gw: {})
    relay_definitions = {"relay_wait": {"name": "relay_wait"}}
    monkeypatch.setattr(
        relay_wiring, "list_relay_tool_definitions", lambda federation: relay_definitions
    )

    agent = _FakeGatewayAgent()
    surfaces = SimpleNamespace(
        remote_mcp_federation=object(),
        jarvis_jobs=None,
        status={"configured": True, "reason": None},
    )

    relay_wiring._refresh_agent_relay_tool_surfaces(agent, surfaces)

    assert "relay_wait" in agent._tool_definitions, "late federation must re-seed definitions"
    assert "shell_bash" in agent._tool_definitions, "builtins must survive the re-seed"
    assert captured_kwargs["preloaded_tools"] is agent._tool_definitions


def test_refresh_agent_relay_tool_surfaces_unchanged_catalog_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1244 rerun evidence (2026-08-21): an UNCHANGED catalog must not bump
    the federation epoch or rebuild anything -- pre-fix, every TTL-expired
    refresh bumped unconditionally, and an ABSENT federation keeps the SHORT
    failure TTL, so a relay-less serve bumped every turn and every resident
    workspace fleet was evicted mid-turn on its next resolve (the
    ``SyncMCPToolExecutor is closed`` campaign kills)."""

    monkeypatch.setattr(relay_wiring, "create_sync_tool_executor", lambda *a, **k: object())
    monkeypatch.setattr(relay_wiring, "namespace_proxies", lambda gw: {})
    monkeypatch.setattr(relay_wiring, "list_relay_tool_definitions", lambda federation: {})

    agent = _FakeGatewayAgent()
    old_executor = agent.tool_executor
    surfaces = SimpleNamespace(
        remote_mcp_federation=None,
        jarvis_jobs=None,
        status={"configured": False, "reason": "relay_tools_not_configured"},
    )

    relay_wiring._refresh_agent_relay_tool_surfaces(agent, surfaces)

    assert getattr(agent, "_relay_federation_epoch", 0) == 0, "no epoch churn without a change"
    assert agent.gateway_builds == 0, "unchanged catalog must not rebuild the gateway"
    assert agent.tool_executor is old_executor, "unchanged catalog must not swap the executor"


def test_refresh_agent_relay_tool_surfaces_changed_catalog_still_bumps_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sabotage twin of the no-op guard: a genuinely CHANGED catalog (absent ->
    present with tools) still bumps the epoch and rebuilds (#1236 intact)."""

    monkeypatch.setattr(relay_wiring, "create_sync_tool_executor", lambda *a, **k: object())
    monkeypatch.setattr(relay_wiring, "namespace_proxies", lambda gw: {})
    monkeypatch.setattr(
        relay_wiring,
        "list_relay_tool_definitions",
        lambda federation: {} if federation is None else {"relay_wait": {"name": "relay_wait"}},
    )

    agent = _FakeGatewayAgent()
    surfaces = SimpleNamespace(
        remote_mcp_federation=object(),
        jarvis_jobs=None,
        status={"configured": True, "reason": None},
    )

    relay_wiring._refresh_agent_relay_tool_surfaces(agent, surfaces)

    assert getattr(agent, "_relay_federation_epoch", 0) == 1, "a real change must bump the epoch"
    assert agent.gateway_builds == 1
