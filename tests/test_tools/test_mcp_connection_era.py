"""Typed era-downgrade reason on the MCP execution path (#1201, failing-first).

The MCP client is fully v2, but ``tools.mcp.connect_mode=auto`` (the default)
can land on the LEGACY era under the #1186 race even when client and server
both speak 2026-07-28 -- and neither ``mcp_executor.py`` nor ``execution.py``
ever captured that fact. This module covers the fix: connect-time capture
(``AsyncMCPToolExecutor.start`` / ``_route``), the pure auto/pinned
classification matrix, and that the downgrade reaches a queryable record --
without changing negotiation itself (a pinned-modern refusal stays exactly
the existing typed ``MCP_PROTOCOL_REFUSED`` path).

The companion ``_read_mcp_yaml`` swallow fix (scope item 3) is covered in
``tests/test_tools/test_mcp_config.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from mcp.shared.exceptions import MCPError

from clio_agent import conf
from clio_agent.errors import (
    MCP_PROTOCOL_DOWNGRADED_TO_LEGACY,
    MCPUnsupportedProtocolVersionError,
)
from clio_agent.tools.mcp_connection_era import (
    MCPTaskCapability,
    all_latest_task_capabilities,
    classify_connection_era,
    instrument_client_era,
    latest_mcp_connection_era,
    latest_task_capability,
    record_task_capability,
    recorded_mcp_connection_downgrades,
)
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor


@pytest.fixture(autouse=True)
def _clean_connect_mode_env(monkeypatch):
    """Every test starts from the real default (``auto``), never an ambient override."""
    monkeypatch.delenv("CLIO_MCP_CONNECT_MODE", raising=False)
    conf.reload()
    yield


class _EraClient:
    """Minimal async client that reports a fixed negotiated ``protocol_version``."""

    def __init__(self, protocol_version: str | None) -> None:
        self.protocol_version = protocol_version

    async def __aenter__(self) -> "_EraClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return []

    async def read_resource(self, uri: str) -> Any:
        raise AssertionError(f"unexpected resource read: {uri}")


# --------------------------------------------------------------------------- #
# Pure classification matrix (scope items 1+2): classify_connection_era never
# touches I/O, so this is the cheap, exhaustive coverage of the auto/pinned
# decision independent of any executor wiring.
# --------------------------------------------------------------------------- #


def test_modern_under_auto_is_not_a_downgrade() -> None:
    record = classify_connection_era(
        server_id="acme", protocol_version="2026-07-28", connect_mode="auto"
    )
    assert record.era == "modern"
    assert record.pinned is False
    assert record.degrade_reason is None


def test_legacy_under_auto_is_a_downgrade_and_is_recorded() -> None:
    before = len(recorded_mcp_connection_downgrades())

    record = classify_connection_era(
        server_id="acme", protocol_version="2025-06-18", connect_mode="auto"
    )

    assert record.era == "legacy"
    assert record.pinned is False
    assert record.degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    after = recorded_mcp_connection_downgrades()
    assert len(after) == before + 1
    assert after[-1].server_id == "acme"
    assert after[-1].protocol_version == "2025-06-18"
    assert after[-1].degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY


def test_legacy_under_auto_also_calls_stream_audit(monkeypatch) -> None:
    """#1201 fix-round finding #1c: _record_downgrade must ALSO call
    stream_audit, matching the tool_runtime_reason precedent, not only log +
    ring."""
    calls: list[tuple[str, dict]] = []

    def _fake_stream_audit(stage: str, **fields) -> None:
        calls.append((stage, fields))

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", _fake_stream_audit)

    classify_connection_era(
        server_id="audited-server", protocol_version="2025-06-18", connect_mode="auto"
    )

    assert len(calls) == 1
    stage, fields = calls[0]
    assert stage == "mcp_connection_downgrade"
    assert fields["server_id"] == "audited-server"
    assert fields["reason"] == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    assert fields["protocol_version"] == "2025-06-18"


def test_legacy_under_pinned_legacy_mode_is_not_a_downgrade() -> None:
    """A pinned ``legacy`` mode is operator intent -- landing on legacy is expected."""
    record = classify_connection_era(
        server_id="acme", protocol_version="2025-06-18", connect_mode="legacy"
    )
    assert record.era == "legacy"
    assert record.pinned is True
    assert record.degrade_reason is None


def test_pinned_modern_mode_is_never_a_downgrade() -> None:
    """Any pinned mode (explicit version or ``legacy``) never emits a downgrade."""
    record = classify_connection_era(
        server_id="acme", protocol_version="2025-06-18", connect_mode="2026-07-28"
    )
    assert record.pinned is True
    assert record.degrade_reason is None


@pytest.mark.parametrize("protocol_version", [None, "", "not-a-real-version"])
def test_unknown_protocol_version_is_never_a_downgrade(protocol_version: str | None) -> None:
    """Unset/unrecognized is not proven downgrade evidence, even under auto."""
    record = classify_connection_era(
        server_id="acme", protocol_version=protocol_version, connect_mode="auto"
    )
    assert record.era == "unknown"
    assert record.degrade_reason is None


# --------------------------------------------------------------------------- #
# Executor connect-seam wiring (acceptance bullets 1-3): a fake server plugged
# in as the client_factory target, exercising start()/_route() for real.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_executor_stamps_downgrade_reason_under_auto_mode() -> None:
    """A fake server negotiating legacy under auto mode reaches the executor's
    per-server runtime record with server id + both eras + a typed reason."""

    client = _EraClient("2025-06-18")
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _server: client,
        server_id="acme",
    )
    await executor.start()
    try:
        era = executor.connection_era
        assert era is not None
        assert era.server_id == "acme"
        assert era.protocol_version == "2025-06-18"
        assert era.era == "legacy"
        assert era.connect_mode == "auto"
        assert era.degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_executor_pinned_legacy_mode_emits_no_degrade(monkeypatch) -> None:
    """Same fake server, but ``CLIO_MCP_CONNECT_MODE=legacy`` (operator intent):
    the era is still recorded, with no degrade reason."""

    monkeypatch.setenv("CLIO_MCP_CONNECT_MODE", "legacy")
    conf.reload()
    client = _EraClient("2025-06-18")
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _server: client,
        server_id="acme",
    )
    await executor.start()
    try:
        era = executor.connection_era
        assert era is not None
        assert era.era == "legacy"
        assert era.connect_mode == "legacy"
        assert era.pinned is True
        assert era.degrade_reason is None
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_pinned_modern_mode_protocol_refusal_is_unchanged(monkeypatch) -> None:
    """A pinned modern mode against a server that refuses it stays on the
    EXISTING typed MCP_PROTOCOL_REFUSED path (-32022) -- #1201 changes nothing
    here, and the era record is never stamped for a connection that failed."""

    monkeypatch.setenv("CLIO_MCP_CONNECT_MODE", "2026-07-28")
    conf.reload()

    class _RefusingClient(_EraClient):
        async def __aenter__(self) -> "_RefusingClient":
            raise MCPError(-32022, "server refuses 2026-07-28", {"requestedVersion": "2026-07-28"})

    client = _RefusingClient(None)
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _server: client,
        server_id="acme",
    )
    with pytest.raises(MCPUnsupportedProtocolVersionError):
        await executor.start()
    assert executor.connection_era is None


@pytest.mark.asyncio
async def test_namespace_direct_connect_stamps_its_own_server_id() -> None:
    """A namespace-direct backend (declared MCP server, routed via _route) gets
    its OWN per-server era record keyed by namespace -- distinct from the
    composite/primary connection's record."""

    composite_target = object()
    namespace_target = object()
    composite_client = _EraClient("2026-07-28")
    namespace_client = _EraClient("2025-06-18")

    def factory(target: object) -> _EraClient:
        return namespace_client if target is namespace_target else composite_client

    executor = AsyncMCPToolExecutor(
        composite_target,
        timeout=1.0,
        client_factory=factory,
        preloaded_tools={},
        namespace_servers={"vigil": namespace_target},
        server_id="composite",
    )
    await executor.start()
    try:
        assert executor.connection_era is not None
        assert executor.connection_era.era == "modern"
        # Not yet connected until a namespaced call routes to it.
        assert executor.namespace_connection_era("vigil") is None

        client, bare, namespace = await executor._route("vigil_open")  # noqa: SLF001

        assert client is namespace_client
        assert bare == "open"
        assert namespace == "vigil"
        vigil_era = executor.namespace_connection_era("vigil")
        assert vigil_era is not None
        assert vigil_era.server_id == "vigil"
        assert vigil_era.era == "legacy"
        assert vigil_era.degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    finally:
        await executor.aclose()


