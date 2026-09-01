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
from pathlib import Path
from typing import Any

import pytest
from fastmcp_tasks.client_models import ClientGetTaskResult
from mcp.types import Tool as McpTool

from clio_agent.tools import relay_factory
from clio_agent.tools.mcp_task_records import TaskKey
from clio_agent.tools.relay_factory import (
    RelayTransportConfig,
    RelayTransportUnavailable,
    discover_relay_tool_surfaces,
    resolve_relay_cluster,
    resolve_relay_jarvis_door_namespace,
    resolve_relay_transport_config,
)
from clio_agent.tools.relay_transport import (
    OWNER_SESSION_ID_HEADER,
    RELAY_POLL_INTERVAL_MS,
    SESSION_GENERATION_ID_HEADER,
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
    monkeypatch.delenv("CLIO_RELAY_MCP_URL", raising=False)
    monkeypatch.delenv("CLIO_RELAY_HTTP_URL", raising=False)
    monkeypatch.delenv("CLIO_RELAY_API_TOKEN", raising=False)
    monkeypatch.delenv("CLIO_RELAY_OWNER_SESSION_ID", raising=False)
    monkeypatch.delenv("CLIO_RELAY_SESSION_GENERATION_ID", raising=False)


def _configure_relay_doors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the three always-required transport knobs, owner session left unset."""

    monkeypatch.setenv("CLIO_RELAY_MCP_URL", "http://127.0.0.1:18795/mcp")
    monkeypatch.setenv("CLIO_RELAY_HTTP_URL", "http://127.0.0.1:8795")
    monkeypatch.setenv("CLIO_RELAY_API_TOKEN", "token-alice")


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


def test_owner_session_identity_reaches_the_relay_api_request_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST: the relay HTTP API that serves artifact bytes is an OWNED
    SESSION API -- it answers every read with 403 ``exact owner session and
    generation headers are required`` unless the request carries them. The
    transport has always been able to send them; nothing ever resolved them from
    configuration, so ``relay_fetch_artifact`` could not reach that door at all.

    Proven on the header map the client actually sends (the same dict handed to
    both the httpx client and the MCP client), not on an intermediate config
    read."""

    _configure_relay_doors(monkeypatch)
    monkeypatch.setenv("CLIO_RELAY_OWNER_SESSION_ID", "  p5local-gate-1  ")
    monkeypatch.setenv("CLIO_RELAY_SESSION_GENERATION_ID", "  d2bf4aaa  ")

    resolved = resolve_relay_transport_config()
    assert isinstance(resolved, RelayTransportConfig)
    assert resolved.owner_session_id == "p5local-gate-1"
    assert resolved.owner_session_generation_id == "d2bf4aaa"

    headers = resolved.client()._headers
    assert headers[OWNER_SESSION_ID_HEADER] == "p5local-gate-1"
    assert headers[SESSION_GENERATION_ID_HEADER] == "d2bf4aaa"


def test_unset_owner_session_identity_sends_no_owner_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment whose relay API is not owned-session bound is unchanged: no
    owner headers are invented, and the transport stays configured."""

    _configure_relay_doors(monkeypatch)

    resolved = resolve_relay_transport_config()
    assert isinstance(resolved, RelayTransportConfig)
    assert resolved.owner_session_id == ""
    assert resolved.owner_session_generation_id == ""

    headers = resolved.client()._headers
    assert OWNER_SESSION_ID_HEADER not in headers
    assert SESSION_GENERATION_ID_HEADER not in headers


def test_explicit_owner_session_argument_still_wins_over_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured identity is a DEFAULT, never an override: a caller that
    binds its own owned session (the per-session seam) keeps that binding."""

    _configure_relay_doors(monkeypatch)
    monkeypatch.setenv("CLIO_RELAY_OWNER_SESSION_ID", "p5local-gate-1")
    monkeypatch.setenv("CLIO_RELAY_SESSION_GENERATION_ID", "d2bf4aaa")

    resolved = resolve_relay_transport_config()
    assert isinstance(resolved, RelayTransportConfig)

    headers = resolved.client(
        owner_session_id="other-session",
        owner_session_generation_id="other-generation",
    )._headers
    assert headers[OWNER_SESSION_ID_HEADER] == "other-session"
    assert headers[SESSION_GENERATION_ID_HEADER] == "other-generation"


@pytest.mark.parametrize(
    ("owner_session_id", "generation_id", "missing"),
    [
        ("p5local-gate-1", "", ["owner_session_generation_id"]),
        ("", "d2bf4aaa", ["owner_session_id"]),
    ],
)
def test_half_configured_owner_session_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
    owner_session_id: str,
    generation_id: str,
    missing: list[str],
) -> None:
    """FAILING-FIRST: a half-configured owned session must be a loud, queryable
    reason -- never a client that silently drops one header and then gets 403s
    from the relay API with no local explanation."""

    _configure_relay_doors(monkeypatch)
    if owner_session_id:
        monkeypatch.setenv("CLIO_RELAY_OWNER_SESSION_ID", owner_session_id)
    if generation_id:
        monkeypatch.setenv("CLIO_RELAY_SESSION_GENERATION_ID", generation_id)

    resolved = resolve_relay_transport_config()

    assert isinstance(resolved, RelayTransportUnavailable)
    assert resolved.reason == "relay_owner_session_identity_incomplete"
    assert resolved.details["missing"] == missing
    assert resolved.to_wire()["configured"] is False


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


