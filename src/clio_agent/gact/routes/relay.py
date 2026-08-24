"""Relay configuration and live reachability route registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.relay_status import probe_relay_status
from clio_agent.gact.routes._body import NonObjectBodyError, json_body

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_relay_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the relay status surface on an application.

    Args:
        app: GACT FastAPI application receiving the route.
        deps: Shared route dependencies; unused by this process-level concern.
    """
    del deps

    # Each FastAPI host owns one process-level override.  App construction must
    # begin from the deployment's configured state rather than inheriting a
    # previous in-process test/application instance.
    from clio_agent.tools.relay_factory import reset_runtime_relay_override

    reset_runtime_relay_override()

    @app.get("/v1/relay/status")
    async def relay_status() -> dict[str, Any]:
        """Return configured relay identity and a fresh bounded TCP probe."""
        return await probe_relay_status()

    @app.put("/v1/relay/configuration")
    async def configure_relay(request: Request) -> dict[str, Any]:
        """Attach relay access for the lifetime of this agent process."""
        try:
            body = await json_body(
                request,
                route="PUT /v1/relay/configuration",
                non_object="raise",
                null_is_empty=False,
            )
        except NonObjectBodyError as exc:
            raise HTTPException(status_code=422, detail="Expected a connection object") from exc

        mcp_url = _relay_url(body.get("mcp_url"), field="Control service address")
        http_url = _relay_url(body.get("http_url"), field="Job service address")
        credential = body.get("access_token")
        if credential is not None and not isinstance(credential, str):
            raise HTTPException(status_code=422, detail="Access credential must be text")
        if isinstance(credential, str) and len(credential) > 8192:
            raise HTTPException(status_code=422, detail="Access credential is too long")

        from clio_agent.gact.relay_wiring import (
            configure_relay_expert_invokers,
            invalidate_relay_tool_surfaces,
        )
        from clio_agent.tools.relay_factory import configure_runtime_relay

        try:
            configure_runtime_relay(
                mcp_url=mcp_url,
                http_url=http_url,
                api_token=credential,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        invalidate_relay_tool_surfaces(
            app,
            status={"configured": True, "reason": "relay_catalog_refresh_pending"},
        )
        configure_relay_expert_invokers(app)
        return await probe_relay_status()

    @app.delete("/v1/relay/configuration")
    async def disconnect_relay() -> dict[str, Any]:
        """Detach relay access until this agent process restarts."""
        from clio_agent.gact.relay_wiring import (
            configure_relay_expert_invokers,
            invalidate_relay_tool_surfaces,
        )
        from clio_agent.tools.relay_factory import disconnect_runtime_relay

        disconnect_runtime_relay()
        invalidate_relay_tool_surfaces(
            app,
            status={
                "configured": False,
                "reason": "relay_tools_not_configured",
                "details": {"missing": ["api_token", "http_url", "mcp_url"]},
            },
        )
        configure_relay_expert_invokers(app)
        return await probe_relay_status()


def _relay_url(value: object, *, field: str) -> str:
    """Validate one relay URL without allowing embedded credentials."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field} is required")
    normalized = value.strip()
    if len(normalized) > 2048:
        raise HTTPException(status_code=422, detail=f"{field} is too long")
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=422, detail=f"{field} must be an HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status_code=422, detail=f"{field} cannot contain credentials")
    return normalized
