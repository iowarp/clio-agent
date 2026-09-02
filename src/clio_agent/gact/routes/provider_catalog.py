"""Normalized provider/model catalog route for the React client."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import FastAPI

from clio_agent.gact.events import Event
from clio_agent.gact.provider_catalog import discover_provider
from clio_agent.providers.catalog import as_lm_presets

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_normalized_provider_catalog_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the capability-evidence provider catalog."""

    del deps

    @app.get("/v1/provider-catalog")
    async def provider_catalog(refresh: bool = False) -> dict[str, object]:
        providers = await asyncio.gather(
            *(discover_provider(preset, refresh=refresh) for preset in as_lm_presets())
        )
        payload: dict[str, object] = {
            "catalog_id": "active",
            "providers": providers,
            "authoritative": "live_handshake",
        }
        app.state.provider_catalog = payload
        if refresh:
            for session in app.state.sessions.list():
                app.state.bus.publish(
                    Event(
                        type="provider_catalog.refreshed",
                        session_id=session.id,
                        payload=payload,
                    )
                )
        return payload


__all__ = ["register_normalized_provider_catalog_routes"]
