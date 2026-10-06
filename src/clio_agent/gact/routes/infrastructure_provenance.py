"""Trusted, connected-CLIO provenance setup and verification controls."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.concurrency import run_in_threadpool

from clio_agent import conf
from clio_agent.gact.infrastructure.models import ExternalServiceConnection
from clio_agent.gact.infrastructure.provenance_connections import (
    ProvenanceConnectionInput,
    activate_connection,
    connection_revision,
    connection_selected,
    disconnect_connection,
    verify_connection,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.provenance_config import attention_capture_enabled


def _active(app: FastAPI, row: ExternalServiceConnection) -> bool:
    if row.service_id == "cmf":
        backend = getattr(app.state, "artifact_provenance_backend", None)
        provider = getattr(backend, "provider", None)
        return (
            getattr(provider, "name", "") == "cmf"
            and getattr(getattr(provider, "config", None), "server_url", "").rstrip("/") == row.url
        )
    backend = getattr(app.state, "semantic_trace_backend", None)
    reader = getattr(backend, "reader", None)
    provider = reader("flowcept") if callable(reader) else None
    config = getattr(provider, "config", None)
    return bool(
        config
        and config.settings_path == row.configuration.get("settings_path")
        and config.persistence_owner == "collector"
        and config.privacy
        == ("full" if row.configuration.get("capture_attention") == "true" else "metadata")
        and attention_capture_enabled() == (row.configuration.get("capture_attention") == "true")
        and (
            row.configuration.get("capture_attention") != "true"
            or conf.resolve(
                "provenance.attention.files_dir",
                env="CLIO_PROVENANCE_ATTENTION_FILES_DIR",
                default="",
            )
            == row.configuration.get("attention_files_dir")
        )
    )


def register_infrastructure_provenance_routes(app: FastAPI) -> None:
    """Attach connections without granting stop, uninstall or upstream-data ownership."""
    lock = asyncio.Lock()
    active_revisions: dict[str, str] = {}
    for row in app.state.infrastructure_store.connections():
        if row.service_id in {"flowcept", "cmf"} and _active(app, row):
            try:
                active_revisions[row.id] = connection_revision(row)
            except (OSError, ValueError):
                continue

    def store() -> InfrastructureStore:
        return app.state.infrastructure_store

    def get(connection_id: str) -> ExternalServiceConnection:
        row = store().connection(connection_id)
        if row is None or row.service_id not in {"flowcept", "cmf"}:
            raise HTTPException(status_code=404, detail="Provenance connection not found")
        return row

    def status(row: ExternalServiceConnection) -> dict[str, Any]:
        try:
            revision = connection_revision(row)
            verified = row.verification.get("revision") == revision and bool(
                row.verification.get("write_readback")
            )
        except (OSError, ValueError):
            revision = ""
            verified = False
        return {
            **row.model_dump(mode="json"),
            "verified": verified,
            "active": bool(revision and active_revisions.get(row.id) == revision),
            "selected": connection_selected(row),
        }

    @app.get("/v1/infrastructure/provenance/connections")
    async def connections() -> dict[str, Any]:
        return {
            "connections": await run_in_threadpool(
                lambda: [
                    status(row)
                    for row in store().connections()
                    if row.service_id in {"flowcept", "cmf"}
                ]
            )
        }

    @app.post("/v1/infrastructure/provenance/connections")
    async def connect(body: ProvenanceConnectionInput) -> dict[str, Any]:
        async with lock:
            row = body.record()
            try:
                await run_in_threadpool(connection_revision, row)
            except (OSError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="Check the settings path on the connected CLIO"
                ) from exc
            # Repeated clicks/reconnect keep one resource and its verification.
            existing = next(
                (
                    held
                    for held in store().connections()
                    if (held.service_id, held.url, held.configuration)
                    == (row.service_id, row.url, row.configuration)
                ),
                None,
            )
            if existing:
                row = existing.model_copy(update={"label": body.label})
            await run_in_threadpool(store().put_connection, row)
            return await run_in_threadpool(status, row)

    @app.post("/v1/infrastructure/provenance/connections/{connection_id}/verify")
    async def verify(connection_id: str) -> dict[str, Any]:
        async with lock:
            row = get(connection_id)
            # A failed recheck must revoke its previous readiness claim.
            row = row.model_copy(update={"verification": {}, "reachable": False})
            await run_in_threadpool(store().put_connection, row)
            try:
                verified = await run_in_threadpool(verify_connection, row)
                await run_in_threadpool(store().replace_connection, row, verified)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return await run_in_threadpool(status, verified)

    @app.post("/v1/infrastructure/provenance/connections/{connection_id}/use")
    async def use(connection_id: str) -> dict[str, Any]:
        async with lock:
            row = get(connection_id)
            try:
                return await run_in_threadpool(activate_connection, row)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/v1/infrastructure/provenance/connections/{connection_id}", status_code=204)
    async def forget(connection_id: str) -> None:
        async with lock:
            row = get(connection_id)
            if row.id in active_revisions or connection_selected(row):
                raise HTTPException(
                    status_code=409,
                    detail="Select another provenance connection and restart CLIO before forgetting this one",
                )
            await run_in_threadpool(store().delete_connection, connection_id)

    @app.post("/v1/infrastructure/provenance/connections/{connection_id}/disconnect")
    async def disconnect(connection_id: str) -> dict[str, Any]:
        async with lock:
            try:
                return await run_in_threadpool(disconnect_connection, get(connection_id))
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
