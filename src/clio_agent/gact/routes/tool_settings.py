"""The plugin tools page routes (``/v1/settings/tools``), TD54.

``GET /v1/settings/tools?ref=<marketplace>/<plugin>`` returns the plugin's tools with
their flags, defaults, the person's saved rules and the outcome in each approval mode.
``PUT /v1/settings/tools`` saves the person's permission (a policy rule) and/or I/O
(``tools.callers`` in the user ``config.yaml``) for one or more tools.
``POST /v1/settings/tools/rules/remove`` removes one saved rule.
The logic lives in :mod:`clio_agent.gact.tool_settings_page`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from yaml import YAMLError

from clio_agent.gact import tool_settings_page as page
from clio_agent.gact.routes._body import json_body


def register_tool_settings_routes(app: FastAPI) -> None:
    """Register the tools page routes."""
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    @app.get("/v1/settings/tools")
    async def get_tool_settings(ref: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(page.tools_view, app, ref)
        except StoreError as exc:
            raise HTTPException(404, detail=str(exc)) from exc

    @app.put("/v1/settings/tools")
    async def put_tool_settings(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="PUT /v1/settings/tools")
        try:
            return await asyncio.to_thread(page.save_tool_settings, app, body)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        except (OSError, YAMLError) as exc:
            raise HTTPException(
                503, detail="Tool settings could not be saved; check configuration storage"
            ) from exc

    @app.post("/v1/settings/tools/rules/remove")
    async def remove_tool_rule(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/settings/tools/rules/remove")
        try:
            return await asyncio.to_thread(page.remove_rule, app, body)
        except LookupError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
