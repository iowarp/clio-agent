"""Durable user-level MCP configuration routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request

from clio_agent import conf
from clio_agent.gact.mcp_user_configuration import mcp_server_arg_value
from clio_agent.gact.resource_processing import DocumentProcessorClient
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.routes.mcp_server_specs import stdio_server_spec
from clio_agent.tools.mcp_config import MCPTransportError, spec_from_declaration, transport_for
from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage
from clio_agent.tools.mcp_probe_hardening import probe_server_context
from clio_agent.tools.mcp_redaction import redact_mcp_error, redact_mcp_spec
from clio_agent.tools.mcp_runtime import make_mcp_client

logger = logging.getLogger(__name__)


def _configuration_name(value: str) -> str:
    """Validate one user-level MCP declaration key."""

    name = str(value or "").strip()
    if not name or len(name) > 128 or any(char in name for char in ("/", "\\", "\x00")):
        raise HTTPException(status_code=422, detail="Invalid MCP configuration name")
    return name


def _configuration_spec(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize a declaration through the public install grammar."""

    transport_kind = str(body.get("transport") or "stdio").lower()
    connection_only = body.get("connection_only")
    if connection_only is not None and not isinstance(connection_only, bool):
        raise HTTPException(422, "connection_only must be a boolean")
    if connection_only and (body.get("always_load") or body.get("alwaysLoad")):
        raise HTTPException(
            422, "A connection available only through plugins cannot be always loaded"
        )
    if transport_kind == "stdio":
        return {
            **stdio_server_spec(body),
            **({"connection_only": connection_only} if connection_only is not None else {}),
        }
    if transport_kind in {"http", "streamable-http", "sse"}:
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=422, detail="http transport requires 'url'")
        parsed = urlsplit(url.strip())
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise HTTPException(
                422, "Use an HTTP or HTTPS MCP endpoint without userinfo or a fragment"
            )
        headers = body.get("headers") or {}
        if (
            not isinstance(headers, dict)
            or not all(
                isinstance(key, str)
                and isinstance(value, str)
                and "\n" not in key + value
                and "\r" not in key + value
                for key, value in headers.items()
            )
            or len(headers) > 16
        ):
            raise HTTPException(
                422, "Request headers must contain at most 16 single-line name/value pairs"
            )
        spec: dict[str, Any] = {
            "transport": "sse" if transport_kind == "sse" else "http",
            "url": url.strip(),
            "always_load": bool(body.get("always_load") or body.get("alwaysLoad")),
        }
        if headers:
            spec["headers"] = headers
        if connection_only is not None:
            spec["connection_only"] = connection_only
        if body.get("authentication") == "oauth":
            from clio_agent.gact.mcp_auth import managed_oauth  # noqa: PLC0415

            spec["auth"] = managed_oauth()
        return spec
    raise HTTPException(
        status_code=422,
        detail=f"unknown transport: {transport_kind!r} (use stdio|http)",
    )


def _configured_web_remote_url(spec: Mapping[str, Any]) -> str:
    """Return the explicit ``--remote-url`` value without discovery."""

    return mcp_server_arg_value(spec, "--remote-url")


def _sync_document_processor(app: FastAPI, remote_url: str) -> None:
    """Point uploaded-source conversion at the same configured Web Search service."""

    factory = getattr(app.state, "resource_converter_factory", None)
    if factory is None:
        return
    current = factory.get(DocumentProcessorClient.id)
    max_resource_bytes = int(getattr(current, "max_resource_bytes", 0) or 0)
    fallback_url = conf.resolve(
        "resources.document_processor_url",
        env="CLIO_DOCUMENT_PROCESSOR_URL",
        default="",
        cast=conf.as_str,
    )
    factory.register(
        DocumentProcessorClient(remote_url or fallback_url, max_resource_bytes=max_resource_bytes)
    )


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


#: Launchers that download and build the server on its first start (``uvx git+...``
#: can take a minute): the person is waiting on this probe, so it waits that long too.
_FETCHING_LAUNCHERS = frozenset({"uvx", "uv", "npx", "pnpx", "bunx", "pipx"})
_FIRST_START_PROBE_RETRIES = 12


