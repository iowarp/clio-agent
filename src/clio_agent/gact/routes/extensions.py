"""Shared extension creation, portable uploads and explicit public MCP discovery."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.agents.resolution import plugin_service
from clio_agent.gact.plugin_authoring import installed_changed
from clio_agent.gact.routes._body import json_body
from clio_agent.plugins.local import MAX_BYTES, archive_files, create_local
from clio_agent.plugins.store import StoreError


def register_extension_routes(app: FastAPI) -> None:
    """Expose one authoring owner and read-only registry metadata, never eager connections."""

    async def install(body: dict[str, Any]) -> dict[str, Any]:
        service = plugin_service(app)
        try:
            await asyncio.to_thread(service.ready)
            result = await asyncio.to_thread(create_local, service.store, body)
        except (ValueError, StoreError) as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(
                409,
                "The agent host could not save this extension. Check folder permissions or choose another name.",
            ) from None
        installed_changed(app)
        result.pop("federated_pins", None)
        return result

    @app.post("/v1/plugins/local", status_code=201)
    async def local(request: Request) -> dict[str, Any]:
        return await install(await json_body(request, route="Create local extension"))

    @app.post("/v1/plugins/upload", status_code=201)
    async def upload(request: Request, name: str) -> dict[str, Any]:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES * 4 // 3 + 1024:
                raise HTTPException(413, "Upload a ZIP smaller than 2 MiB")
        try:
            payload = json.loads(raw)
            encoded = base64.b64decode(payload["archive"], validate=True)
            files = await asyncio.to_thread(archive_files, encoded)
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise HTTPException(422, str(exc)) from None
        return await install({"name": name, "files": files})

    @app.get("/v1/extensions/mcp-registry")
    async def registry(search: str = "", cursor: str = "") -> dict[str, Any]:
        if len(search) > 160 or len(cursor) > 2048:
            raise HTTPException(422, "Search or page cursor is too long")
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                async with client.stream(
                    "GET",
                    "https://registry.modelcontextprotocol.io/v0.1/servers",
                    params={"search": search, "limit": 24, "version": "latest", "cursor": cursor},
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise ValueError("Registry response exceeds the display limit")
            data = json.loads(raw)
            if not isinstance(data.get("servers"), list):
                raise ValueError("Unexpected MCP registry response")
            return {
                "servers": data["servers"][:24],
                "next_cursor": (data.get("metadata") or {}).get("nextCursor") or "",
            }
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(
                502, "The MCP Registry could not be read. Retry or add a custom connection"
            ) from None
