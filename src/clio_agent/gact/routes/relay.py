"""Relay configuration and live reachability route registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from clio_agent.gact.relay_status import probe_relay_status

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_relay_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the relay status surface on an application.

    Args:
        app: GACT FastAPI application receiving the route.
        deps: Shared route dependencies; unused by this read-only concern.
    """
    del deps

    @app.get("/v1/relay/status")
    async def relay_status() -> dict[str, Any]:
        """Return configured relay identity and a fresh bounded TCP probe."""
        return await probe_relay_status()
