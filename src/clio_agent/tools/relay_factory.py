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

# The workspace UI may attach or detach a relay for the lifetime of the serving
# process.  Assignment is atomic under CPython and readers only ever receive an
# immutable value, so a turn sees either the complete old or complete new
# configuration.  Secrets remain process-local and are never projected on the
# GACT wire or written to the shared config store.
_runtime_relay_override: RelayTransportConfig | RelayTransportUnavailable | None = None


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
    #: This deployment's owned relay session, or ``""`` when the relay HTTP API
    #: is not owned-session bound. Both fields move together -- see
    #: :func:`resolve_relay_transport_config`.
    owner_session_id: str = ""
    owner_session_generation_id: str = ""

    def client(
        self,
        *,
        owner_session_id: str | None = None,
        owner_session_generation_id: str | None = None,
        session_id: str | None = None,
        store: TaskRecordStore | None = None,
    ) -> RelayTransportClient:
        """Construct one fresh owner-bound transport client.

        An explicitly supplied owned session wins; the configured identity is
        the DEFAULT applied when the caller binds none, which is what lets the
        boot-time tool surfaces (``relay_fetch_artifact`` above all) reach an
        owned-session relay API at all.
        """

        from clio_agent.tools.relay_transport import RelayTransportClient  # noqa: PLC0415

        if owner_session_id is None and owner_session_generation_id is None:
            owner_session_id = self.owner_session_id or None
            owner_session_generation_id = self.owner_session_generation_id or None

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
    #: clio-relay#209 A2: the local-CLI-subprocess cluster-lifecycle tool surface
    #: (register/bootstrap/status/session/proxy). Unlike ``jarvis_jobs`` /
    #: ``remote_mcp_federation`` this never depends on a reachable relay MCP/HTTP
    #: door -- it drives the DEPLOYED ``clio-relay`` executable directly, so it is
    #: built unconditionally in :func:`discover_relay_tool_surfaces` (never gated on
    #: ``resolve_relay_transport_config``). ``None`` only when the tool surface
    #: itself could not be constructed (never observed in practice today).
    relay_install: Any | None = None


def configure_runtime_relay(
    *, mcp_url: str, http_url: str, api_token: str | None = None
) -> RelayTransportConfig:
    """Apply a complete process-local relay connection.

    An omitted token reuses the currently resolved credential when one exists;
    a first-time connection must provide it.  The returned object is intended
    for internal wiring only and must never be serialized because it contains
    the bearer credential.

    Args:
        mcp_url: Authenticated relay control endpoint.
        http_url: Relay job and artifact endpoint.
        api_token: New bearer credential, or ``None`` to retain the current one.

    Returns:
        The immutable process-local transport configuration.

    Raises:
        ValueError: The connection is incomplete.
    """

    global _runtime_relay_override
    current = resolve_relay_transport_config()
    credential = (api_token or "").strip()
    if not credential and isinstance(current, RelayTransportConfig):
        credential = current.api_token
    values = {
        "mcp_url": mcp_url.strip(),
        "http_url": http_url.strip(),
        "api_token": credential,
    }
    missing = sorted(key for key, value in values.items() if not value)
    if missing:
        raise ValueError(f"relay connection is incomplete: missing {', '.join(missing)}")
    configured = RelayTransportConfig(**values)
    _runtime_relay_override = configured
    return configured


def disconnect_runtime_relay() -> None:
    """Disable relay access until the current agent process restarts."""

    global _runtime_relay_override
    _runtime_relay_override = RelayTransportUnavailable(
        reason="relay_not_configured",
        details={"missing": ["api_token", "http_url", "mcp_url"]},
    )


def reset_runtime_relay_override() -> None:
    """Restore server-managed resolution, primarily during app construction."""

    global _runtime_relay_override
    _runtime_relay_override = None


def relay_connection_metadata() -> dict[str, Any]:
    """Return non-secret connection fields for the management surface."""

    resolved = resolve_relay_transport_config()
    if isinstance(resolved, RelayTransportConfig):
        return {
            "mcp_url": resolved.mcp_url,
            "http_url": resolved.http_url,
            "credential_configured": True,
            "configuration_scope": (
                "agent_run" if _runtime_relay_override is not None else "server"
            ),
            "can_manage": True,
        }
    return {
        "mcp_url": None,
        "http_url": None,
        "credential_configured": False,
        "configuration_scope": ("agent_run" if _runtime_relay_override is not None else "none"),
        "can_manage": True,
    }


def resolve_relay_transport_config() -> RelayTransportConfig | RelayTransportUnavailable:
    """Resolve the relay factory through config file, environment, then defaults."""

    if _runtime_relay_override is not None:
        return _runtime_relay_override

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

    # The relay HTTP API can be an OWNED SESSION API -- the shape a desktop door
    # runs in, where the cluster serves job and artifact records through the
    # session ``session start`` created. That API answers every authenticated
    # read with "exact owner session and generation headers are required" unless
    # the request also carries this identity, so an unresolved identity means
    # the artifact-bytes door is simply unreachable. Both halves are one fact
    # and are refused together rather than half-sent.
    owner_session_id = conf.resolve(
        "relay.owner_session_id", env="CLIO_RELAY_OWNER_SESSION_ID", default="", cast=conf.as_str
    ).strip()
    owner_session_generation_id = conf.resolve(
        "relay.owner_session_generation_id",
        env="CLIO_RELAY_SESSION_GENERATION_ID",
        default="",
        cast=conf.as_str,
    ).strip()
    owner_values = {
        "owner_session_id": owner_session_id,
        "owner_session_generation_id": owner_session_generation_id,
    }
    if any(owner_values.values()) and not all(owner_values.values()):
        return RelayTransportUnavailable(
            reason="relay_owner_session_identity_incomplete",
            details={"missing": sorted(key for key, value in owner_values.items() if not value)},
        )

    return RelayTransportConfig(
        mcp_url=mcp_url,
        http_url=http_url,
        api_token=api_token,
        owner_session_id=owner_session_id,
        owner_session_generation_id=owner_session_generation_id,
    )


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


