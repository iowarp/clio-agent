"""Connector routes (``mcp-apps-contracts.md``, Connectors).

* ``GET /v1/connectors`` lists every connector with its state for the person
  (``?refresh=true`` checks each enabled server again), plus the entries that did
  not load (``diagnostics``).
* ``GET /v1/connectors/{id}`` one row; ``GET /v1/connectors/{id}/logo`` its SVG.
  ``?session_id=`` reads the row as that conversation sees it: its own values over
  the defaults, with ``default_value`` and ``conversation`` per need (CV-D5).
* ``PUT /v1/connectors/{id}/values`` ``{values: {key: str | null}}`` sets needs,
  secrets included, and returns the row once CLIO has reconnected (CN7).
* ``POST /v1/connectors/{id}/enable`` ``{scope: "all" | {agents: [...]}}`` and
  ``POST /v1/connectors/{id}/disable``.
* ``POST /v1/connectors`` ``{url}`` or ``{command, args}`` (and an optional
  ``name``) adds a custom connector (AQ11); ``DELETE /v1/connectors/{id}`` removes
  a custom one.

A sign-in need uses ``/v1/mcp/configuration/{id}/sign-in``: the connection's name
is the connector's id.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from clio_agent.gact import connectors_service as service
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def _error(exc: service.ConnectorError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=exc.code, message=str(exc), recoverable=exc.status != 404)
        ).model_dump(exclude_none=True),
    )


def register_connector_routes(app: FastAPI) -> None:
    """Register the connector routes."""

    @app.get("/v1/connectors")
    async def list_connectors(refresh: bool = False) -> dict[str, Any]:
        from clio_agent.connectors.entries import load_catalog  # noqa: PLC0415

        rows = await service.rows(app, refresh=refresh)
        catalog = await asyncio.to_thread(load_catalog)
        return {
            "connectors": rows,
            "diagnostics": [row.to_dict() for row in catalog.diagnostics],
        }

    @app.get("/v1/connectors/{connector_id}")
    async def get_connector(
        connector_id: str, refresh: bool = False, session_id: str = ""
    ) -> dict[str, Any]:
        try:
            found = service.entry(connector_id)
        except service.ConnectorError as exc:
            raise _error(exc) from None
        if session_id:
            from clio_agent.gact.connector_values import session_row  # noqa: PLC0415

            if app.state.sessions.get(session_id) is None:
                raise HTTPException(404, "No such conversation")
            return await asyncio.to_thread(session_row, app, session_id, found)
        choice = service.state.choice(connector_id)
        if choice.enabled and not any(
            need.kind != "sign-in" for need in service.missing(found, choice)
        ):
            await service.probe(app, found, choice, force=refresh)
        return service.row(app, found, choice)

    @app.get("/v1/connectors/{connector_id}/logo")
    async def connector_logo(connector_id: str) -> Response:
        try:
            found = service.entry(connector_id)
        except service.ConnectorError as exc:
            raise _error(exc) from None
        path = found.logo_path
        if path is None or not path.is_file():
            raise HTTPException(404, "This connector has no logo")
        return Response(
            content=await asyncio.to_thread(path.read_bytes),
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "max-age=3600",
                # An SVG served on CLIO's origin must not run script.
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.put("/v1/connectors/{connector_id}/values")
    async def put_values(connector_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request, route="PUT /v1/connectors/{id}/values")
        values = body.get("values")
        if not isinstance(values, dict):
            raise HTTPException(422, "values must be an object of need keys")
        try:
            return await service.set_values(app, connector_id, values)
        except service.ConnectorError as exc:
            raise _error(exc) from None

    @app.post("/v1/connectors/{connector_id}/enable")
    async def enable(connector_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/connectors/{id}/enable")
        try:
            return await service.enable(app, connector_id, body.get("scope") or "all")
        except service.ConnectorError as exc:
            raise _error(exc) from None

    @app.post("/v1/connectors/{connector_id}/disable")
    async def disable(connector_id: str) -> dict[str, Any]:
        try:
            return await service.disable(app, connector_id)
        except service.ConnectorError as exc:
            raise _error(exc) from None

    @app.post("/v1/connectors", status_code=201)
    async def create(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/connectors")
        try:
            return await service.create_custom(app, body)
        except service.ConnectorError as exc:
            raise _error(exc) from None

    @app.delete("/v1/connectors/{connector_id}")
    async def delete(connector_id: str) -> dict[str, Any]:
        try:
            found = service.entry(connector_id)
        except service.ConnectorError as exc:
            raise _error(exc) from None
        if found.source != "custom":
            raise _error(
                service.ConnectorError(
                    "connector_not_custom", "Only a connector added by address can be removed"
                )
            )
        await service.disable(app, connector_id)
        await asyncio.to_thread(service.state.forget, connector_id)
        await asyncio.to_thread(service.secrets.forget, connector_id)
        return {"id": connector_id, "removed": True}
