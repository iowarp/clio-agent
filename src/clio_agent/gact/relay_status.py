"""Relay endpoint configuration and bounded reachability probing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

RELAY_MCP_URL_ENV = "CLIO_RELAY_MCP_URL"
RELAY_PROBE_TIMEOUT_SECONDS = 3.0

RELAY_STATUS_REASONS: dict[str, dict[str, Any]] = {
    "relay_endpoint_invalid": {
        "category": "relay_configuration",
        "description": "The configured relay MCP URL has no resolvable network host.",
        "recovery_actions": ["set_clio_relay_mcp_url"],
    },
    "relay_tcp_unreachable": {
        "category": "relay_connectivity",
        "description": "The bounded TCP probe could not connect to the relay endpoint.",
        "recovery_actions": ["check_relay_service", "check_network_path"],
    },
}


@dataclass(frozen=True)
class RelayEndpoint:
    """Resolved relay endpoint configuration used by all status projections."""

    configured: bool
    host: str | None
    port: int | None


def resolve_relay_endpoint() -> RelayEndpoint:
    """Resolve relay network identity from the transport's MCP URL setting.

    Returns:
        The configured flag plus parsed host and effective TCP port. A non-empty
        but malformed URL remains configured with no host so status can report a
        typed configuration failure instead of silently treating it as absent.
    """
    from clio_agent.tools.relay_transport import (  # noqa: PLC0415
        RelayTransportUnavailable,
        resolve_relay_transport_config,
    )

    resolved = resolve_relay_transport_config()
    raw_url = "" if isinstance(resolved, RelayTransportUnavailable) else resolved.mcp_url
    if not raw_url:
        return RelayEndpoint(configured=False, host=None, port=None)
    try:
        parsed = urlsplit(raw_url)
        host = parsed.hostname
    except ValueError:
        return RelayEndpoint(configured=True, host=None, port=None)
    if host is None:
        return RelayEndpoint(configured=True, host=None, port=None)
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        port = None
    return RelayEndpoint(configured=True, host=host, port=port)


def relay_capabilities(runtime_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project endpoint configuration without performing network I/O.

    Returns:
        The additive capabilities relay block.
    """
    endpoint = resolve_relay_endpoint()
    if runtime_status is None:
        from clio_agent.tools.relay_transport import (  # noqa: PLC0415
            RelayTransportUnavailable,
            resolve_relay_transport_config,
        )

        resolved = resolve_relay_transport_config()
        runtime_status = (
            {
                **resolved.to_wire(),
                "reason": "relay_tools_not_configured",
            }
            if isinstance(resolved, RelayTransportUnavailable)
            else {"configured": True, "reason": None}
        )
    return {
        "configured": bool(runtime_status.get("configured", False)),
        "host": endpoint.host,
        "reason": runtime_status.get("reason"),
        **(
            {"details": dict(runtime_status["details"])}
            if isinstance(runtime_status.get("details"), dict)
            else {}
        ),
    }


async def _tcp_connect(host: str, port: int, timeout_seconds: float) -> None:
    """Open and close one bounded TCP connection.

    Args:
        host: Relay hostname parsed from the MCP URL.
        port: Effective relay TCP port.
        timeout_seconds: Maximum connection time.
    """
    _reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout_seconds
    )
    writer.close()
    await writer.wait_closed()


async def probe_relay_status() -> dict[str, Any]:
    """Probe the configured relay endpoint without guessing reachability.

    Returns:
        Relay configuration, reachability, probe time, and mechanism/error detail.
    """
    endpoint = resolve_relay_endpoint()
    from clio_agent.tools.relay_factory import relay_connection_metadata  # noqa: PLC0415
    from clio_agent.tools.relay_transport import (  # noqa: PLC0415
        RelayTransportUnavailable,
        resolve_relay_transport_config,
    )

    resolved = resolve_relay_transport_config()
    metadata = relay_connection_metadata()
    if isinstance(resolved, RelayTransportUnavailable):
        return {
            "configured": False,
            "host": endpoint.host,
            "reachable": None,
            "checked_at": None,
            "reason": "relay_tools_not_configured",
            "details": dict(resolved.details),
            "detail": "relay_tools_not_configured: relay transport configuration is incomplete",
            **metadata,
        }

    checked_at = datetime.now(timezone.utc).isoformat()
    if endpoint.host is None or endpoint.port is None:
        return {
            "configured": True,
            "host": None,
            "reachable": False,
            "checked_at": checked_at,
            "reason": "relay_endpoint_invalid",
            "detail": f"relay_endpoint_invalid: {RELAY_MCP_URL_ENV} has no valid host/port",
            **metadata,
        }

    target = f"{endpoint.host}:{endpoint.port}"
    try:
        await _tcp_connect(endpoint.host, endpoint.port, RELAY_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - exact probe error belongs on the API
        return {
            "configured": True,
            "host": endpoint.host,
            "reachable": False,
            "checked_at": checked_at,
            "reason": "relay_tcp_unreachable",
            "detail": f"relay_tcp_unreachable: TCP connect to {target} failed: {exc}",
            **metadata,
        }
    return {
        "configured": True,
        "host": endpoint.host,
        "reachable": True,
        "checked_at": checked_at,
        "reason": None,
        "detail": f"TCP connect to {target} succeeded",
        **metadata,
    }