def resolve_relay_jarvis_door_namespace() -> str:
    """Resolve the relay-registered JARVIS door namespace, config -> env -> default.

    The correct-shape local relay door projects the six curated JARVIS
    operations under the OPERATOR-REGISTERED route
    (``remote_jarvis_jarvis_create_pipeline``, ...) rather than the compact
    aliases (``jarvis_create_pipeline``, ...) this surface was originally built
    against -- the compact names are ABSENT from that door's catalog, and only
    the registered route engages relay's input-staging contract. This is the
    single seam :class:`clio_agent.tools.jarvis_jobs.JarvisJobs` calls to
    resolve its door dispatch names
    (:func:`clio_agent.tools.jarvis_jobs.resolve_jarvis_door_tool_name`).
    Default ``"remote_jarvis"`` reproduces the new door; setting this to ``""``
    reproduces the OLD compact door verbatim (the evidence door used them) --
    expressed through config, not hardcoded as an alternate branch anywhere
    else. Never gates, degrades, or fabricates a value; only reads config.
    """

    from clio_agent import conf  # noqa: PLC0415 - keep transport import-light

    return conf.resolve(
        "relay.jarvis_door_namespace",
        env="CLIO_RELAY_JARVIS_DOOR_NAMESPACE",
        default="remote_jarvis",
        cast=conf.as_str,
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


def _build_relay_install_surface() -> Any:
    """Build the local-CLI cluster-lifecycle tool surface (clio-relay#209 A2).

    Independent of the relay MCP/HTTP transport: this surface drives the DEPLOYED
    ``clio-relay`` executable directly (subprocess, not the MCP door), so it is
    built even when ``resolve_relay_transport_config`` reports
    ``relay_not_configured``. Never raises: an unresolvable executable degrades to
    a typed ``relay_cli_unavailable`` status retained on the surface for
    diagnostics -- each tool call re-resolves the executable itself and fails
    typed at CALL time, so the executable appearing on PATH later needs no restart.

    M7 (review round 2, the ledger-wipe bug class): this function is called again
    every #1227 D2 TTL-triggered relay catalog refresh (``gact/relay_wiring.py``),
    which used to construct a BRAND NEW ``RelayInstallSurface`` with its OWN
    fresh, empty job registry each time -- so a bootstrap/session/proxy job
    started against the previously-discovered surfaces went unreachable
    (``relay_install_job_not_found``) the moment the catalog refreshed, even
    though its subprocess was still running. Fixed by threading the SAME
    process-wide job registry singleton through every rebuild here.
    """

    from clio_agent.tools.relay_cli_runner import (  # noqa: PLC0415
        RelayCliUnavailableError,
        resolve_relay_cli_executable,
    )
    from clio_agent.tools.relay_install_jobs import (  # noqa: PLC0415
        default_relay_install_job_registry,
    )
    from clio_agent.tools.relay_install_surface import RelayInstallSurface  # noqa: PLC0415

    try:
        executable = resolve_relay_cli_executable()
        status: dict[str, Any] = {"configured": True, "reason": None, "executable": executable}
    except RelayCliUnavailableError as exc:
        status = {"configured": False, "reason": exc.reason, "details": exc.details}
    return RelayInstallSurface(cli_status=status, job_registry=default_relay_install_job_registry())


async def discover_relay_tool_surfaces() -> RelayToolSurfaces:
    """Build the production JARVIS owner and discover the remote MCP catalog once."""

    relay_install = _build_relay_install_surface()

    resolved = resolve_relay_transport_config()
    if isinstance(resolved, RelayTransportUnavailable):
        return RelayToolSurfaces(
            None,
            None,
            {**resolved.to_wire(), "reason": "relay_tools_not_configured"},
            relay_install=relay_install,
        )

    from clio_agent.tools.jarvis_jobs import JarvisJobs  # noqa: PLC0415
    from clio_agent.tools.remote_mcp import RemoteMcpFederation  # noqa: PLC0415

    factory: Callable[[], AbstractAsyncContextManager[RelayTransportClient]] = resolved.client
    cluster_hint = resolve_relay_cluster() or None
    door_namespace = resolve_relay_jarvis_door_namespace()
    jarvis_jobs = JarvisJobs(factory, cluster_hint=cluster_hint, door_namespace=door_namespace)
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
        return RelayToolSurfaces(None, jarvis_jobs, status, relay_install=relay_install)
    return RelayToolSurfaces(
        federation, jarvis_jobs, {"configured": True, "reason": None}, relay_install=relay_install
    )
