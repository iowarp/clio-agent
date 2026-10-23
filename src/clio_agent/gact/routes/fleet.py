"""``GET /v1/fleet``: connected accounts, their usage and spend, and their models."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from clio_agent.gact.fleet.read import read_fleet
from clio_agent.gact.fleet.session_check import entry_fit, session_requirements


def register_fleet_routes(app: FastAPI) -> None:
    """Register the fleet read route."""

    @app.get("/v1/fleet")
    async def fleet(refresh: bool = False, session: str = "") -> dict[str, Any]:
        """The fleet: ``refresh=true`` re-probes providers and re-reads polled usage.

        ``session`` marks each entry with whether it meets that conversation agent's
        requirements (``fit``), for the model picker.
        """

        reading = await read_fleet(app, refresh=refresh)
        wire = reading.to_wire()
        found = session_requirements(app, session) if session else None
        if found is not None:
            fit = entry_fit(reading.entries, *found)
            wire["requires"] = found[0].to_dict()
            for row in wire["entries"]:
                row["fit"] = fit.get(row["ref"])
        return wire


__all__ = ["register_fleet_routes"]
