"""Handshake for declared MCP tool servers (the +MCP scope).

An MCP server is a "provider" of tools rather than models, so it gets its own
report shape (:class:`MCPServerReport`) instead of :class:`ModelProfile`. The
probe connects each declared server over its transport and lists its tools —
reusing the same ``Client(transport_for(spec))`` path the gateway uses — so a
single ``/v1/mcp/handshake`` (and ``/v1/health`` rows) can answer "is this tool
server reachable and what does it expose".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from clio_agent.providers.handshake.model import ConnectivityState

#: default per-server probe budget; spawning a stdio MCP (e.g. uvx) can be slow.
DEFAULT_MCP_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class MCPServerReport:
    """Connectivity + tool inventory for one declared MCP server."""

    name: str
    connectivity: ConnectivityState
    transport: str = ""
    tool_count: int = 0
    tools: tuple[str, ...] = ()
    error: str | None = None
    latency_ms: float | None = None

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
        return IntegrationStatus(
            name=f"mcp:{self.name}",
            state=state,
            summary=summary,
            config_source="handshake",
            next_action=("" if self.ok else f"Could not reach MCP server {self.name!r}."),
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
    try:
        from fastmcp import Client  # noqa: PLC0415

        from clio_agent.tools.mcp_config import transport_for  # noqa: PLC0415

        async def _list() -> list[Any]:
            async with Client(transport_for(spec)) as client:
                return await client.list_tools()

        tools = await asyncio.wait_for(_list(), timeout=timeout_s)
        names = tuple(sorted(getattr(t, "name", str(t)) for t in tools))
        return MCPServerReport(
            name=spec.name,
            connectivity=ConnectivityState.OK,
            transport=transport,
            tool_count=len(names),
            tools=names,
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except (TimeoutError, asyncio.TimeoutError):
        return MCPServerReport(
            name=spec.name,
            connectivity=ConnectivityState.TIMEOUT,
            transport=transport,
            error=f"did not respond within {timeout_s:g}s",
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in MCPServerReport.error
        return MCPServerReport(
            name=spec.name,
            connectivity=ConnectivityState.UNREACHABLE,
            transport=transport,
            error=str(exc)[:300],
            latency_ms=(time.monotonic() - started) * 1000.0,
        )


async def handshake_mcp_servers(
    specs: list[Any], *, timeout_s: float = DEFAULT_MCP_TIMEOUT_S
) -> list[MCPServerReport]:
    """Probe all declared MCP servers in parallel; one bad server never sinks the rest."""
    if not specs:
        return []
    return list(await asyncio.gather(*(_probe_one(s, timeout_s=timeout_s) for s in specs)))
