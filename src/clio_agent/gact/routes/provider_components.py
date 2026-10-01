"""Provider component routes: is a newer SDK available, and update it in place.

* ``GET /v1/providers/{id}/components`` -- installed vs latest installable
  version of the provider's SDK components (PyPI JSON, TTL-cached;
  ``refresh=true`` asks again), ``update_available``, the CLI the transport
  runs (``client``: installed vs bundled + version) and the last update job.
* ``POST /v1/providers/{id}/components/update`` -- start the staged update
  (``checking`` -> ``downloading`` -> ``installing`` -> ``verifying`` ->
  ``done`` | ``failed``) on a worker thread; 409 while one is running.
* ``GET /v1/providers/{id}/components/update`` -- the job's current stage.

Only the providers declared in
:data:`clio_agent.providers.components.registry.PROVIDER_COMPONENTS` have
components; any other provider is a typed 405, never an empty success.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.provider_catalog_snapshot import invalidate_provider
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderPreset
from clio_agent.providers.components import client_binary
from clio_agent.providers.components.registry import components_for
from clio_agent.providers.components.status import provider_component_status
from clio_agent.providers.components.updater import (
    UPDATER,
    UpdateEnvironment,
    UpdateInProgressError,
    UpdateJob,
    verify_provider_in_child,
)
from clio_agent.providers.support_record import record_support


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=status != 404)
        ).model_dump(exclude_none=True),
    )


def live_update_environment() -> UpdateEnvironment:
    """The environment of THIS runtime: its interpreter, PyPI, and the real provider check."""
    return UpdateEnvironment(
        python=sys.executable,
        verify_provider=verify_provider_in_child,
        record_support=record_support,
    )


def register_provider_component_routes(app: FastAPI, presets: list[LMProviderPreset]) -> None:
    """Register the component status/update routes for ``presets``."""

    def _preset_with_components(provider_id: str) -> LMProviderPreset:
        preset = next((p for p in presets if p.id == provider_id), None)
        if preset is None:
            raise _error(404, "not_found", f"unknown provider: {provider_id}")
        if components_for(preset.provider) is None:
            raise _error(
                405,
                "provider_has_no_components",
                f"provider '{provider_id}' has no updatable components",
            )
        return preset

    def _job_wire(job: UpdateJob | None) -> dict[str, Any] | None:
        return job.to_wire() if job is not None else None

    @app.get("/v1/providers/{provider_id}/components")
    async def provider_components(provider_id: str, refresh: bool = False) -> dict[str, Any]:
        """Installed vs latest SDK components, ``update_available``, the CLI in use."""
        preset = _preset_with_components(provider_id)
        status = await asyncio.to_thread(
            provider_component_status, preset.provider, refresh=refresh
        )
        assert status is not None  # guarded by _preset_with_components
        selection = await asyncio.to_thread(client_binary.provider_client, preset.provider)
        return {
            "provider_id": preset.id,
            **status.to_wire(),
            "client": selection.to_wire() if selection is not None else None,
            "update": _job_wire(UPDATER.job(preset.provider)),
        }

    @app.post("/v1/providers/{provider_id}/components/update", status_code=202)
    async def update_provider_components(provider_id: str) -> dict[str, Any]:
        """Start the staged in-place update of the provider's SDK components."""
        preset = _preset_with_components(provider_id)

        def _finished(job: UpdateJob) -> None:
            client_binary.reset_client_cache()
            invalidate_provider(app, preset.id)

        try:
            job = UPDATER.start(preset.provider, live_update_environment(), on_finish=_finished)
        except UpdateInProgressError as exc:
            raise _error(409, "component_update_in_progress", str(exc)) from exc
        return {"provider_id": preset.id, **job.to_wire()}

    @app.get("/v1/providers/{provider_id}/components/update")
    async def provider_component_update(provider_id: str) -> dict[str, Any]:
        """The provider's latest update job (its current stage while running)."""
        preset = _preset_with_components(provider_id)
        job = UPDATER.job(preset.provider)
        if job is None:
            raise _error(
                404, "component_update_not_found", f"no component update has run for {provider_id}"
            )
        return {"provider_id": preset.id, **job.to_wire()}


__all__ = [
    "live_update_environment",
    "register_provider_component_routes",
]
