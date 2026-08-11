"""Typed capture + degrade-reason classification for MCP connection era (#1201).

The MCP client is fully v2 (P1 campaign complete on develop), but the ``auto``
connect mode (:mod:`clio_agent.tools.mcp_runtime`) probes the modern
``server/discover`` wire first and falls back to the legacy ``initialize``
handshake when that probe loses the #1186 race (a slow first response --
cold uv env, matplotlib import, launcher cache-lock contention -- burns both
the probe and its one corrective re-probe). A server and client that BOTH
speak 2026-07-28 can therefore land on the legacy era anyway, purely from
timing, not capability. Neither ``mcp_executor.py`` nor ``execution.py`` ever
captured which era a connection actually landed on; only the diagnostic
``/v1/mcp/handshake`` probe recorded ``protocol_version``
(``providers/handshake/mcp.py``). A real execution-path connection silently
downgrading left no typed reason, log line, or trace entry -- the exact
no-silent-fallback violation this module closes.

This is the ONE place execution-path connect sites (the executor's
``start()`` / ``_route()`` / ``read_resource()`` in
:mod:`clio_agent.tools.mcp_executor`) stamp a :class:`MCPConnectionEra`
immediately after entering a client, mirroring the
``MCP_RESULT_DOWNGRADED_TO_COMPLETE`` (:mod:`clio_agent.tools.mcp_results`) /
``mcp_tasks_declaration_suppressed`` (:mod:`clio_agent.tools.mcp_task_extension`)
degrade-reason conventions already used in this package: a typed catalog
entry, never a silently absorbed fact.

Era determination reads the SDK's own protocol-version registry
(``mcp_types.version``) instead of re-deriving it: a version in
``MODERN_PROTOCOL_VERSIONS`` is "modern", one in ``HANDSHAKE_PROTOCOL_VERSIONS``
is "legacy", anything else (unset/unrecognized) is "unknown" -- never treated
as proven downgrade evidence.

A downgrade is recorded ONLY when ``connect_mode`` resolved to ``auto`` AND
the negotiated era is legacy. A PINNED mode (an explicit modern version, or
the literal ``"legacy"``) is operator intent: legitimate negotiation failure
under a pinned modern mode already raises the typed ``MCP_PROTOCOL_REFUSED``
path (:class:`clio_agent.errors.MCPUnsupportedProtocolVersionError`)
unchanged, and a pinned legacy connection landing on legacy is not a
downgrade at all.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Literal

from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from clio_agent.errors import MCP_PROTOCOL_DOWNGRADED_TO_LEGACY

logger = logging.getLogger(__name__)

ProtocolEra = Literal["modern", "legacy", "unknown"]


@dataclass(frozen=True)
class MCPConnectionEra:
    """One execution-path client connection's classified protocol era.

    The per-server runtime record scope item 1 asks for: stamped by
    :func:`classify_connection_era` right after a client is entered, and kept
    on the owning executor (``AsyncMCPToolExecutor.connection_era`` for the
    primary/composite connection, ``.namespace_connection_era(namespace)`` for
    a namespace-direct backend).
    """

    server_id: str
    protocol_version: str | None
    era: ProtocolEra
    connect_mode: str
    pinned: bool
    degrade_reason: str | None = None


def resolved_connect_mode() -> str:
    """Return the resolved ``tools.mcp.connect_mode`` value.

    Deliberately a SECOND, independent ``conf.resolve`` of the same key
    :func:`clio_agent.tools.mcp_runtime.make_mcp_client` already reads when
    building the client, rather than threading a new return value through
    ``make_mcp_client``'s many execution-path callers (#1106). It is a pure
    config read with no side effect -- config cannot change mid-connect -- so
    the two reads cannot desync, and this keeps the #1186-race-sensitive
    connect logic in ``mcp_runtime.py`` completely untouched.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return conf.resolve(
        "tools.mcp.connect_mode",
        env="CLIO_MCP_CONNECT_MODE",
        default="auto",
        cast=conf.as_str,
    )


def classify_connection_era(
    *, server_id: str, protocol_version: str | None, connect_mode: str
) -> MCPConnectionEra:
    """Classify one negotiated connection, recording a downgrade when real.

    ``protocol_version`` is read straight off ``client.protocol_version``
    after ``__aenter__`` (the SDK populates it for both eras: the
    ``initialize`` handshake, ``server/discover``, or a direct version pin).
    ``connect_mode`` is the resolved ``tools.mcp.connect_mode`` in effect for
    that same client (:func:`resolved_connect_mode`).

    "Pinned" mirrors :func:`clio_agent.tools.mcp_runtime.make_mcp_client`'s
    own pin check exactly (``connect_mode and connect_mode != "auto"``) so the
    two notions of "pinned" cannot drift apart.
    """

    if protocol_version in MODERN_PROTOCOL_VERSIONS:
        era: ProtocolEra = "modern"
    elif protocol_version in HANDSHAKE_PROTOCOL_VERSIONS:
        era = "legacy"
    else:
        era = "unknown"

    pinned = bool(connect_mode) and connect_mode != "auto"
    degrade_reason = (
        MCP_PROTOCOL_DOWNGRADED_TO_LEGACY if (not pinned and era == "legacy") else None
    )

    record = MCPConnectionEra(
        server_id=server_id,
        protocol_version=protocol_version,
        era=era,
        connect_mode=connect_mode,
        pinned=pinned,
        degrade_reason=degrade_reason,
    )
    if degrade_reason is not None:
        _record_downgrade(record)
    return record


#: Bounded, queryable in-process ring of real era downgrades -- mirrors
#: ``clio_agent.tools.execution._emit_tool_runtime_reason``'s ring: this
#: tools-layer module imports no gact and cannot reach the per-session
#: semantic-event trace directly, so it retains the same audit-sink pattern
#: every other tools-layer degrade reason in this package uses.
_DOWNGRADES: "deque[MCPConnectionEra]" = deque(maxlen=256)
_DOWNGRADES_LOCK = threading.Lock()


def _record_downgrade(record: MCPConnectionEra) -> None:
    """Append a real downgrade to the queryable ring and log it loudly."""

    with _DOWNGRADES_LOCK:
        _DOWNGRADES.append(record)
    logger.warning(
        "mcp connection downgraded to legacy era reason=%s server=%s protocol_version=%s "
        "connect_mode=%s",
        record.degrade_reason,
        record.server_id,
        record.protocol_version,
        record.connect_mode,
    )


def recorded_mcp_connection_downgrades() -> list[MCPConnectionEra]:
    """Return a snapshot of recorded era-downgrade records (queryable audit)."""

    with _DOWNGRADES_LOCK:
        return list(_DOWNGRADES)


__all__ = [
    "MCPConnectionEra",
    "ProtocolEra",
    "classify_connection_era",
    "recorded_mcp_connection_downgrades",
    "resolved_connect_mode",
]
