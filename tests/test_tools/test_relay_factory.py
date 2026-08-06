"""#1171 cluster-discovery gap: CLIO_RELAY_CLUSTER reaches curated tool descriptions.

``resolve_relay_cluster`` is the single seam both the relay placement path
(``clio_agent.gact.relay_wiring.configure_relay_expert_invokers``) and the
curated tool-definition builders (``JarvisJobs``, ``RemoteMcpFederation``) now
read the deployment's registered cluster identity through. These tests cover
the config precedence itself and the end-to-end wiring inside
``discover_relay_tool_surfaces`` -- proving the env value actually reaches the
built tool definitions' descriptions, not just an intermediate variable.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import Tool as McpTool

from clio_agent.tools import relay_factory
from clio_agent.tools.relay_factory import (
    RelayTransportUnavailable,
    discover_relay_tool_surfaces,
    resolve_relay_cluster,
)
from clio_agent.tools.relay_transport import RelayRemoteMcpCatalog


class _FakeRelayClient:
    """Minimal async-context-manager relay stub for the discovery boot path only."""

    def __init__(self, catalog: RelayRemoteMcpCatalog) -> None:
        self._catalog = catalog

    async def __aenter__(self) -> "_FakeRelayClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def discover_remote_mcp(self) -> RelayRemoteMcpCatalog:
        return self._catalog


class _FakeResolvedTransport:
    """Stands in for ``RelayTransportConfig`` -- only ``.client`` is exercised."""

    def __init__(self, catalog: RelayRemoteMcpCatalog) -> None:
        self._catalog = catalog

    def client(self, **_kwargs: Any) -> _FakeRelayClient:
        return _FakeRelayClient(self._catalog)


def _catalog() -> RelayRemoteMcpCatalog:
    return RelayRemoteMcpCatalog(
        revision="a" * 64,
        tools={},
        follow_tools={
            "relay_wait": McpTool(
                name="relay_wait",
                inputSchema={
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                },
                outputSchema={"type": "object"},
            )
        },
    )


@pytest.fixture(autouse=True)
def _clear_relay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests own their own CLIO_RELAY_CLUSTER; never inherit the real process env."""

    monkeypatch.delenv("CLIO_RELAY_CLUSTER", raising=False)


def test_resolve_relay_cluster_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAILING-FIRST: the deployment's cluster identity is real config, file -> env
    -> default -- not a box-local file an agent has to go hunting for."""

    assert resolve_relay_cluster() == ""
    monkeypatch.setenv("CLIO_RELAY_CLUSTER", "  ares-p5run2  ")
    assert resolve_relay_cluster() == "ares-p5run2"


@pytest.mark.asyncio
async def test_discover_relay_tool_surfaces_stamps_cluster_into_jarvis_and_relay_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST: with CLIO_RELAY_CLUSTER configured, the single boot seam
    (``discover_relay_tool_surfaces``) stamps it into every built jarvis_* tool
    description and relay_wait's -- proving the env value reaches the actual
    tool definitions the model sees, not just an intermediate config read."""

    monkeypatch.setenv("CLIO_RELAY_CLUSTER", "ares-p5run2")
    monkeypatch.setattr(
        relay_factory,
        "resolve_relay_transport_config",
        lambda: _FakeResolvedTransport(_catalog()),
    )

    surfaces = await discover_relay_tool_surfaces()

    assert surfaces.jarvis_jobs is not None
    jarvis_tool = await surfaces.jarvis_jobs.server.get_tool("create_pipeline")
    assert jarvis_tool is not None
    assert "This deployment's registered cluster is 'ares-p5run2'" in jarvis_tool.description
    assert "pass it as `cluster` verbatim" in jarvis_tool.description

    assert surfaces.remote_mcp_federation is not None
    follow_tool = await surfaces.remote_mcp_federation.follow_server.get_tool("wait")
    assert follow_tool is not None
    assert follow_tool.description == "This deployment's registered cluster is 'ares-p5run2'."


@pytest.mark.asyncio
async def test_discover_relay_tool_surfaces_unset_cluster_leaves_descriptions_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset CLIO_RELAY_CLUSTER -> no placeholder anywhere; this must never enable
    or alter placement either -- discovery only builds tool projections here."""

    monkeypatch.setattr(
        relay_factory,
        "resolve_relay_transport_config",
        lambda: _FakeResolvedTransport(_catalog()),
    )

    surfaces = await discover_relay_tool_surfaces()

    assert surfaces.jarvis_jobs is not None
    jarvis_tool = await surfaces.jarvis_jobs.server.get_tool("create_pipeline")
    assert jarvis_tool is not None
    assert "registered cluster" not in jarvis_tool.description

    assert surfaces.remote_mcp_federation is not None
    follow_tool = await surfaces.remote_mcp_federation.follow_server.get_tool("wait")
    assert follow_tool is not None
    assert not (follow_tool.description or "")


def test_relay_transport_unavailable_short_circuits_without_cluster_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured relay transport stays the typed degrade it already was --
    the cluster stamp is orthogonal and never masks a real relay_not_configured."""

    monkeypatch.setattr(
        relay_factory,
        "resolve_relay_transport_config",
        lambda: RelayTransportUnavailable(
            reason="relay_not_configured", details={"missing": ["mcp_url"]}
        ),
    )

    import asyncio

    surfaces = asyncio.run(discover_relay_tool_surfaces())
    assert surfaces.jarvis_jobs is None
    assert surfaces.remote_mcp_federation is None
    assert surfaces.status["reason"] == "relay_tools_not_configured"
