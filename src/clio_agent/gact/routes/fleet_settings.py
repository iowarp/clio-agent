"""Authenticated Fleet judge model settings."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from yaml import YAMLError

from clio_agent.gact.fleet_settings import FleetUpdate, fleet_settings, save_fleet_settings


def register_fleet_settings_routes(app: FastAPI) -> None:
    """Expose persisted judge choices without changing the conversation's model."""

    @app.get("/v1/settings/fleet")
    def get_fleet() -> dict[str, Any]:
        return fleet_settings(app)

    @app.patch("/v1/settings/fleet")
    def patch_fleet(update: FleetUpdate) -> dict[str, Any]:
        try:
            return save_fleet_settings(update, app)
        except ValueError as exc:
            raise HTTPException(409 if "changed" in str(exc) else 422, detail=str(exc)) from exc
        except (OSError, YAMLError) as exc:
            raise HTTPException(
                503, detail="Fleet settings could not be saved; check configuration storage"
            ) from exc
