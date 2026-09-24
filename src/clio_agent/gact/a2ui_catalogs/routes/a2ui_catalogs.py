"""GACT 0.3 A2UI catalog discovery routes.

The client registry source for S6: ``GET /v1/a2ui/catalogs`` lists every
installed catalog (builtin ∪ every discovered pack, regardless of session);
``GET /v1/sessions/{sid}/a2ui/catalogs`` adds the session's producibility
verdict and, for each producible catalog, its full ``file`` + ``sidecar`` +
``instructions`` so a client can build its renderer registry without a
second round trip per catalog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.a2ui_catalogs.activation import session_producible_catalog_ids
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry, CatalogRegistry


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(error=ErrorInfo(error=code, message=message)).model_dump(
            exclude_none=True
        ),
    )


def _catalog_summary(entry: "CatalogEntry") -> dict[str, Any]:
    """Return one catalog's identity + shape summary (no full file body)."""

    return {
        "catalogId": entry.catalog_id,
        "protocolVersion": entry.protocol_version,
        "source": entry.source,
        "checksum": entry.checksum,
        "componentNames": sorted(entry.file.get("components", {})),
        "functionNames": sorted(entry.file.get("functions", {})),
        # S1 (iowarp/clio-agent, A2UI catalog contract): the wire contract is
        # "absent, never null" -- an unset optional sidecar field (e.g. an
        # `implements` entry's `presets`, an `events` route's `context_schema`/
        # `operation`/`narration`) must be OMITTED, not serialised as JSON
        # `null`. Without `exclude_none`, every builtin catalog's `implements`
        # map dumps `"presets": null` for each of its ~30 unaliased components,
        # and every declared event dumps `"context_schema": null` /
        # `"operation": null` / `"narration": null` when unset -- a client zod
        # schema using `.optional()` (which rejects an explicit `null`) then
        # fails to parse the WHOLE catalog list, silently emptying the
        # client's registry (gact-tui's catalog-registry.ts).
        "sidecar": entry.sidecar.model_dump(mode="json", exclude_none=True),
    }


def _catalog_row(entry: "CatalogEntry", *, producible: bool) -> dict[str, Any]:
    """Return one session-scoped catalog row: the summary plus producibility."""

    row = _catalog_summary(entry)
    row["producible"] = producible
    if producible:
        row["file"] = entry.file
        row["instructions"] = entry.instructions
    return row


def register_a2ui_catalog_routes(app: FastAPI) -> None:
    """Register the installed-catalog and session-catalog discovery routes."""

    @app.get("/v1/a2ui/catalogs")
    async def list_installed_catalogs() -> dict[str, Any]:
        """Return every installed catalog: builtins plus every discovered pack."""

        registry: "CatalogRegistry" = app.state.a2ui_catalogs
        return {"catalogs": [_catalog_summary(entry) for entry in registry.installed()]}

    @app.get("/v1/sessions/{sid}/a2ui/catalogs")
    async def list_session_catalogs(sid: str) -> dict[str, Any]:
        """Return installed catalogs with this session's producibility verdict."""

        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")
        registry: "CatalogRegistry" = app.state.a2ui_catalogs
        producible_ids = set(session_producible_catalog_ids(app, sid))
        rows = [
            _catalog_row(entry, producible=entry.catalog_id in producible_ids)
            for entry in registry.installed()
        ]
        return {"catalogs": rows}


__all__ = ["register_a2ui_catalog_routes"]
