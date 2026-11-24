"""Plugin page routes (host semantics HS3): list, open, close, act on, and watch a page open.

A page is a plugin's entry in the host's primary navigation; see
:mod:`clio_agent.gact.plugin_pages`. Its events and permission prompts arrive on its
context session's event stream and interaction routes. ``status`` reports the steps of
the page's latest open (:mod:`clio_agent.gact.plugin_page_startup`).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.plugin_page_startup import page_startups
from clio_agent.gact.plugin_pages import plugin_pages
from clio_agent.gact.plugin_tabs import PluginTabError
from clio_agent.gact.protocol_v3 import A2UI_V091
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def _error(exc: PluginTabError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=exc.reason, message=str(exc), recoverable=False)
        ).model_dump(exclude_none=True),
    )


def register_plugin_page_routes(app: FastAPI) -> None:
    """Register the global plugin page routes."""

    @app.get("/v1/plugin-pages")
    async def list_plugin_pages() -> dict[str, Any]:
        return {"pages": await plugin_pages(app).list()}

    @app.post("/v1/plugin-pages/{plugin}/{page}/open")
    async def open_plugin_page(plugin: str, page: str) -> dict[str, Any]:
        try:
            return await plugin_pages(app).open(plugin, page)
        except PluginTabError as exc:
            raise _error(exc) from exc

    @app.post("/v1/plugin-pages/{plugin}/{page}/close")
    async def close_plugin_page(plugin: str, page: str) -> dict[str, Any]:
        try:
            return await plugin_pages(app).close(plugin, page)
        except PluginTabError as exc:
            raise _error(exc) from exc

    @app.get("/v1/plugin-pages/{plugin}/{page}/status")
    async def plugin_page_status(plugin: str, page: str) -> dict[str, Any]:
        # Cheap enough to poll while an open is in flight: it reads only the record.
        return page_startups(app).status(plugin, page)

    @app.post("/v1/plugin-pages/{plugin}/{page}/actions")
    async def plugin_page_action(plugin: str, page: str, request: Request) -> dict[str, Any]:
        if getattr(request.state, "a2ui_protocol_version", None) != A2UI_V091:
            raise _error(
                PluginTabError(406, "unsupported_protocol", f"A2UI {A2UI_V091} must be negotiated")
            )
        body = await json_body(request, route="POST /v1/plugin-pages/{plugin}/{page}/actions")
        try:
            return await plugin_pages(app).action(plugin, page, body)
        except PluginTabError as exc:
            raise _error(exc) from exc


__all__ = ["register_plugin_page_routes"]
