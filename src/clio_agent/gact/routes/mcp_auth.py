"""User-owned browser authorization for saved remote MCP connections."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.mcp_auth import McpSignIns, managed_oauth
from clio_agent.gact.mcp_user_configuration import get_user_mcp_server, set_user_mcp_server
from clio_agent.gact.routes._body import json_body
from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage


def sign_ins(app: FastAPI) -> McpSignIns:
    """Return this application instance's bounded sign-in owner."""
    owner = getattr(app.state, "mcp_sign_ins", None)
    if owner is None:

        async def connected() -> None:
            from clio_agent.gact.routes.mcp_configuration import (
                _refresh_agent_tool_gateway,  # noqa: PLC0415
            )

            await _refresh_agent_tool_gateway(app)

        owner = app.state.mcp_sign_ins = McpSignIns(connected)
    return owner


def _redirect_uri(value: Any, origin: str) -> str:
    """Accept the initiating web UI or Desktop's loopback callback, never userinfo/fragment."""
    uri = str(value or "")
    parsed = urlsplit(uri)
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("Use the CLIO sign-in return address")
    is_loopback = (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.path == "/clio-storage-return"
        and parsed.port is not None
    )
    is_web = (
        bool(origin)
        and f"{parsed.scheme}://{parsed.netloc}" == origin.rstrip("/")
        and parsed.scheme in {"http", "https"}
        and parsed.path == "/oauth/mcp"
    )
    if not uri or not (is_loopback or is_web):
        raise ValueError("The return address must belong to this CLIO window or its local receiver")
    return uri


def register_mcp_auth_routes(app: FastAPI) -> None:
    """Register explicit start/status/return/cancel/forget operations, without token APIs."""

    @app.post("/v1/mcp/configuration/{name}/sign-in")
    async def start(name: str, request: Request) -> dict[str, Any]:
        import asyncio  # noqa: PLC0415

        body = await json_body(request, route="MCP sign-in")
        try:
            redirect_uri = _redirect_uri(
                body.get("redirect_uri"), request.headers.get("origin", "")
            )
            spec = await asyncio.to_thread(get_user_mcp_server, name)
            if not spec or not spec.get("url"):
                raise ValueError("Save a remote MCP connection before signing in")
            await sign_ins(app).cancel_name(name)
            auth = managed_oauth(redirect_uri, connection_id=name)
            metadata_url = str(body.get("client_metadata_url") or "").strip() or None
            if metadata_url:
                parsed = urlsplit(metadata_url)
                if parsed.scheme != "https" or not parsed.netloc or parsed.path in {"", "/"}:
                    raise ValueError("Published client metadata must be an HTTPS document URL")
                auth["client_metadata_url"] = metadata_url
            await asyncio.to_thread(set_user_mcp_server, name, {**spec, "auth": auth})
            return await sign_ins(app).start(
                name,
                redirect_uri,
                client_id=str(body.get("client_id") or ""),
                client_secret=str(body.get("client_secret") or ""),
                client_metadata_url=metadata_url,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/v1/mcp/configuration/{name}/sign-in/{flow_id}")
    async def status(name: str, flow_id: str) -> dict[str, Any]:
        try:
            flow = sign_ins(app).get(flow_id)
            if flow.name != name:
                raise ValueError("Sign-in not found")
            return flow.public()
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/v1/mcp/configuration/{name}/sign-in/{flow_id}/complete")
    async def complete(name: str, flow_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request, route="MCP sign-in return")
        try:
            return await sign_ins(app).complete(name, flow_id, str(body.get("callback_url") or ""))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/v1/mcp/configuration/{name}/sign-in/{flow_id}")
    async def cancel(name: str, flow_id: str) -> dict[str, Any]:
        try:
            flow = sign_ins(app).get(flow_id)
            if flow.name != name:
                raise ValueError("Sign-in not found")
            await sign_ins(app).cancel(flow_id)
            return flow.public()
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.delete("/v1/mcp/configuration/{name}/authorization")
    async def forget(name: str) -> dict[str, Any]:
        import asyncio  # noqa: PLC0415

        from clio_agent.gact.routes.mcp_configuration import (
            _refresh_agent_tool_gateway,  # noqa: PLC0415
        )

        spec = await asyncio.to_thread(get_user_mcp_server, name)
        if not spec or not spec.get("url"):
            raise HTTPException(404, "Remote MCP connection not found")
        await sign_ins(app).cancel_name(name)
        connection_id = (spec.get("auth") or {}).get("connection_id")
        await asyncio.to_thread(
            DurableFileTokenStorage(str(spec["url"]), connection_id=connection_id).clear
        )
        await _refresh_agent_tool_gateway(app)
        return {"name": name, "state": "signed_out"}
