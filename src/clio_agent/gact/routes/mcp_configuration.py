"""Durable user-level MCP configuration routes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.routes._body import json_body
from clio_agent.gact.routes.mcp_server_specs import stdio_server_spec
from clio_agent.tools.mcp_config import transport_from_spec
from clio_agent.tools.mcp_redaction import redact_mcp_spec
from clio_agent.tools.mcp_runtime import make_mcp_client


def _configuration_name(value: str) -> str:
    """Validate one user-level MCP declaration key."""

    name = str(value or "").strip()
    if not name or len(name) > 128 or any(char in name for char in ("/", "\\", "\x00")):
        raise HTTPException(status_code=422, detail="Invalid MCP configuration name")
    return name


def _configuration_spec(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize a declaration through the public install grammar."""

    transport_kind = str(body.get("transport") or "stdio").lower()
    if transport_kind == "stdio":
        return stdio_server_spec(body)
    if transport_kind in {"http", "streamable-http"}:
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=422, detail="http transport requires 'url'")
        return {"transport": "http", "url": url.strip()}
    raise HTTPException(
        status_code=422,
        detail=f"unknown transport: {transport_kind!r} (use stdio|http)",
    )


def _configured_web_remote_url(spec: Mapping[str, Any]) -> str:
    """Return the explicit ``--remote-url`` value without discovery."""

    args = spec.get("args")
    if not isinstance(args, (list, tuple)):
        return ""
    values = [str(value) for value in args]
    try:
        index = values.index("--remote-url")
    except ValueError:
        return ""
    return values[index + 1].strip() if index + 1 < len(values) else ""


async def _probe_web_search_remote(remote_url: str) -> None:
    """Require readiness from the exact configured high-capacity service."""

    if not remote_url.startswith(("http://", "https://")):
        raise ValueError("Web Search remote URL must use http:// or https://")
    ready_url = urljoin(remote_url.rstrip("/") + "/", "readyz")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(ready_url)
    response.raise_for_status()
    payload = response.json()
    checks = payload.get("checks") if isinstance(payload, Mapping) else None
    if not isinstance(checks, Mapping) or checks.get("docling") != "ready":
        raise RuntimeError("Web Search document conversion is not ready")
    unavailable = sorted(str(name) for name, state in checks.items() if state != "ready")
    if unavailable:
        raise RuntimeError(f"Web Search dependency not ready: {', '.join(unavailable)}")


async def _probe_user_mcp_server(spec: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """Return live tools and a degraded detail for one saved spec."""

    try:
        transport = transport_from_spec(dict(spec))
        async with make_mcp_client(transport, server_id="user-configuration-probe") as client:
            tools = await client.list_tools()
        tool_names = [str(tool.name) for tool in tools]
    except Exception as exc:  # noqa: BLE001 - saved configuration remains durable
        return [], f"{type(exc).__name__}: {exc}"

    remote_url = _configured_web_remote_url(spec)
    if not remote_url:
        return tool_names, None
    try:
        await _probe_web_search_remote(remote_url)
    except Exception as exc:  # noqa: BLE001 - preserve tools while surfacing readiness
        return tool_names, f"{type(exc).__name__}: {exc}"
    return tool_names, None


def _forget_ephemeral_duplicates(app: FastAPI, name: str) -> None:
    """Remove process-only rows now owned by durable configuration."""

    installed = getattr(app.state, "external_mcp_servers", {}) or {}
    duplicates = [
        server_id
        for server_id, info in installed.items()
        if str(info.get("name") or "").strip().casefold() == name.casefold()
        or str(server_id).strip().casefold() == name.casefold()
    ]
    for server_id in duplicates:
        installed.pop(server_id, None)


async def _configuration_row(name: str, spec: Mapping[str, Any] | None) -> dict[str, Any]:
    """Shape and live-probe one durable configuration row."""

    if spec is None:
        return {
            "name": name,
            "configured": False,
            "scope": "user",
            "status": "local_fallback",
            "tools": [],
            "tools_count": 0,
            "retryable": False,
        }
    tools, error = await _probe_user_mcp_server(spec)
    row: dict[str, Any] = {
        "name": name,
        "configured": True,
        "scope": "user",
        "status": "ready" if error is None else "degraded",
        "transport": str(spec.get("transport") or "stdio"),
        "spec": redact_mcp_spec(dict(spec)),
        "tools": tools,
        "tools_count": len(tools),
        "retryable": error is not None,
    }
    if error is not None:
        row["error"] = error
    return row


def register_mcp_configuration_routes(app: FastAPI) -> None:
    """Register GET, PUT, and DELETE for durable user MCP declarations."""

    from clio_agent.gact.mcp_user_configuration import (
        McpUserConfigurationError,
        get_user_mcp_server,
        remove_user_mcp_server,
        set_user_mcp_server,
    )

    @app.get("/v1/mcp/configuration/{name}")
    async def get_mcp_configuration(name: str) -> dict[str, Any]:
        key = _configuration_name(name)
        try:
            spec = await asyncio.to_thread(get_user_mcp_server, key)
        except McpUserConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _configuration_row(key, spec)

    @app.put("/v1/mcp/configuration/{name}")
    async def put_mcp_configuration(name: str, request: Request) -> dict[str, Any]:
        key = _configuration_name(name)
        body = await json_body(request, route="PUT /v1/mcp/configuration/{name}")
        spec = _configuration_spec(body)
        try:
            saved = await asyncio.to_thread(set_user_mcp_server, key, spec)
        except McpUserConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _forget_ephemeral_duplicates(app, key)
        display_name = str(body.get("name") or "").strip()
        if display_name:
            _forget_ephemeral_duplicates(app, display_name)
        return await _configuration_row(key, saved)

    @app.delete("/v1/mcp/configuration/{name}")
    async def delete_mcp_configuration(name: str) -> dict[str, Any]:
        key = _configuration_name(name)
        try:
            removed = await asyncio.to_thread(remove_user_mcp_server, key)
        except McpUserConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _forget_ephemeral_duplicates(app, key)
        return {
            "name": key,
            "configured": False,
            "removed": removed,
            "scope": "user",
            "status": "local_fallback",
            "tools": [],
            "tools_count": 0,
            "retryable": False,
        }
