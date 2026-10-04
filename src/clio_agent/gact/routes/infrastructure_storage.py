"""Host-specific filesystem browsing and persistent deployment storage settings."""

from __future__ import annotations

from typing import Any

from clio_schemas.connected_resources import HostStorageLocations
from fastapi import FastAPI, HTTPException

from clio_agent.gact.infrastructure.model_runtimes import ENGINES, deployment_storage_configuration
from clio_agent.gact.infrastructure.models import CommandSpec, InfrastructureTarget, TargetFacts
from clio_agent.gact.infrastructure.probe import probe_target
from clio_agent.gact.infrastructure.storage import (
    StorageInspectionRequest,
    inspect_target_path,
    resolved_locations,
)


def register_infrastructure_storage_routes(app: FastAPI) -> None:
    """Register storage control routes using the active CLIO's transport registry."""

    def target_for(target_id: str) -> InfrastructureTarget:
        target = app.state.infrastructure_store.target(target_id)
        if target is None:
            raise HTTPException(404, "Infrastructure target not found")
        return target

    async def facts_for(target: InfrastructureTarget) -> TargetFacts:
        async def execute(spec: CommandSpec) -> Any:
            return await app.state.infrastructure_runtime.execute_on_target(target.id, spec)

        return await probe_target(target, execute if target.kind == "ssh" else None)

    def settings_response(target: InfrastructureTarget, facts: TargetFacts) -> dict[str, Any]:
        inherited = target.model_copy(update={"storage": HostStorageLocations()})
        return {
            "target_id": target.id,
            "host_label": target.label,
            "hostname": facts.hostname,
            "requested": target.storage.model_dump(),
            "effective": resolved_locations(target, facts).model_dump(),
            "defaults": resolved_locations(inherited, facts).model_dump(),
        }

    async def inspect(
        target: InfrastructureTarget, body: StorageInspectionRequest
    ) -> dict[str, Any]:
        async def execute(spec: CommandSpec) -> Any:
            return await app.state.infrastructure_runtime.execute_on_target(target.id, spec)

        return await inspect_target_path(target, body, execute)

    @app.get("/v1/infrastructure/targets/{target_id}/storage")
    async def storage_locations(target_id: str) -> dict[str, Any]:
        """Return requested paths and their effective values on the selected host."""
        target = target_for(target_id)
        try:
            return settings_response(target, await facts_for(target))
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.put("/v1/infrastructure/targets/{target_id}/storage")
    async def set_storage_locations(target_id: str, body: HostStorageLocations) -> dict[str, Any]:
        """Validate paths on their host and save settings without moving existing data."""
        previous = target_for(target_id)
        target = previous.model_copy(update={"storage": body})
        try:
            facts = await facts_for(target)
            effective = resolved_locations(target, facts)
            for path in set(effective.model_dump().values()):
                report = await inspect(target, StorageInspectionRequest(path=path))
                if not report["writable"]:
                    raise ValueError(f"Storage directory is not writable on {target.label}: {path}")

            # Freeze paths in older receipts before changing defaults. A crash
            # between these writes is safe: both still refer to the old data.
            existing = [
                row
                for row in app.state.infrastructure_store.services()
                if row.target_id == target_id
                and row.service_id in ENGINES
                and "storage.service_directory" not in row.configuration
            ]
            if existing:
                for row in existing:
                    configuration = deployment_storage_configuration(
                        row.service_id,
                        facts,
                        previous,
                        row.configuration,
                        row.owned_resources,
                    )
                    app.state.infrastructure_store.update_service(
                        target_id,
                        row.service_id,
                        configuration=configuration,
                    )
            app.state.infrastructure_store.set_storage(target_id, body)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return settings_response(target, facts)

    @app.post("/v1/infrastructure/targets/{target_id}/storage/inspect")
    async def inspect_storage(target_id: str, body: StorageInspectionRequest) -> dict[str, Any]:
        """Browse folders or check space on the host named in the response."""
        try:
            return await inspect(target_for(target_id), body)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