# --------------------------------------------------------------------------- #
# instrument_client_era (#1201 fix-round finding #0): a REAL regression class
# for the bug the gateway-leg fix caught -- Python resolves dunder methods
# invoked via syntax (``async with obj:``) on type(obj), never on the
# instance, so an instance-level ``__aenter__`` override is silently never
# called by that syntax. Every direct-connect call site this package wires
# (elicitation_bridge.py, routes/mcp.py, gateway.py's proxy backend) uses
# EITHER style depending on the caller, so instrument_client_era must work
# for both -- proven directly here without needing a real subprocess.
# --------------------------------------------------------------------------- #


class _AsyncWithOnlyClient:
    """A client entered ONLY via ``async with`` -- never an explicit
    ``.__aenter__()`` call. Reproduces exactly how ``tools/gateway.py``'s
    proxy machinery and several direct-connect call sites (elicitation_bridge,
    the REST dispatch in routes/mcp.py) actually enter their client."""

    def __init__(self, protocol_version: str) -> None:
        self.protocol_version = protocol_version
        self.entered = False

    async def __aenter__(self) -> "_AsyncWithOnlyClient":
        self.entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_instrument_client_era_fires_through_async_with_syntax() -> None:
    """The class-swap approach (not an instance __aenter__ patch) must be
    observed by the ``async with`` statement, which resolves __aenter__ via
    type(obj), bypassing any instance-level override."""

    client = instrument_client_era(_AsyncWithOnlyClient("2025-06-18"), server_id="via-async-with")

    async with client:
        assert client.entered is True

    era = latest_mcp_connection_era("via-async-with")
    assert era is not None
    assert era.era == "legacy"
    assert era.degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY


