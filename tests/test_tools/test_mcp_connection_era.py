"""Typed era-downgrade reason on the MCP execution path (#1201).

The MCP client is fully v2, but ``tools.mcp.connect_mode=auto`` (the default)
can land on the LEGACY era under the #1186 race even when client and server
both speak 2026-07-28 -- and neither ``mcp_executor.py`` nor ``execution.py``
ever captured that fact. This module covers the pure auto/pinned
classification matrix in ``classify_connection_era``; connect-seam wiring
(``AsyncMCPToolExecutor.start`` / ``_route``) is covered separately once the
executor is wired to call it.

The companion ``_read_mcp_yaml`` swallow fix (scope item 3) is covered in
``tests/test_tools/test_mcp_config.py``.
"""

from __future__ import annotations

from clio_agent.errors import MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
from clio_agent.tools.mcp_connection_era import (
    classify_connection_era,
    recorded_mcp_connection_downgrades,
)

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


def test_unknown_protocol_version_is_never_a_downgrade() -> None:
    """Unset/unrecognized is not proven downgrade evidence, even under auto."""
    for protocol_version in (None, "", "not-a-real-version"):
        record = classify_connection_era(
            server_id="acme", protocol_version=protocol_version, connect_mode="auto"
        )
        assert record.era == "unknown"
        assert record.degrade_reason is None
