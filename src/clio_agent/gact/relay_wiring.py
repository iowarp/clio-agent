"""Production relay owners installed at GACT and agent assembly boundaries."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.relay_expert_invoker import RelayExpertInvoker
from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import namespace_proxies

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: #1227 D2 default: how long a discovered relay catalog is trusted before the
#: next turn re-discovers it. The old behavior discovered ONCE at boot and
#: cached forever (``app.state.relay_tool_surfaces`` set once, never
#: invalidated), so a tool that appeared on the door later (e.g. the jarvis
#: surface landing after a dev-mode relay update) stayed invisible until a
#: full process restart -- and the failure surfaced as
#: ``custom_agent_tools_unavailable`` / ``not_implemented``, a MISLEADING
#: reason (the truth was catalog staleness, not tool unavailability).
RELAY_TOOL_SURFACES_DEFAULT_TTL_SECONDS = 300


def _relay_tool_surfaces_ttl_seconds() -> float:
    """Resolve the catalog re-discovery TTL, config -> env -> default."""

    from clio_agent import conf  # noqa: PLC0415 - keep this module import-light

    return float(
        conf.resolve(
            "relay.tool_surfaces_ttl_seconds",
            env="CLIO_RELAY_TOOL_SURFACES_TTL_SECONDS",
            default=RELAY_TOOL_SURFACES_DEFAULT_TTL_SECONDS,
            cast=conf.as_int,
        )
    )


async def relay_tool_surfaces_for_app(app: FastAPI) -> Any:
    """Discover and retain the one production relay tool projection for an app."""

    existing = getattr(app.state, "relay_tool_surfaces", None)
    if existing is not None:
        return existing
    from clio_agent.tools.relay_transport import discover_relay_tool_surfaces  # noqa: PLC0415

    surfaces = await discover_relay_tool_surfaces()
    app.state.relay_tool_surfaces = surfaces
    app.state.relay_tool_status = dict(surfaces.status)
    app.state.relay_tool_surfaces_discovered_at = time.monotonic()
    return surfaces


async def refresh_relay_tool_surfaces_if_stale(app: FastAPI) -> Any:
    """Re-discover the relay catalog once its TTL has elapsed (#1227 D2).

    First discovery happens here ONLY for an explicitly configured relay
    transport: under the #1232 lazy boot nothing discovers eagerly anymore, so
    the first turn is the construction moment (observed live: with the
    unconditional no-op, the #1229 no-ambient-discovery rule and the #1232
    lazy boot composed into NOBODY ever discovering — every custom-agent ACL
    bricked typed on custom_agent_tools_unavailable, L3 runs 4-5). The
    ambient-poison case #1229 fixed stays dead: an UNCONFIGURED transport
    (typed RelayTransportUnavailable, e.g. env leaked mid-process on a box
    with no relay) is still a no-op — a turn never pays an ambient probe. On the common path this
    is one ``time.monotonic()`` subtraction unless the TTL has elapsed. When it has, the catalog is re-discovered and, critically, the
    already-constructed singleton agent (``app.state.agent`` -- one host per
    process, reused turn to turn) has its relay-owned attributes updated IN
    PLACE: ``ClioAgent._build_gateway`` reads ``self._remote_mcp_federation`` /
    ``self._jarvis_jobs`` / ``self._relay_status`` fresh on every gateway
    build (per turn, per workspace), so this takes effect on the NEXT turn
    with no restart. Never raises: a failed re-discovery degrades to the
    typed ``relay_catalog_discovery_failed`` reason
    :func:`~clio_agent.tools.relay_factory.discover_relay_tool_surfaces`
    already produces, leaving the previous (still-cached) surfaces in place
    rather than tearing down a working catalog over one transient probe.
    """

    existing = getattr(app.state, "relay_tool_surfaces", None)
    if existing is None:
        from clio_agent.tools.relay_transport import (  # noqa: PLC0415
            RelayTransportUnavailable,
            resolve_relay_transport_config,
        )

        if isinstance(resolve_relay_transport_config(), RelayTransportUnavailable):
            return None
        surfaces = await relay_tool_surfaces_for_app(app)
        # Push onto the LIVE agent exactly like the TTL path below: under the
        # #1232 lazy boot the agent was constructed without relay kwargs, so
        # filling app.state alone leaves its gateway toolless and every
        # custom-agent ACL bricks (L3 run 6: identical
        # custom_agent_tools_unavailable AFTER first discovery succeeded).
        agent = getattr(app.state, "agent", None)
        if agent is not None and surfaces is not None:
            _refresh_agent_relay_tool_surfaces(agent, surfaces)
        return surfaces

    discovered_at = getattr(app.state, "relay_tool_surfaces_discovered_at", None)
    ttl = _relay_tool_surfaces_ttl_seconds()
    if isinstance(discovered_at, (int, float)) and (time.monotonic() - discovered_at) < ttl:
        return existing

    from clio_agent.tools.relay_transport import discover_relay_tool_surfaces  # noqa: PLC0415

    try:
        surfaces = await discover_relay_tool_surfaces()
    except Exception as exc:  # noqa: BLE001 - degrade to the still-cached surfaces
        logger.warning(
            "relay catalog refresh failed, keeping the previously discovered "
            "surfaces reason=relay_catalog_refresh_failed error=%r",
            exc,
        )
        # Push the TTL clock forward anyway so a persistently unreachable
        # relay does not retry on every single turn.
        app.state.relay_tool_surfaces_discovered_at = time.monotonic()
        return existing

    app.state.relay_tool_surfaces = surfaces
    app.state.relay_tool_status = dict(surfaces.status)
    app.state.relay_tool_surfaces_discovered_at = time.monotonic()

    agent = getattr(app.state, "agent", None)
    if agent is not None:
        _refresh_agent_relay_tool_surfaces(agent, surfaces)
    return surfaces


def _refresh_agent_relay_tool_surfaces(agent: Any, surfaces: Any) -> None:
    """Rebuild the singleton agent's default gateway from a fresh catalog.

    agent.py stays a pure runtime HOST with no relay-refresh method of its
    own (RULE: no accretion onto that file) -- this owner-module function
    does the rebuild directly. Built fully off to the side; ``tool_executor``
    is reassigned LAST (one atomic ``STORE_ATTR``), so a concurrent turn
    reading it sees either the fully-old or fully-new executor, never a torn
    mix. Per-workspace executors are not touched: each already reads
    ``agent._remote_mcp_federation`` fresh on its next reaper-evicted rebuild.
    """

    agent._remote_mcp_federation = surfaces.remote_mcp_federation  # noqa: SLF001
    agent._jarvis_jobs = surfaces.jarvis_jobs  # noqa: SLF001
    agent._relay_status = dict(surfaces.status)  # noqa: SLF001
    gateway = agent._build_tool_gateway(set_catalog=True)  # noqa: SLF001
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=agent._tool_definitions,  # noqa: SLF001
        namespace_servers=namespace_proxies(gateway),
        server_id="gateway:default",
    )
    agent._tool_gateway = gateway  # noqa: SLF001
    agent.tool_executor = executor


async def relay_agent_kwargs(app: FastAPI) -> dict[str, Any]:
    """Return the relay-owned constructor arguments for one ClioAgent host."""

    surfaces = await relay_tool_surfaces_for_app(app)
    return {
        "remote_mcp_federation": surfaces.remote_mcp_federation,
        "jarvis_jobs": surfaces.jarvis_jobs,
        "relay_status": surfaces.status,
    }


async def construct_agent_with_relay(app: FastAPI, *, arc: Any) -> Any:
    """Construct a first-time provider-bound agent with the retained relay owners."""

    from clio_agent.agent import ClioAgent  # noqa: PLC0415

    relay_kwargs = await relay_agent_kwargs(app)
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: ClioAgent(verbose=False, arc=arc, **relay_kwargs)
    )


def configure_relay_expert_invokers(app: FastAPI) -> None:
    """Publish configured ``relay:<cluster>`` placement owners at app assembly."""

    from clio_agent import conf  # noqa: PLC0415
    from clio_agent.tools.relay_factory import resolve_relay_cluster  # noqa: PLC0415
    from clio_agent.tools.relay_transport import (  # noqa: PLC0415
        RelayTransportUnavailable,
        resolve_relay_transport_config,
    )

    resolved = resolve_relay_transport_config()
    if isinstance(resolved, RelayTransportUnavailable):
        app.state.relay_expert_invokers = {}
        app.state.relay_runtime_status = {
            **resolved.to_wire(),
            "reason": "relay_tools_not_configured",
        }
        return
    cluster = resolve_relay_cluster()
    prompt_path = conf.resolve(
        "relay.remote_agent.prompt_path",
        env="CLIO_RELAY_REMOTE_AGENT_PROMPT_PATH",
        default="",
        cast=conf.as_str,
    ).strip()
    missing = sorted(
        name
        for name, value in {"cluster": cluster, "remote_agent_prompt_path": prompt_path}.items()
        if not value
    )
    if missing:
        app.state.relay_expert_invokers = {}
        app.state.relay_runtime_status = {
            "configured": True,
            "reason": "relay_spawn_not_configured",
            "details": {"missing": missing},
        }
        logger.warning(
            "relay placement degraded reason=relay_spawn_not_configured missing=%s",
            ",".join(missing),
        )
        return

    def client_factory(parent_session_id: str) -> Any:
        return resolved.client(session_id=parent_session_id)

    app.state.relay_expert_invokers = {
        cluster: RelayExpertInvoker(
            app,
            client_factory,
            cluster=cluster,
            prompt_path=prompt_path,
            mcp_config_path=conf.resolve(
                "relay.remote_agent.mcp_config_path",
                env="CLIO_RELAY_REMOTE_AGENT_MCP_CONFIG_PATH",
                default="",
                cast=conf.as_str,
            ).strip()
            or None,
            model=conf.resolve(
                "relay.remote_agent.model",
                env="CLIO_RELAY_REMOTE_AGENT_MODEL",
                default="",
                cast=conf.as_str,
            ).strip()
            or None,
            workdir=conf.resolve(
                "relay.remote_agent.workdir",
                env="CLIO_RELAY_REMOTE_AGENT_WORKDIR",
                default="",
                cast=conf.as_str,
            ).strip()
            or None,
        )
    }
    app.state.relay_runtime_status = {"configured": True, "reason": None}
