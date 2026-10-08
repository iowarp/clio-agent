"""Discover a persisted bind's context window after a restart (F042).

``PUT /v1/providers/lm`` folds a provider handshake into the bound config, so
``GET /v1/providers/lm`` reports the served ``context_window``. A server that
boots from the persisted selection built its agent without that fold: turns
were right (each folds its own handshake) but the GET reported no window until
the next PUT. This runs the same handshake once in the background after the
boot agent is ready, so startup is never blocked on a provider round trip.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from clio_agent.gact.providers.auth import _is_placeholder_api_key

logger = logging.getLogger(__name__)


def schedule(app: Any, agent: Any) -> asyncio.Task[None] | None:
    """Start :func:`refresh_bound_window` for the boot agent, when a loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(refresh_bound_window(app, agent))
    app.state.boot_handshake_task = task
    return task


async def refresh_bound_window(app: Any, agent: Any) -> None:
    """Fold a handshake into the boot agent's config unless a bind already did."""
    cfg = getattr(agent, "_provider_config", None)
    if cfg is None or not cfg.model or not cfg.api_base:
        return
    if getattr(app.state, "lm_handshake_report", None) is not None:
        return  # a PUT bound (and handshook) first
    from clio_agent.providers.handshake import HandshakeContext, run_handshake  # noqa: PLC0415

    try:
        report = await run_handshake(
            HandshakeContext(
                provider_id=cfg.provider_id or cfg.provider,
                provider_kind=cfg.provider,
                api_base=cfg.api_base,
                api_key="" if _is_placeholder_api_key(cfg.api_key) else cfg.api_key,
                target_model=cfg.model,
                auth_mode="passive",
            )
        )
    # Reported, not raised: a turn still folds its own handshake.
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("boot provider discovery skipped reason=boot_handshake_failed error=%r", exc)
        app.state.boot_handshake_error = repr(exc)
        return
    # A bind that landed while discovery ran owns the agent and the report now.
    if getattr(app.state, "agent", None) is not agent or getattr(
        app.state, "lm_handshake_report", None
    ):
        return
    match = getattr(report, "match_model", None)
    if not getattr(report, "ok", False) or match is None or match(cfg.model) is None:
        # e.g. the managed server is stopped: the window is found on the next bind or turn.
        logger.warning(
            "boot provider discovery found no model reason=boot_handshake_unmatched model=%s",
            cfg.model,
        )
        app.state.boot_handshake_error = "boot_handshake_unmatched"
        return
    cfg.apply_handshake(report)
    app.state.lm_handshake_report = report
