"""``GET /v1/fleet``: connected accounts, their usage and spend, and their models."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from clio_agent.gact.fleet.read import read_fleet


def register_fleet_routes(app: FastAPI) -> None:
    """Register the fleet read route."""

    @app.get("/v1/fleet")
    async def fleet(refresh: bool = False) -> dict[str, Any]:
        """The fleet: ``refresh=true`` re-probes providers and re-reads polled usage."""

        return (await read_fleet(app, refresh=refresh)).to_wire()


__all__ = ["register_fleet_routes"]
