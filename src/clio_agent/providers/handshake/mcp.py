"""Handshake for declared MCP tool servers (the +MCP scope).

An MCP server is a "provider" of tools rather than models, so it gets its own
report shape (:class:`MCPServerReport`) instead of :class:`ModelProfile`. The
probe connects each declared server over its transport and lists its tools —
via the single ``make_mcp_client(transport_for(spec))`` factory (#1106) — so a
single ``/v1/mcp/handshake`` (and ``/v1/health`` rows) can answer "is this tool
server reachable and what does it expose".

#1201: this probe's OWN connect is instrumented (``server_id=spec.name``) like
any other direct execution-path connect, AND the report additionally surfaces
``execution_era`` -- the LATEST era actually observed for this server across
EVERY execution path (this probe, a direct dynamic-agent/REST call, or the
real backend leg behind the gateway proxy; see
:mod:`clio_agent.tools.mcp_connection_era`) -- so a real, silent auto-mode
downgrade from live traffic is visible here even when THIS diagnostic
connect happens to land on the modern era.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.providers.handshake.model import ConnectivityState

if TYPE_CHECKING:
    from clio_agent.tools.mcp_connection_era import MCPConnectionEra

#: default per-server probe budget; spawning a stdio MCP (e.g. uvx) can be slow.
DEFAULT_MCP_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class MCPServerReport:
    """Connectivity + tool inventory for one declared MCP server.

    On the 2026-07-28 wire the client probes ``server/discover`` on connect; the
    negotiated protocol era, the backend's own version, and its natural-language
    instructions are surfaced here so a handshake consumer can record *what*
    answered, not merely that something did (#1111).
    """

    name: str
    connectivity: ConnectivityState
    transport: str = ""
    tool_count: int = 0
    tools: tuple[str, ...] = ()
    error: str | None = None
    latency_ms: float | None = None
    protocol_version: str | None = None
    server_version: str | None = None
    instructions: str | None = None
    #: #1201: the LATEST era observed for this server across every execution
    #: path (not just this probe's own connect) -- None when never observed.
    execution_era: "MCPConnectionEra | None" = None

    @property
    def ok(self) -> bool:
        return self.connectivity == ConnectivityState.OK

    def to_integration_status(self) -> Any:
        """Render to a ``runtime.status.IntegrationStatus`` row."""
        from clio_agent.runtime.status import (  # noqa: PLC0415
            IntegrationState,
            IntegrationStatus,
        )

        if self.connectivity == ConnectivityState.SKIPPED:
            state = IntegrationState.SKIPPED
        elif self.connectivity == ConnectivityState.OK:
            state = IntegrationState.READY if self.tool_count else IntegrationState.DEGRADED
        else:
            state = IntegrationState.UNAVAILABLE
        summary = (
            f"{self.tool_count} tool(s)" if self.ok else (self.error or f"{self.name} unreachable")
        )
        details: dict[str, Any] = {"transport": self.transport, "tools": list(self.tools[:30])}
        if self.latency_ms is not None:
            details["latency_ms"] = round(self.latency_ms, 1)
        if self.protocol_version is not None:
            details["protocol_version"] = self.protocol_version
        if self.server_version is not None:
            details["server_version"] = self.server_version
        if self.instructions is not None:
            details["instructions"] = self.instructions
        # #1201: a REAL observed downgrade always surfaces, even when this probe's
        # own connect landed on modern -- doctor must show what live traffic did,
        # not just what the diagnostic connect happened to do just now.
        era = self.execution_era
        downgraded = era is not None and era.degrade_reason is not None
        if era is not None:
            details["execution_era"] = era.era
            details["execution_protocol_version"] = era.protocol_version
        next_action = "" if self.ok else f"Could not reach MCP server {self.name!r}."
        if downgraded:
            assert era is not None
            details["execution_downgrade_reason"] = era.degrade_reason
            state = IntegrationState.DEGRADED if state == IntegrationState.READY else state
            summary = f"{summary} (execution-path downgrade: {era.degrade_reason})"
            next_action = (
                "Investigate why this server's real traffic negotiated the legacy era "
                "under auto mode (the #1186 race); consider pinning tools.mcp.connect_mode."
            )
        return IntegrationStatus(
            name=f"mcp:{self.name}",
            state=state,
            summary=summary,
            config_source="handshake",
            next_action=next_action,
            capabilities=list(self.tools[:30]),
            details=details,
        )


async def _probe_one(spec: Any, *, timeout_s: float) -> MCPServerReport:
    """Connect one declared MCP server and list its tools; never raises."""
    transport = (
        "stdio" if getattr(spec, "transport", "") == "stdio" else getattr(spec, "transport", "")
    )
    if getattr(spec, "validation_errors", ()):  # malformed declaration — don't try to spawn
        return MCPServerReport(
            name=spec.name,
            connectivity=ConnectivityState.SKIPPED,
            transport=transport,
            error="; ".join(spec.validation_errors),
        )
    started = time.monotonic()
    # #1201: the latest era ANY execution path observed for this server, prior to
    # (and independent of) this probe's own connect below -- attached to every
    # branch so a real downgrade from live traffic is visible even when this
    # probe cannot reach the server at all right now.
    from clio_agent.tools.mcp_connection_era import latest_mcp_connection_era  # noqa: PLC0415

    execution_era = latest_mcp_connection_era(spec.name)
    try:
        from clio_agent.tools.mcp_config import transport_for  # noqa: PLC0415
        from clio_agent.tools.mcp_runtime import make_mcp_client  # noqa: PLC0415

        async def _list() -> tuple[list[Any], dict[str, Any]]:
            # server_id=spec.name: this diagnostic connect is ALSO a direct,
            # unmirrored execution-path connection -- classify + record it too.
            async with make_mcp_client(transport_for(spec), server_id=spec.name) as client:
                tools = await client.list_tools()
                # server/discover output, captured while the session is live.
                server_info = getattr(client, "server_info", None)
                discovered = {
                    "protocol_version": getattr(client, "protocol_version", None),
                    "server_version": getattr(server_info, "version", None),
                    "instructions": getattr(client, "instructions", None),
                }
                return tools, discovered

        tools, discovered = await asyncio.wait_for(_list(), timeout=timeout_s)
        names = tuple(sorted(getattr(t, "name", str(t)) for t in tools))
        return MCPServerReport(
            name=spec.name,
            connectivity=ConnectivityState.OK,
            transport=transport,
            tool_count=len(names),
            tools=names,
            latency_ms=(time.monotonic() - started) * 1000.0,
            protocol_version=discovered["protocol_version"],
            server_version=discovered["server_version"],
            instructions=discovered["instructions"],
            execution_era=latest_mcp_connection_era(spec.name),
        )
    except (TimeoutError, asyncio.TimeoutError):
        return MCPServerReport(
            name=spec.name,
            connectivity=ConnectivityState.TIMEOUT,
            transport=transport,
            error=f"did not respond within {timeout_s:g}s",
            latency_ms=(time.monotonic() - started) * 1000.0,
            execution_era=execution_era,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in MCPServerReport.error
        return MCPServerReport(
            name=spec.name,
            connectivity=ConnectivityState.UNREACHABLE,
            transport=transport,
            error=str(exc)[:300],
            latency_ms=(time.monotonic() - started) * 1000.0,
            execution_era=execution_era,
        )


async def handshake_mcp_servers(
    specs: list[Any], *, timeout_s: float = DEFAULT_MCP_TIMEOUT_S
) -> list[MCPServerReport]:
    """Probe all declared MCP servers in parallel; one bad server never sinks the rest."""
    if not specs:
        return []
    return list(await asyncio.gather(*(_probe_one(s, timeout_s=timeout_s) for s in specs)))
