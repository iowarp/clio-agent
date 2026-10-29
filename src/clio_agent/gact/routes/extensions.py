"""Shared extension creation, portable uploads and explicit public MCP discovery."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import zipfile
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from clio_agent.gact.agents.resolution import plugin_service
from clio_agent.gact.plugin_authoring import installed_changed
from clio_agent.gact.routes._body import json_body
from clio_agent.plugins.local import (
    MAX_BYTES,
    PRIVATE_MARKETPLACE,
    archive_files,
    create_local,
)
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

    @app.get("/v1/plugins/local")
    async def local_library() -> dict[str, Any]:
        """Locate the private collection and read saved agents without installing sources."""
        store = plugin_service(app).store

        def inventory() -> dict[str, Any]:
            with store.lock:
                root = store.root.resolve()
                agents_directory = root / "user-agents"
                return {
                    "root": str(root),
                    "agents_directory": str(agents_directory),
                    "plugins_directory": str(root / "local" / PRIVATE_MARKETPLACE / "plugins"),
                    "agents": [
                        {
                            "ref": ref,
                            "path": str(agents_directory / f"{agent.id}.json"),
                            "definition": agent.to_dict(),
                        }
                        for ref, agent in store.user_agents().items()
                        # Never report a derived path outside the agent directory.
                        if (agents_directory / f"{agent.id}.json").resolve().parent
                        == agents_directory
                    ],
                }

        try:
            return await asyncio.to_thread(inventory)
        except OSError:
            raise HTTPException(
                409, "The agent host could not read the private collection"
            ) from None

    @app.post("/v1/plugins/upload", status_code=201)
    async def upload(request: Request, name: str, kind: str = "") -> dict[str, Any]:
        if kind not in {"", "skill", "plugin"}:
            raise HTTPException(422, "Choose a skill or plugin upload")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES * 4 // 3 + 1024:
                raise HTTPException(413, "Upload a ZIP smaller than 2 MiB")
        try:
            payload = json.loads(raw)
            encoded = base64.b64decode(payload["archive"], validate=True)
            files = await asyncio.to_thread(archive_files, encoded)
            if kind == "skill" and "SKILL.md" not in files:
                raise ValueError("Upload a skill folder with SKILL.md at its root")
            if kind == "plugin" and not (
                {"plugin.json", ".claude-plugin/plugin.json"} & files.keys()
            ):
                raise ValueError(
                    "Upload a plugin folder containing plugin.json or .claude-plugin/plugin.json"
                )
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise HTTPException(422, str(exc)) from None
        return await install({"name": name, "files": files})

    @app.get("/v1/plugins/local/export")
    async def export_local() -> Response:
        """Export portable source files for an explicit, user-managed GitHub backup."""
        service = plugin_service(app)

        def archive() -> bytes:
            root = service.store.root / "local" / PRIVATE_MARKETPLACE
            with service.store.lock:
                if not root.is_dir():
                    raise HTTPException(404, "Create a local skill or plugin first")
                output = io.BytesIO()
                size = 0
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                    for path in sorted(root.rglob("*")):
                        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                            raise HTTPException(
                                409, "The local marketplace contains an external link"
                            )
                        if path.is_file():
                            size += path.stat().st_size
                            if size > 16 * MAX_BYTES:
                                raise HTTPException(
                                    413,
                                    "Use the marketplace folder to back up this larger collection",
                                )
                            zipped.write(path, path.relative_to(root).as_posix())
                return output.getvalue()

        return Response(
            await asyncio.to_thread(archive),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="private-local-marketplace.zip"'},
        )

    @app.get("/v1/extensions/mcp-registry")
    async def registry(search: str = "", cursor: str = "") -> dict[str, Any]:
        if len(search) > 160 or len(cursor) > 2048:
            raise HTTPException(422, "Search or page cursor is too long")
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                async with client.stream(
                    "GET",
                    "https://registry.modelcontextprotocol.io/v0.1/servers",
                    params={
                        "search": search,
                        "limit": 24,
                        "version": "latest",
                        "cursor": cursor,
                    },
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
                502,
                "The MCP Registry could not be read. Retry or add a custom connection",
            ) from None