# --------------------------------------------------------------------------- #
# M7 (review round 2, the ledger-wipe bug class): the relay-install job
# registry survives a #1227 D2 TTL-triggered catalog refresh.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ttl_refresh_construction_preserves_install_job_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST (M7): every #1227 D2 TTL-triggered relay catalog refresh
    (``gact/relay_wiring.py::refresh_relay_tool_surfaces_if_stale``) calls
    ``discover_relay_tool_surfaces()`` again, which used to construct a BRAND
    NEW ``RelayInstallSurface`` with its OWN fresh, empty job registry every
    time -- so a bootstrap job started against the FIRST-discovered surfaces
    went unreachable (``relay_install_job_not_found``) through the SECOND set
    of surfaces, even though its subprocess was still running.

    Proven end to end through the actual production entry point
    (``discover_relay_tool_surfaces``, not a private helper): start a job
    through surfaces #1, build surfaces #2 the exact same way the TTL refresh
    path does, and poll the SAME job_id through surfaces #2 while it is still
    non-terminal.
    """

    import asyncio
    import sys
    import textwrap
    import time

    py_path = tmp_path / "fake_relay_cli.py"
    py_path.write_text(
        textwrap.dedent(
            """
            import sys, time
            def main() -> int:
                time.sleep(0.6)
                sys.stdout.write('{"cluster": "demo", "installed": true}\\n')
                return 0
            if __name__ == "__main__":
                sys.exit(main())
            """
        ),
        encoding="utf-8",
    )
    if sys.platform.startswith("win"):
        executable = tmp_path / "fake_relay_cli.cmd"
        executable.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n', encoding="utf-8"
        )
    else:
        executable = tmp_path / "fake_relay_cli"
        executable.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{py_path}" "$@"\n', encoding="utf-8"
        )
        executable.chmod(0o755)

    monkeypatch.setenv("CLIO_RELAY_CLI_PATH", str(executable))
    monkeypatch.setattr(
        relay_factory,
        "resolve_relay_transport_config",
        lambda: RelayTransportUnavailable(
            reason="relay_not_configured", details={"missing": ["mcp_url"]}
        ),
    )

    # Surfaces #1: the FIRST discovery (boot, or the first configured turn).
    surfaces1 = await discover_relay_tool_surfaces()
    assert surfaces1.relay_install is not None
    started = await surfaces1.relay_install.invoke(
        "relay_cluster_bootstrap", {"action": "start", "cluster": "demo"}
    )
    assert started["terminal"] is False
    job_id = started["job_id"]

    # Surfaces #2: simulate the TTL refresh path -- the SAME production entry
    # point, called again, exactly like refresh_relay_tool_surfaces_if_stale
    # does once the catalog TTL elapses.
    surfaces2 = await discover_relay_tool_surfaces()
    assert surfaces2.relay_install is not None
    assert surfaces2.relay_install is not surfaces1.relay_install  # a genuinely NEW surface

    polled = await surfaces2.relay_install.invoke(
        "relay_cluster_bootstrap", {"action": "status", "job_id": job_id}
    )
    assert polled["job_id"] == job_id
    assert polled["error_reason"] != "relay_install_job_not_found"

    # Drain to terminal through surfaces #2 so no thread/subprocess dangles
    # past the test.
    deadline = time.monotonic() + 10.0
    while not polled["terminal"] and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        polled = await surfaces2.relay_install.invoke(
            "relay_cluster_bootstrap", {"action": "status", "job_id": job_id}
        )
    assert polled["terminal"] is True
    assert polled["state"] == "completed"


class TestRuntimeRelayCredentialContinuity:
    """A carried-over bearer credential must never follow a redirected endpoint.

    ``configure_runtime_relay`` reuses the currently resolved credential when
    ``api_token`` is omitted. Without an endpoint-continuity check, an
    unauthenticated ``PUT /v1/relay/configuration`` naming an attacker host
    installed the deployment's real token against that host, and every later
    relay MCP/HTTP call sent ``Authorization: Bearer <token>`` to it -- a
    credential the module promises stays process-local and that
    ``relay_connection_metadata`` deliberately withholds from the wire.
    """

    @pytest.fixture(autouse=True)
    def _isolated_override(self) -> "Any":
        relay_factory.reset_runtime_relay_override()
        relay_factory.configure_runtime_relay(
            mcp_url="https://relay.internal/mcp",
            http_url="https://relay.internal",
            api_token="T-secret",
        )
        yield
        relay_factory.reset_runtime_relay_override()

    def test_same_endpoint_still_reuses_the_held_credential(self) -> None:
        configured = relay_factory.configure_runtime_relay(
            mcp_url="https://relay.internal/mcp/v2",
            http_url="https://relay.internal",
        )
        assert configured.api_token == "T-secret"

    @pytest.mark.parametrize(
        ("mcp_url", "http_url"),
        [
            ("https://attacker.example/mcp", "https://attacker.example"),
            ("https://relay.internal/mcp", "https://attacker.example"),
            ("https://relay.internal:9443/mcp", "https://relay.internal"),
            ("http://relay.internal/mcp", "https://relay.internal"),
        ],
    )
    def test_redirecting_the_endpoint_without_a_credential_is_refused(
        self, mcp_url: str, http_url: str
    ) -> None:
        with pytest.raises(ValueError, match="relay_credential_endpoint_mismatch"):
            relay_factory.configure_runtime_relay(mcp_url=mcp_url, http_url=http_url)

        held = relay_factory.resolve_relay_transport_config()
        assert isinstance(held, RelayTransportConfig)
        assert held.mcp_url == "https://relay.internal/mcp"
        assert held.http_url == "https://relay.internal"
        assert held.api_token == "T-secret"

    def test_an_explicit_credential_may_point_anywhere(self) -> None:
        configured = relay_factory.configure_runtime_relay(
            mcp_url="https://other.example/mcp",
            http_url="https://other.example",
            api_token="T-other",
        )
        assert configured.api_token == "T-other"
        assert configured.mcp_url == "https://other.example/mcp"

    def test_a_blank_credential_is_treated_as_omitted(self) -> None:
        with pytest.raises(ValueError, match="relay_credential_endpoint_mismatch"):
            relay_factory.configure_runtime_relay(
                mcp_url="https://attacker.example/mcp",
                http_url="https://attacker.example",
                api_token="   ",
            )

    def test_a_first_time_connection_without_a_credential_still_reports_incomplete(
        self,
    ) -> None:
        relay_factory.disconnect_runtime_relay()
        with pytest.raises(ValueError, match="missing api_token"):
            relay_factory.configure_runtime_relay(
                mcp_url="https://relay.internal/mcp",
                http_url="https://relay.internal",
            )
