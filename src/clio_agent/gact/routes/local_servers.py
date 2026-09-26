"""``/v1/providers/servers`` -- saved local and self-hosted model servers.

Owns the saved-server surface Settings > Providers edits: list, add, change an
address, remove, and check reachability. Persistence is
:mod:`clio_agent.gact.local_server_store` (the user ``config.yaml``); every
mutating call and every explicit check runs one live handshake against the
server's address and returns its result with the entry. The latest result
per server is kept in memory on ``app.state`` (runtime evidence, never
written to configuration).

A catalog runtime's saved address is also what discovery probes: see
:func:`clio_agent.gact.provider_catalog_snapshot._resolve_presets`. Each
mutation of one retires that provider's catalog entry so the next catalog
read re-discovers it at its new address.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from clio_agent.gact import local_server_store as store
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.lm_provider_types import LMProviderPreset

__all__ = ["register_local_server_routes"]


class AddServerRequest(BaseModel):
    """Body of ``POST /v1/providers/servers``."""

    address: str
    label: str = ""
    #: A catalog runtime (``lm_studio``, ``ollama``, ...) whose address this is;
    #: omitted for a custom OpenAI-compatible server.
    preset_id: str | None = None


class UpdateServerRequest(BaseModel):
    """Body of ``PATCH /v1/providers/servers/{server_id}``."""

    address: str | None = None
    label: str | None = None


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=status < 500)
        ).model_dump(exclude_none=True),
    )


def _checks(app: FastAPI) -> dict[str, dict[str, Any]]:
    checks = getattr(app.state, "local_server_checks", None)
    if not isinstance(checks, dict):
        checks = {}
        app.state.local_server_checks = checks
    return checks


async def check_address(preset: "LMProviderPreset", address: str) -> dict[str, Any]:
    """One live handshake of ``preset`` at ``address``: reachability and served models."""
    from clio_agent.gact.provider_catalog import probe_api_key  # noqa: PLC0415
    from clio_agent.providers.handshake import HandshakeContext, run_handshake  # noqa: PLC0415

    report = await run_handshake(
        HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=address,
            api_key=probe_api_key(preset),
            auth_mode="passive",
            allow_external_sources=True,
        ),
        force=True,
    )
    connectivity = report.connectivity.value
    return {
        "reachable": connectivity == "ok",
        "connectivity": connectivity,
        "models": [model.id for model in report.models],
        "error": report.error or "",
        "checked_at": report.generated_at or datetime.now(timezone.utc).isoformat(),
    }


def register_local_server_routes(app: FastAPI, presets: "list[LMProviderPreset]") -> None:
    """Register the saved-server routes on ``app``.

    Must be registered before ``GET /v1/providers/{provider_id}`` so the
    literal ``servers`` segment wins FastAPI's order-based match.
    """

    def preset_for(preset_id: str) -> "LMProviderPreset":
        preset = next((p for p in presets if p.id == preset_id), None)
        if preset is None:
            raise _error(404, "not_found", f"unknown provider: {preset_id}")
        return preset

    def retire(entry: store.LocalServerEntry) -> None:
        if entry.custom:
            return
        from clio_agent.gact.provider_catalog_snapshot import invalidate_provider  # noqa: PLC0415

        invalidate_provider(app, entry.preset_id)

    async def checked(entry: store.LocalServerEntry) -> dict[str, Any]:
        result = await check_address(preset_for(entry.preset_id), entry.address)
        _checks(app)[entry.id] = result
        return {**entry.to_wire(), "check": result}

    def listed(entry: store.LocalServerEntry) -> dict[str, Any]:
        return {**entry.to_wire(), "check": _checks(app).get(entry.id)}

    def entries() -> list[store.LocalServerEntry]:
        try:
            return store.list_servers()
        except store.LocalServerStoreError as exc:
            raise _error(500, "server_config_invalid", str(exc)) from exc

    @app.get("/v1/providers/servers")
    async def list_local_servers(check: bool = False) -> dict[str, Any]:
        """Every saved server with its latest check; ``check=true`` probes them all now."""
        saved = entries()
        if check:
            return {"servers": list(await asyncio.gather(*(checked(entry) for entry in saved)))}
        return {"servers": [listed(entry) for entry in saved]}

    @app.post("/v1/providers/servers")
    async def add_local_server(req: AddServerRequest) -> dict[str, Any]:
        """Save a server (a catalog runtime's address, or a custom one), then check it."""
        if req.preset_id:
            preset_for(req.preset_id)
        try:
            entry = store.add_server(
                address=req.address,
                label=req.label or (preset_for(req.preset_id).label if req.preset_id else ""),
                preset_id=req.preset_id,
            )
        except store.LocalServerStoreError as exc:
            raise _error(422, "invalid_server", str(exc)) from exc
        retire(entry)
        return await checked(entry)

    @app.patch("/v1/providers/servers/{server_id}")
    async def update_local_server(server_id: str, req: UpdateServerRequest) -> dict[str, Any]:
        """Change a saved server's address or label, then check it."""
        try:
            entry = store.update_server(server_id, address=req.address, label=req.label)
        except KeyError as exc:
            raise _error(404, "not_found", f"no saved server: {server_id}") from exc
        except store.LocalServerStoreError as exc:
            raise _error(422, "invalid_server", str(exc)) from exc
        retire(entry)
        return await checked(entry)

    @app.delete("/v1/providers/servers/{server_id}")
    async def remove_local_server(server_id: str) -> dict[str, Any]:
        """Forget a saved server; a catalog runtime goes back to its own address."""
        entry = next((e for e in entries() if e.id == server_id), None)
        if entry is None:
            raise _error(404, "not_found", f"no saved server: {server_id}")
        try:
            store.remove_server(server_id)
        except store.LocalServerStoreError as exc:
            raise _error(500, "server_config_invalid", str(exc)) from exc
        _checks(app).pop(server_id, None)
        retire(entry)
        return {"removed": server_id}

    @app.post("/v1/providers/servers/{server_id}/check")
    async def check_local_server(server_id: str) -> dict[str, Any]:
        """Check one saved server's reachability now."""
        entry = next((e for e in entries() if e.id == server_id), None)
        if entry is None:
            raise _error(404, "not_found", f"no saved server: {server_id}")
        return await checked(entry)
