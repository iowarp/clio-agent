"""Production relay owners installed at GACT and agent assembly boundaries."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.invoker import RelayExpertInvoker

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def relay_tool_surfaces_for_app(app: FastAPI) -> Any:
    """Discover and retain the one production relay tool projection for an app."""

    existing = getattr(app.state, "relay_tool_surfaces", None)
    if existing is not None:
        return existing
    from clio_agent.tools.relay_transport import discover_relay_tool_surfaces  # noqa: PLC0415

    surfaces = await discover_relay_tool_surfaces()
    app.state.relay_tool_surfaces = surfaces
    app.state.relay_tool_status = dict(surfaces.status)
    return surfaces


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
    cluster = conf.resolve(
        "relay.cluster", env="CLIO_RELAY_CLUSTER", default="", cast=conf.as_str
    ).strip()
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
