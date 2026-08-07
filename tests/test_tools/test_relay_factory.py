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

from collections.abc import Mapping
from typing import Any

import pytest
from fastmcp_tasks.client_models import ClientGetTaskResult
from mcp.types import Tool as McpTool

from clio_agent.tools import relay_factory
from clio_agent.tools.mcp_task_records import TaskKey
from clio_agent.tools.relay_factory import (
    RelayTransportUnavailable,
    discover_relay_tool_surfaces,
    resolve_relay_cluster,
    resolve_relay_jarvis_door_namespace,
)
from clio_agent.tools.relay_transport import (
    RELAY_POLL_INTERVAL_MS,
    RelayRemoteMcpCatalog,
    RelayTaskIdentity,
)


class _FakeRelayClient:
    """Minimal async-context-manager relay stub for the discovery boot path only.

    ``submit``/``poll`` additionally support one bounded JARVIS dispatch so the
    door-namespace wiring test below can drive a real ``JarvisJobs`` call and
    observe the exact door tool name it submits -- not just an intermediate
    config read.
    """

    def __init__(self, catalog: RelayRemoteMcpCatalog) -> None:
        self._catalog = catalog
        self.submitted: list[str] = []

    async def __aenter__(self) -> "_FakeRelayClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def discover_remote_mcp(self) -> RelayRemoteMcpCatalog:
        return self._catalog

    async def submit(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RelayTaskIdentity:
        del idempotency_key, timeout_seconds, arguments
        self.submitted.append(tool_name)
        return RelayTaskIdentity.from_key(
            TaskKey("fake-relay", "session-alice", f"job-{len(self.submitted)}")
        )

    async def poll(self, task: RelayTaskIdentity) -> ClientGetTaskResult:
        return ClientGetTaskResult(
            taskId=task.task_id,
            status="completed",
            createdAt="2026-08-06T00:00:00Z",
            lastUpdatedAt="2026-08-06T00:00:01Z",
            pollIntervalMs=RELAY_POLL_INTERVAL_MS,
            resultType="complete",
            result={"pipeline_id": "p", "created": True},
            error=None,
        )


class _FakeResolvedTransport:
    """Stands in for ``RelayTransportConfig`` -- only ``.client`` is exercised.

    ``client`` (an optional pre-built instance) is returned from every call
    instead of a fresh one when supplied, so a test can observe dispatches
    made across the multiple ``async with client_factory()`` blocks a real
    ``JarvisJobs`` call opens.
    """

    def __init__(
        self, catalog: RelayRemoteMcpCatalog, *, client: _FakeRelayClient | None = None
    ) -> None:
        self._catalog = catalog
        self._client = client

    def client(self, **_kwargs: Any) -> _FakeRelayClient:
        return self._client if self._client is not None else _FakeRelayClient(self._catalog)


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
    """Tests own their own relay env knobs; never inherit the real process env."""

    monkeypatch.delenv("CLIO_RELAY_CLUSTER", raising=False)
    monkeypatch.delenv("CLIO_RELAY_JARVIS_DOOR_NAMESPACE", raising=False)


def test_resolve_relay_cluster_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAILING-FIRST: the deployment's cluster identity is real config, file -> env
    -> default -- not a box-local file an agent has to go hunting for."""

    assert resolve_relay_cluster() == ""
    monkeypatch.setenv("CLIO_RELAY_CLUSTER", "  ares-p5run2  ")
    assert resolve_relay_cluster() == "ares-p5run2"


def test_resolve_relay_jarvis_door_namespace_defaults_to_the_registered_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST: the correct-shape local relay door projects the six curated
    JARVIS operations under the operator-registered route -- the compact aliases
    this surface was originally built against are ABSENT from its catalog. With
    no override, the resolved namespace must be the registered-route default."""

    assert resolve_relay_jarvis_door_namespace() == "remote_jarvis"


def test_resolve_relay_jarvis_door_namespace_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The door namespace is real config, file -> env -> default, mirroring
    ``resolve_relay_cluster``'s own precedence."""

    monkeypatch.setenv("CLIO_RELAY_JARVIS_DOOR_NAMESPACE", "  compact-route  ")
    assert resolve_relay_jarvis_door_namespace() == "compact-route"


def test_resolve_relay_jarvis_door_namespace_file_layer_empty_reproduces_the_compact_door() -> None:
    """The OLD compact door (the p5run2 evidence door used it) is reachable
    ONLY through the config FILE layer setting an explicit empty namespace --
    an empty env var is treated as unset (:mod:`clio_agent.conf` precedence),
    so this is the one way to opt into it, never a second hardcoded branch."""

    from tests._config_layer import set_config  # noqa: PLC0415

    set_config("relay.jarvis_door_namespace", "")
    assert resolve_relay_jarvis_door_namespace() == ""


@pytest.mark.asyncio
async def test_discover_relay_tool_surfaces_wires_the_resolved_door_namespace_into_jarvis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST: the config-resolved JARVIS door namespace must reach the
    actual dispatch the ``JarvisJobs`` instance ``discover_relay_tool_surfaces``
    builds performs -- not just an intermediate variable in this factory.
    Proven on the real dispatch path (the door tool name ``relay.submit``
    actually receives), the same seam a live tool-not-found rejection would
    surface."""

    monkeypatch.setenv("CLIO_RELAY_JARVIS_DOOR_NAMESPACE", "compact-route")
    catalog = _catalog()
    client = _FakeRelayClient(catalog)
    monkeypatch.setattr(
        relay_factory,
        "resolve_relay_transport_config",
        lambda: _FakeResolvedTransport(catalog, client=client),
    )

    surfaces = await discover_relay_tool_surfaces()
    assert surfaces.jarvis_jobs is not None

    await surfaces.jarvis_jobs.create_pipeline({"cluster": "ares", "pipeline_id": "p"})

    assert client.submitted == ["compact-route_jarvis_create_pipeline"]


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