async def _probe_user_mcp_server(spec: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """Return live tools and a degraded detail for one saved spec."""

    transport_spec = dict(spec)
    # A read-only check may discover that sign-in is required, but must never
    # register an OAuth client or start consent merely because a list was opened.
    if (
        spec.get("auth")
        and not DurableFileTokenStorage(
            str(spec.get("url") or ""), connection_id=(spec.get("auth") or {}).get("connection_id")
        ).has_tokens()
    ):
        transport_spec.pop("auth", None)
    if "transport" not in transport_spec:
        transport_spec["transport"] = str(spec.get("type") or "").strip().lower() or (
            "http" if spec.get("url") and not spec.get("command") else "stdio"
        )
    launcher = Path(str(spec.get("command") or "")).name
    fetching = transport_spec["transport"] == "stdio" and launcher in _FETCHING_LAUNCHERS
    normalized = None
    try:
        normalized = spec_from_declaration("configuration", transport_spec)
        if not normalized.usable:
            raise MCPTransportError("; ".join(normalized.validation_errors))
        transport = transport_for(normalized)
        with probe_server_context(
            "", timeout_retries=_FIRST_START_PROBE_RETRIES if fetching else None
        ):
            async with make_mcp_client(transport, server_id="user-configuration-probe") as client:
                tools = await client.list_tools()
        tool_names = [str(tool.name) for tool in tools]
    except Exception as exc:  # noqa: BLE001 - saved configuration remains durable
        # MCP 2 can turn an HTTP failure into a JSON-RPC error without retaining
        # the response. Read only challenge headers without client registration
        # or credentialed redirects. POST-only MCP servers reject GET before
        # authentication; use a harmless protocol ping for that case.
        if spec.get("url"):
            try:
                async with httpx.AsyncClient(timeout=5, follow_redirects=False) as probe:
                    headers = {
                        **dict(spec.get("headers") or {}),
                        "Accept": "application/json, text/event-stream",
                    }
                    async with probe.stream(
                        "GET",
                        str(spec["url"]),
                        headers=headers,
                    ) as response:
                        status = response.status_code
                    if status == 405:
                        async with probe.stream(
                            "POST",
                            str(spec["url"]),
                            headers=headers,
                            json={"jsonrpc": "2.0", "id": "clio-auth-discovery", "method": "ping"},
                        ) as response:
                            status = response.status_code
                    if status == 401:
                        return (
                            [],
                            "mcp_auth_required: Sign in or configure the server's required request headers.",
                        )
            except httpx.HTTPError:
                pass  # The original handshake error is the actionable failure below.
        pending: list[BaseException] = [exc]
        seen: set[int] = set()
        while pending and len(seen) < 32:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            if getattr(getattr(error, "response", None), "status_code", None) == 401:
                return [], "mcp_auth_required: Sign in to use this connection."
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
            if error.__cause__ is not None:
                pending.append(error.__cause__)
            elif error.__context__ is not None:
                pending.append(error.__context__)
        detail = redact_mcp_error(
            exc,
            {"url": normalized.url, "headers": normalized.headers, "env": normalized.env}
            if normalized is not None
            else spec,
        )
        if fetching and "timed out" in str(exc):
            detail += f" ({launcher} may still be preparing the server on its first start; retry)"
        return [], detail

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


async def _refresh_agent_tool_gateway(app: FastAPI) -> None:
    """Apply a saved user MCP change to new turns without restarting CLIO."""

    agent = getattr(app.state, "agent", None)
    refresh = getattr(agent, "refresh_declared_mcp_servers", None)
    if not callable(refresh):
        return
    replaced = await asyncio.to_thread(refresh)
    in_flight = [
        task
        for task in (getattr(app.state, "in_flight_turns", {}) or {}).values()
        if isinstance(task, asyncio.Task) and not task.done()
    ]

    async def _retire() -> None:
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
        close = getattr(replaced, "close", None)
        if callable(close):
            try:
                await asyncio.to_thread(close)
            except Exception as exc:  # noqa: BLE001 - replacement is already live
                logger.warning("replaced MCP executor retirement failed: %r", exc)

    asyncio.create_task(_retire())


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
        "status": "ready"
        if error is None
        else "auth_required"
        if error.startswith("mcp_auth_required:")
        else "degraded",
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
        list_user_mcp_servers,
        remove_user_mcp_server,
        set_user_mcp_server,
        user_mcp_labels,
        validate_user_mcp_server_name,
    )
    from clio_agent.gact.routes.mcp_auth import register_mcp_auth_routes, sign_ins

    register_mcp_auth_routes(app)

    @app.get("/v1/mcp/configuration")
    async def list_mcp_configuration() -> dict[str, Any]:
        """The services the person saved, without starting them (a row's GET probes it)."""

        try:
            servers = await asyncio.to_thread(list_user_mcp_servers)
            labels = await asyncio.to_thread(user_mcp_labels)
        except McpUserConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "servers": [
                {
                    "name": name,
                    "label": labels.get(name, name),
                    "configured": True,
                    "scope": "user",
                    "transport": str(spec.get("transport") or "stdio"),
                    "always_load": bool(spec.get("always_load")),
                    "spec": redact_mcp_spec(dict(spec)),
                }
                for name, spec in sorted(servers.items())
            ]
        }

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
        try:
            validate_user_mcp_server_name(key)
        except McpUserConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        body = await json_body(request, route="PUT /v1/mcp/configuration/{name}")
        spec = _configuration_spec(body)
        if spec.get("auth"):
            spec["auth"]["connection_id"] = key
        await sign_ins(app).cancel_name(key)
        try:
            saved = await asyncio.to_thread(
                set_user_mcp_server, key, spec, label=str(body.get("name") or key)
            )
        except McpUserConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _forget_ephemeral_duplicates(app, key)
        display_name = str(body.get("name") or "").strip()
        if display_name:
            _forget_ephemeral_duplicates(app, display_name)
        if key == "web":
            _sync_document_processor(app, _configured_web_remote_url(saved))
        await _refresh_agent_tool_gateway(app)
        return await _configuration_row(key, saved)

    @app.delete("/v1/mcp/configuration/{name}")
    async def delete_mcp_configuration(name: str) -> dict[str, Any]:
        key = _configuration_name(name)
        await sign_ins(app).cancel_name(key)
        try:
            previous = await asyncio.to_thread(get_user_mcp_server, key)
            removed = await asyncio.to_thread(remove_user_mcp_server, key)
        except McpUserConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if removed and previous and previous.get("url") and previous.get("auth"):
            storage = DurableFileTokenStorage(
                str(previous["url"]),
                connection_id=previous["auth"].get("connection_id"),
            )
            await asyncio.to_thread(storage.clear)
        _forget_ephemeral_duplicates(app, key)
        if key == "web":
            _sync_document_processor(app, "")
        await _refresh_agent_tool_gateway(app)
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
