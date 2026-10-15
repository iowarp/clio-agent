"""Authenticated global preference routes backed by the user config document."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import yaml
from fastapi import FastAPI, HTTPException

from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.runtime_settings import (
    RuntimeSettings,
    RuntimeSettingsError,
    UpdateRuntimeSettings,
    get_runtime_settings,
    update_runtime_settings,
)


def _respond(operation: Callable[[], RuntimeSettings]) -> RuntimeSettings:
    try:
        return operation()
    except RuntimeSettingsError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "error": {
                    "error": "configuration_conflict"
                    if exc.status_code == 409
                    else "invalid_configuration_setting",
                    "message": str(exc),
                }
            },
        ) from exc
    except (OSError, yaml.YAMLError, ValueError, TypeError) as exc:
        # Never return raw config content, parser excerpts or private key values.
        raise HTTPException(
            500,
            detail={
                "error": {
                    "error": "configuration_unavailable",
                    "message": "Unable to read or save the configuration file. Check its format and permissions.",
                }
            },
        ) from exc


def register_runtime_settings_routes(app: FastAPI) -> None:
    """Register curated configuration reads and updates under the normal API auth."""

    update_lock = asyncio.Lock()

    @app.get("/v1/settings/runtime", response_model=RuntimeSettings)
    def get_settings() -> RuntimeSettings:
        return _respond(get_runtime_settings)

    @app.patch("/v1/settings/runtime", response_model=RuntimeSettings)
    async def patch_settings(request: UpdateRuntimeSettings) -> RuntimeSettings:
        async with update_lock:
            result = await run_off_loop(lambda: _respond(lambda: update_runtime_settings(request)))
            if "agent_tasks.max_concurrent" in request.changes and hasattr(
                app.state, "max_concurrent_agent_tasks"
            ):
                app.state.max_concurrent_agent_tasks = int(
                    next(
                        row.value
                        for row in result.settings
                        if row.key == "agent_tasks.max_concurrent"
                    )
                )
                if hasattr(app.state, "agent_task_registry"):
                    from clio_agent.gact.turn_spawn import _admit_next_queued  # noqa: PLC0415

                    _admit_next_queued(app)
            return result
