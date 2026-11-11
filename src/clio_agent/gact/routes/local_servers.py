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
read re-discovers it at its new address. When the bound model runs on that
server at its previous address, the binding follows it to the new one (the
response's ``binding``), so turns stop talking to the old address.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from clio_agent.gact import local_server_store as store
from clio_agent.gact.lm_provider_types import LMProviderRequest
from clio_agent.gact.providers.response_settings import SETTING_FIELDS
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


async def check_address(
    preset: "LMProviderPreset", address: str, server_id: str = ""
) -> dict[str, Any]:
    """One live handshake of ``preset`` at ``address``: reachability and served models.

    ``server_id`` names a saved server whose own key (its linked deployment's, for a
    custom entry such as ``vllm@<name>``) is sent instead of the preset's.
    """
    from clio_agent.gact.provider_catalog import probe_api_key  # noqa: PLC0415
    from clio_agent.providers.handshake import HandshakeContext, run_handshake  # noqa: PLC0415
    from clio_agent.providers.model_discovery import resolve_cloud_api_key  # noqa: PLC0415

    own_key = resolve_cloud_api_key(server_id) if server_id and server_id != preset.id else ""
    report = await run_handshake(
        HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=address,
            api_key=own_key or probe_api_key(preset),
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


#: Bound settings a re-bind carries to the new address. ``thinking_level`` is left
#: out: an omitted level keeps the person's own for the same provider and model.
_CARRIED = (
    "provider",
    "provider_id",
    "model",
    "provider_options",
    "thinking_budget",
    "turn_timeout_s",
    "transport",
    *SETTING_FIELDS,
)


def _same_address(left: object, right: object) -> bool:
    return str(left or "").rstrip("/") == str(right or "").rstrip("/")


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
        from clio_agent.gact.provider_catalog_snapshot import invalidate_provider  # noqa: PLC0415

        # A custom server is its own catalog provider (and fleet account), keyed by its id.
        invalidate_provider(app, entry.id if entry.custom else entry.preset_id)

    async def checked(entry: store.LocalServerEntry) -> dict[str, Any]:
        result = await check_address(preset_for(entry.preset_id), entry.address, entry.id)
        _checks(app)[entry.id] = result
        return {**entry.to_wire(), "check": result}

    def listed(entry: store.LocalServerEntry) -> dict[str, Any]:
        return {**entry.to_wire(), "check": _checks(app).get(entry.id)}

    def managed_key(preset_id: str | None, address: str) -> str:
        """The key ref of the CLIO-managed deployment at ``address``, if it is one.

        "Use in Models" saves a managed server's address; its requests must
        then carry that deployment's key. Matched server-side on engine and
        address, so no client ever handles the key or its id. A custom server
        is reached through the generic OpenAI-compatible preset.
        """
        from clio_agent.gact.infrastructure.server_access import (  # noqa: PLC0415
            managed_credential_ref,
        )

        infrastructure = getattr(app.state, "infrastructure_store", None)
        if infrastructure is None:
            return ""
        preset = preset_id or store.CUSTOM_SERVER_PRESET_ID
        return managed_credential_ref(infrastructure, preset, address)

    def own_entry(preset_id: str | None, address: str, label: str) -> tuple[str | None, str]:
        """Save a named instance or the router as its own server, not the engine's.

        The engine's entry (id == preset) is its default deployment's address;
        binding a named instance or the model router must not replace it.
        """
        from clio_agent.gact.infrastructure.model_instances import (  # noqa: PLC0415
            ROUTER_SERVICE,
            instance_label,
            is_named_instance,
        )
        from clio_agent.gact.infrastructure.server_access import (  # noqa: PLC0415
            managed_record_at,
        )

        infrastructure = getattr(app.state, "infrastructure_store", None)
        if infrastructure is None or not preset_id:
            return preset_id, label
        record = managed_record_at(infrastructure, preset_id, address)
        if record is None:
            return preset_id, label
        if record.service_id == ROUTER_SERVICE:
            return None, label or "Model router"
        if is_named_instance(record.service_id):
            return None, label or instance_label(preset_for(preset_id).label, record.service_id)
        return preset_id, label

    async def follow_binding(
        entry: store.LocalServerEntry, previous: str | None, current: str | None
    ) -> dict[str, Any] | None:
        """Move the bound model to ``entry``'s new address when it ran on the old one.

        Turns run on the bound configuration, which a saved address alone does not
        change. A binding pinned to some other address is left as it is. The bind
        goes through ``PUT /v1/providers/lm`` itself, with every bound setting kept.
        """
        from clio_agent.gact.providers.config import _effective_lm_config  # noqa: PLC0415

        bound = _effective_lm_config(app)
        provider_id = entry.id if entry.custom else entry.preset_id
        if (
            not previous
            or not current
            or str(bound.get("provider_id") or "") != provider_id
            or not _same_address(bound.get("api_base"), previous)
            or _same_address(previous, current)
        ):
            return None
        # The person's own bind when there is one; a boot binding has only its config.
        stored = getattr(app.state, "lm_config", None)
        source = stored if isinstance(stored, dict) else bound
        carried = {name: source[name] for name in _CARRIED if source.get(name) is not None}
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/v1/providers/lm"
            and "PUT" in getattr(route, "methods", ())
        )
        try:
            info = await endpoint(LMProviderRequest(**{**carried, "api_base": current}))
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            message = str((detail.get("error") or {}).get("message") or exc.detail)
            return {"followed": False, "provider_id": provider_id, "error": message}
        return {
            "followed": True,
            "provider_id": provider_id,
            "api_base": current,
            "state": str(getattr(info, "state", "") or "ready"),
        }

    async def answer(
        entry: store.LocalServerEntry, previous: str | None, current: str | None
    ) -> dict[str, Any]:
        body = await checked(entry)
        binding = await follow_binding(entry, previous, current)
        return body if binding is None else {**body, "binding": binding}

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
        preset_id, label = own_entry(req.preset_id, req.address, req.label)
        before = {e.id: e.address for e in entries()}
        try:
            # The key is matched through the preset the deployment was bound as
            # (a llama.cpp instance's own), though it is saved as a custom server.
            # Binding the same deployment again moves its entry (no ``-2`` copy).
            entry = store.add_server(
                address=req.address,
                label=label or (preset_for(preset_id).label if preset_id else ""),
                preset_id=preset_id,
                credential_ref=managed_key(req.preset_id or preset_id, req.address),
            )
        except store.LocalServerStoreError as exc:
            raise _error(422, "invalid_server", str(exc)) from exc
        retire(entry)
        previous = before.get(entry.id) or (
            preset_for(entry.preset_id).api_base if not entry.custom else None
        )
        return await answer(entry, previous, entry.address)

    @app.patch("/v1/providers/servers/{server_id}")
    async def update_local_server(server_id: str, req: UpdateServerRequest) -> dict[str, Any]:
        """Change a saved server's address or label, then check it."""
        try:
            current = store.get_server(server_id)
            entry = store.update_server(
                server_id,
                address=req.address,
                label=req.label,
                credential_ref=(
                    managed_key(current.preset_id if current else None, req.address)
                    if req.address is not None
                    else None
                ),
            )
        except KeyError as exc:
            raise _error(404, "not_found", f"no saved server: {server_id}") from exc
        except store.LocalServerStoreError as exc:
            raise _error(422, "invalid_server", str(exc)) from exc
        retire(entry)
        return await answer(entry, current.address if current else None, entry.address)

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
        restored = None if entry.custom else preset_for(entry.preset_id).api_base
        binding = await follow_binding(entry, entry.address, restored)
        return {"removed": server_id, **({"binding": binding} if binding else {})}

    @app.post("/v1/providers/servers/{server_id}/check")
    async def check_local_server(server_id: str) -> dict[str, Any]:
        """Check one saved server's reachability now."""
        entry = next((e for e in entries() if e.id == server_id), None)
        if entry is None:
            raise _error(404, "not_found", f"no saved server: {server_id}")
        return await checked(entry)
