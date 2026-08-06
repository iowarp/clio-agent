"""Production configuration and tool-surface factory for clio-relay."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.tools.mcp_task_records import TaskRecordStore
    from clio_agent.tools.relay_transport import RelayTransportClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelayTransportUnavailable:
    """Typed reason returned when the two-door relay transport is incomplete."""

    reason: str
    details: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        """Return the queryable no-silent-fallback status shape."""

        return {"configured": False, "reason": self.reason, "details": dict(self.details)}


@dataclass(frozen=True)
class RelayTransportConfig:
    """Resolved production configuration for both authenticated relay doors."""

    mcp_url: str
    http_url: str
    api_token: str

    def client(
        self,
        *,
        owner_session_id: str | None = None,
        owner_session_generation_id: str | None = None,
        session_id: str | None = None,
        store: TaskRecordStore | None = None,
    ) -> RelayTransportClient:
        """Construct one fresh owner-bound transport client."""

        from clio_agent.tools.relay_transport import RelayTransportClient  # noqa: PLC0415

        return RelayTransportClient(
            self.mcp_url,
            self.http_url,
            api_token=self.api_token,
            owner_session_id=owner_session_id,
            owner_session_generation_id=owner_session_generation_id,
            session_id=session_id,
            store=store,
        )


@dataclass(frozen=True)
class RelayToolSurfaces:
    """Production relay tool owners plus their queryable boot status."""

    remote_mcp_federation: Any | None
    jarvis_jobs: Any | None
    status: dict[str, Any]


def resolve_relay_transport_config() -> RelayTransportConfig | RelayTransportUnavailable:
    """Resolve the relay factory through config file, environment, then defaults."""

    from clio_agent import conf  # noqa: PLC0415 - keep transport import-light

    mcp_url = conf.resolve(
        "relay.mcp_url", env="CLIO_RELAY_MCP_URL", default="", cast=conf.as_str
    ).strip()
    http_url = conf.resolve(
        "relay.http_url", env="CLIO_RELAY_HTTP_URL", default="", cast=conf.as_str
    ).strip()
    api_token = os.getenv("CLIO_RELAY_API_TOKEN", "").strip()
    values = {"mcp_url": mcp_url, "http_url": http_url, "api_token": api_token}
    missing = sorted(key for key, value in values.items() if not value)
    if missing:
        return RelayTransportUnavailable(
            reason="relay_not_configured", details={"missing": missing}
        )
    return RelayTransportConfig(mcp_url=mcp_url, http_url=http_url, api_token=api_token)


def resolve_relay_cluster() -> str:
    """Resolve this deployment's registered relay cluster identity, config -> env -> default.

    Reads the same ``relay.cluster`` / ``CLIO_RELAY_CLUSTER`` knob the relay
    placement path already consulted
    (:func:`clio_agent.gact.relay_wiring.configure_relay_expert_invokers`) --
    this is now the single seam both that path and the curated tool-definition
    builders (:class:`clio_agent.tools.jarvis_jobs.JarvisJobs`,
    :class:`clio_agent.tools.remote_mcp.RemoteMcpFederation`) call, so the
    value is parsed once. Returns ``""`` when unset; callers decide what unset
    means for their own surface -- this function never gates, degrades, or
    fabricates a value, it only reads config.
    """

    from clio_agent import conf  # noqa: PLC0415 - keep transport import-light

    return conf.resolve(
        "relay.cluster", env="CLIO_RELAY_CLUSTER", default="", cast=conf.as_str
    ).strip()


def relay_transport_from_env(
    *,
    owner_session_id: str | None = None,
    owner_session_generation_id: str | None = None,
    session_id: str | None = None,
    store: TaskRecordStore | None = None,
) -> RelayTransportClient | RelayTransportUnavailable:
    """Build a configured client or return the typed ``relay_not_configured`` reason."""

    resolved = resolve_relay_transport_config()
    if isinstance(resolved, RelayTransportUnavailable):
        return resolved
    return resolved.client(
        owner_session_id=owner_session_id,
        owner_session_generation_id=owner_session_generation_id,
        session_id=session_id,
        store=store,
    )


async def discover_relay_tool_surfaces() -> RelayToolSurfaces:
    """Build the production JARVIS owner and discover the remote MCP catalog once."""

    resolved = resolve_relay_transport_config()
    if isinstance(resolved, RelayTransportUnavailable):
        return RelayToolSurfaces(
            None,
            None,
            {**resolved.to_wire(), "reason": "relay_tools_not_configured"},
        )

    from clio_agent.tools.jarvis_jobs import JarvisJobs  # noqa: PLC0415
    from clio_agent.tools.remote_mcp import RemoteMcpFederation  # noqa: PLC0415

    factory: Callable[[], AbstractAsyncContextManager[RelayTransportClient]] = resolved.client
    cluster_hint = resolve_relay_cluster() or None
    jarvis_jobs = JarvisJobs(factory, cluster_hint=cluster_hint)
    try:
        federation = await RemoteMcpFederation.discover(factory, cluster_hint=cluster_hint)
    except Exception as exc:  # noqa: BLE001 - queryable typed boot degrade
        status = {
            "configured": True,
            "reason": "relay_catalog_discovery_failed",
            "details": {"error": type(exc).__name__, "message": str(exc)},
        }
        logger.warning(
            "relay catalog discovery degraded reason=relay_catalog_discovery_failed error=%r",
            exc,
        )
        return RelayToolSurfaces(None, jarvis_jobs, status)
    return RelayToolSurfaces(federation, jarvis_jobs, {"configured": True, "reason": None})
