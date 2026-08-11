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
from mcp.shared.exceptions import MCPError

from clio_agent import conf
from clio_agent.errors import (
    MCP_PROTOCOL_DOWNGRADED_TO_LEGACY,
    MCPUnsupportedProtocolVersionError,
)
from clio_agent.tools.mcp_connection_era import (
    classify_connection_era,
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
            raise MCPError(
                -32022, "server refuses 2026-07-28", {"requestedVersion": "2026-07-28"}
            )

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