@pytest.mark.asyncio
async def test_instrument_client_era_also_fires_through_explicit_call() -> None:
    """The other invocation style (AsyncMCPToolExecutor's own
    ``await client_ctx.__aenter__()``) must keep working too."""

    client = instrument_client_era(_AsyncWithOnlyClient("2026-07-28"), server_id="via-explicit")

    await client.__aenter__()

    era = latest_mcp_connection_era("via-explicit")
    assert era is not None
    assert era.era == "modern"
    assert era.degrade_reason is None


@pytest.mark.asyncio
async def test_instrument_client_era_does_not_cross_contaminate_instances() -> None:
    """Two instances of the SAME class, instrumented with different server
    ids, must classify independently (the composed subclass is cached and
    shared; only the per-instance server_id attribute may differ)."""

    client_a = instrument_client_era(_AsyncWithOnlyClient("2026-07-28"), server_id="server-a")
    client_b = instrument_client_era(_AsyncWithOnlyClient("2025-06-18"), server_id="server-b")

    async with client_a:
        pass
    async with client_b:
        pass

    era_a = latest_mcp_connection_era("server-a")
    era_b = latest_mcp_connection_era("server-b")
    assert era_a is not None and era_a.era == "modern"
    assert era_b is not None and era_b.era == "legacy" and era_b.degrade_reason is not None


@pytest.mark.asyncio
async def test_instrument_client_era_never_mutates_the_original_class() -> None:
    """Swapping client.__class__ must never leak onto sibling instances of the
    same ORIGINAL class that were never instrumented."""

    plain = _AsyncWithOnlyClient("2026-07-28")
    instrumented = instrument_client_era(_AsyncWithOnlyClient("2026-07-28"), server_id="isolated")

    assert type(plain) is _AsyncWithOnlyClient
    assert type(instrumented) is not _AsyncWithOnlyClient
    async with plain:
        pass
    assert (
        latest_mcp_connection_era("isolated") is None
    )  # plain's connect never classified anything


# --------------------------------------------------------------------------- #
# #1281 (C1-S1): the task-capability registry -- a SECOND, separate write     #
# path (record_task_capability / latest_task_capability), pure and I/O-free.  #
# --------------------------------------------------------------------------- #


def test_record_task_capability_is_queryable_by_server_id() -> None:
    record = record_task_capability("cap-server-a", task_capable=True, source="tool_execution")

    assert record == MCPTaskCapability(
        server_id="cap-server-a", task_capable=True, source="tool_execution"
    )
    assert latest_task_capability("cap-server-a") == record


def test_latest_task_capability_is_unknown_before_any_record() -> None:
    assert latest_task_capability("never-recorded-capability-server") is None


def test_record_task_capability_always_overwrites_the_prior_verdict() -> None:
    """Mirrors ``_record_latest``: the surface answers "what do we know right
    now", not "has it ever been true" -- a later record replaces the earlier
    one for the SAME server_id."""

    record_task_capability("cap-server-b", task_capable=True, source="capabilities_extensions")
    second = record_task_capability("cap-server-b", task_capable=False, source="none")

    assert latest_task_capability("cap-server-b") == second
    assert latest_task_capability("cap-server-b").task_capable is False


def test_all_latest_task_capabilities_snapshots_every_server() -> None:
    record_task_capability("cap-snapshot-a", task_capable=True, source="tool_execution")
    record_task_capability("cap-snapshot-b", task_capable=False, source="none")

    snapshot = all_latest_task_capabilities()

    assert snapshot["cap-snapshot-a"].task_capable is True
    assert snapshot["cap-snapshot-b"].task_capable is False


# --------------------------------------------------------------------------- #
# #1281 (C1-S1): the opportunistic POSITIVE-only capture piggybacked on the   #
# SAME __aenter__ seam era classification uses (instrument_client_era).      #
# --------------------------------------------------------------------------- #


class _CapabilitiesStub:
    """Minimal ``ServerCapabilities``-shaped stub carrying only ``extensions``."""

    def __init__(self, extensions: dict[str, Any] | None) -> None:
        self.extensions = extensions


class _CapabilityAwareClient(_EraClient):
    """An ``_EraClient`` that also reports SERVER-declared capabilities."""

    def __init__(
        self, protocol_version: str | None, extensions: dict[str, Any] | None = None
    ) -> None:
        super().__init__(protocol_version)
        self.server_capabilities = _CapabilitiesStub(extensions)


@pytest.mark.asyncio
async def test_instrumented_connect_opportunistically_records_a_positive_capability() -> None:
    """A real connect whose server-declared extensions carry the tasks id
    records a positive verdict on the SAME __aenter__ seam era uses -- the
    seam that makes ``test_declared_path_serves_task_required_tools`` pass
    without a preceding ``_list_declared_tools`` discovery pass."""

    client = instrument_client_era(
        _CapabilityAwareClient("2026-07-28", extensions={TASKS_EXTENSION_ID: {}}),
        server_id="opportunistic-positive",
    )

    async with client:
        pass

    capability = latest_task_capability("opportunistic-positive")
    assert capability is not None
    assert capability.task_capable is True
    assert capability.source == "capabilities_extensions"


@pytest.mark.asyncio
async def test_instrumented_connect_never_records_a_negative_capability() -> None:
    """Absence of the tasks id at a bare connect is NOT proof of incapability
    (a legacy server's capability rides its listing, not a bare connect) --
    this seam must leave the registry untouched, never write False."""

    assert latest_task_capability("opportunistic-silent") is None
    client = instrument_client_era(
        _CapabilityAwareClient("2026-07-28", extensions={}), server_id="opportunistic-silent"
    )

    async with client:
        pass

    assert latest_task_capability("opportunistic-silent") is None


@pytest.mark.asyncio
async def test_instrumented_connect_never_clobbers_an_earlier_true_verdict() -> None:
    """A definitive True (from ``_list_declared_tools``, say) must survive a
    LATER opportunistic connect that happens to carry no extensions (e.g. a
    proxy front's re-advertisement) -- this seam writes positives only."""

    record_task_capability("opportunistic-sticky", task_capable=True, source="tool_execution")
    client = instrument_client_era(
        _CapabilityAwareClient("2026-07-28", extensions={}), server_id="opportunistic-sticky"
    )

    async with client:
        pass

    capability = latest_task_capability("opportunistic-sticky")
    assert capability is not None
    assert capability.task_capable is True
    assert capability.source == "tool_execution"  # unchanged, not clobbered
