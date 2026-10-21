"""Plugin, agent and session-agent routes.

* ``/v1/plugins`` -- installed plugins and packs (the + menu's catalog),
  marketplaces, install, the trust step, update, uninstall and ``plugin check``.
* ``/v1/agents`` -- installed agents and user agents (parent plus delta),
  resolution previews (provenance and the composed prompt) and the cascade
  preview for removing a plugin.
* ``/v1/sessions/{sid}/agent`` -- the session's pinned resolution; a PATCH adds
  or removes plugins inside the live session and pins the new resolution.

Every failure is a typed error envelope carrying the resolver's diagnostics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.agents.resolution import (
    AGENT_DELTA_KEY,
    SUBAGENT_KEY,
    SessionAgentUnavailable,
    pin_session_agent,
    plugin_service,
    session_delta,
    session_resolution,
)
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.plugins.check import check
from clio_agent.plugins.records import PluginRecord
from clio_agent.plugins.resolve import Delta, Resolution, ResolutionError
from clio_agent.plugins.service import AgentNotFound
from clio_agent.plugins.store import StoreError

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _error(status: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, StoreError):
        status = (
            404 if exc.code.endswith("not_installed") or exc.code.endswith("not_found") else 422
        )
        return _error(status, exc.code, str(exc))
    if isinstance(exc, AgentNotFound):
        return _error(404, exc.reason, str(exc), ref=exc.ref)
    if isinstance(exc, ResolutionError):
        return _error(
            422,
            "agent_resolution_failed",
            str(exc),
            errors=[row.to_dict() for row in exc.diagnostics],
        )
    if isinstance(exc, SessionAgentUnavailable):
        return _error(422, exc.reason, str(exc), ref=exc.ref, errors=exc.errors)
    if isinstance(exc, ValueError):
        return _error(422, "invalid_request", str(exc))
    raise exc


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a store or resolver call off the loop and map its typed errors."""

    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (StoreError, AgentNotFound, ResolutionError, SessionAgentUnavailable, ValueError) as exc:
        raise _translate(exc) from exc


def plugin_row(record: PluginRecord, *, trusted: bool, pending: bool = False) -> dict[str, Any]:
    """A plugin or pack as the + menu lists it."""

    return {
        "id": record.id,
        "ref": f"{record.marketplace}/{record.id}",
        "version": record.version,
        "description": record.description,
        "kind": record.kind,
        "format": record.format,
        "marketplace": record.marketplace,
        "requires": list(record.requires),
        "members": list(record.members),
        "tools": record.tool_names(),
        "skills": [row.id for row in record.skills],
        "mcp_servers": sorted(record.mcp_servers),
        "subagents": [row.id for row in record.subagents],
        "flows": [row.id for row in record.flows],
        "hooks": [row.id for row in record.hooks],
        "commands": [row.id for row in record.commands],
        "catalogs": [row.id for row in record.catalogs],
        "fragments": [row.id for row in record.fragments],
        "trusted": trusted,
        "pending_trust": pending,
        "diagnostics": [row.to_dict() for row in record.diagnostics],
    }


def resolution_view(resolution: Resolution) -> dict[str, Any]:
    """A resolution with the composed prompt's sections attributed to their plugins."""

    return {**resolution.to_dict(), "tool_names": resolution.tool_names}


def _delta_from(body: Mapping[str, Any]) -> Delta:
    def values(key: str) -> tuple[str, ...]:
        raw = body.get(key) or []
        if not isinstance(raw, list):
            raise ValueError(f"{key} must be a list of plugin references")
        return tuple(str(value) for value in raw)

    return Delta(values("add"), values("remove"))


def _query_delta(request: Request) -> Delta:
    params = request.query_params
    return Delta(tuple(params.getlist("add")), tuple(params.getlist("remove")))


def register_plugin_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the plugin, agent and session-agent routes."""

    # ----------------------------------------------------------- plugins
    @app.get("/v1/plugins")
    async def list_plugins() -> dict[str, Any]:
        service = plugin_service(app)

        def rows() -> dict[str, Any]:
            service.ready()
            store = service.store
            pending = store.pending()
            plugins = [plugin_row(r, trusted=store.trusted(r)) for r in store.records()]
            plugins.extend(plugin_row(r, trusted=False, pending=True) for r in pending)
            return {"plugins": plugins, "default_marketplace_error": service.ready_error}

        return await _call(rows)

    @app.get("/v1/plugins/marketplaces")
    async def list_marketplaces() -> dict[str, Any]:
        service = plugin_service(app)

        def rows() -> list[dict[str, Any]]:
            service.ready()
            out = []
            for name, entry in sorted(service.store.marketplaces().items()):
                out.append(
                    {
                        "name": name,
                        "source": entry.get("source", ""),
                        "commit": entry.get("commit", ""),
                        "format": entry.get("format", ""),
                        "trusted": bool(entry.get("trusted")),
                        "trust_reason": entry.get("trust_reason", ""),
                        "default": bool(entry.get("default")),
                        "parent": entry.get("parent", ""),
                        "installed_at": entry.get("installed_at", ""),
                        "diagnostics": entry.get("diagnostics") or [],
                    }
                )
            return out

        return {"marketplaces": await _call(rows)}

    @app.post("/v1/plugins/marketplaces", status_code=201)
    async def install_marketplace(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/plugins/marketplaces")
        source = str(body.get("source") or "").strip()
        if not source:
            raise _error(422, "invalid_request", "source is required")
        service = plugin_service(app)
        await _call(service.ready)
        summary = await _call(
            service.store.install,
            source,
            ref=str(body.get("ref") or ""),
            commit=str(body.get("commit") or ""),
            name=str(body.get("name") or ""),
        )
        summary.pop("federated_pins", None)
        return summary

    @app.post("/v1/plugins/marketplaces/{name}/trust")
    async def trust_marketplace(name: str) -> dict[str, Any]:
        return await _call(plugin_service(app).store.trust, name)

    @app.post("/v1/plugins/marketplaces/{name}/update")
    async def update_marketplace(name: str) -> dict[str, Any]:
        summary = await _call(plugin_service(app).store.update, name)
        summary.pop("federated_pins", None)
        return summary

    @app.delete("/v1/plugins/marketplaces/{name}")
    async def uninstall_marketplace(name: str) -> dict[str, Any]:
        deps.guard_direct_destructive_action(
            app,
            tool_name="gact.plugin.marketplace.delete",
            args={"marketplace": name},
            summary=f"uninstall marketplace {name}",
            reason="user_requested_marketplace_uninstall",
        )
        await _call(plugin_service(app).store.uninstall, name)
        return {"uninstalled": name}

    @app.post("/v1/plugins/check")
    async def check_plugins(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/plugins/check")
        path = str(body.get("path") or "").strip()
        installed = str(body.get("installed") or "").strip()
        if not path and not installed:
            raise _error(422, "invalid_request", "path or installed is required")
        service = plugin_service(app)
        report = await _call(
            check, Path(path).expanduser() if path else None, installed=installed, service=service
        )
        return {**report.to_dict(), "ok": report.ok, "text": report.render()}

    # ------------------------------------------------------------ agents
    @app.get("/v1/agents")
    async def list_agents() -> dict[str, Any]:
        service = plugin_service(app)
        entries = await _call(service.agents)
        return {"agents": [entry.to_dict() for entry in entries]}

    @app.get("/v1/agents/resolve")
    async def resolve_agent_preview(request: Request, ref: str = "") -> dict[str, Any]:
        """Resolve an agent with an optional delta (``?add=..&remove=..``) without pinning."""

        service = plugin_service(app)
        resolution = await _call(service.resolve, ref, delta=_query_delta(request), pin=False)
        return resolution_view(resolution)

    @app.post("/v1/agents/preview-removal")
    async def preview_removal(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/agents/preview-removal")
        plugin = str(body.get("plugin") or "").strip()
        if not plugin:
            raise _error(422, "invalid_request", "plugin is required")
        delta = await _call(_delta_from, body.get("delta") or {})
        return await _call(
            plugin_service(app).preview_removal, str(body.get("ref") or ""), plugin, delta=delta
        )

    @app.post("/v1/agents/user", status_code=201)
    async def save_user_agent(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/agents/user")
        agent_id = str(body.get("id") or "").strip()
        extends = str(body.get("extends") or "").strip()
        if not agent_id or not extends:
            raise _error(422, "invalid_request", "id and extends are required")
        delta = await _call(_delta_from, body)
        entry = await _call(
            plugin_service(app).save_user_agent,
            agent_id,
            extends=extends,
            add=list(delta.add),
            remove=list(delta.remove),
            name=str(body.get("name") or ""),
            description=str(body.get("description") or ""),
        )
        return entry.to_dict()

    @app.delete("/v1/agents/user/{agent_id}")
    async def delete_user_agent(agent_id: str) -> dict[str, Any]:
        deps.guard_direct_destructive_action(
            app,
            tool_name="gact.agent.user.delete",
            args={"agent_id": agent_id},
            summary=f"delete user agent {agent_id}",
            reason="user_requested_user_agent_delete",
        )
        await _call(plugin_service(app).store.delete_user_agent, agent_id)
        return {"deleted": f"user/{agent_id}"}

    # ----------------------------------------------------- session agent
    def _session_or_404(sid: str) -> Any:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _error(404, "not_found", f"session not found: {sid}", session_id=sid)
        return sess

    @app.get("/v1/sessions/{sid}/agent")
    async def get_session_agent(sid: str) -> dict[str, Any]:
        sess = _session_or_404(sid)
        resolution = await _call(session_resolution, app, sid)
        metadata = sess.metadata if isinstance(sess.metadata, Mapping) else {}
        return {
            "session_id": sid,
            "delta": session_delta(app, sid).to_dict(),
            "subagent": bool(metadata.get(SUBAGENT_KEY)),
            "resolution": resolution_view(resolution),
        }

    @app.patch("/v1/sessions/{sid}/agent")
    async def patch_session_agent(sid: str, request: Request) -> dict[str, Any]:
        """Change the session's agent or its live delta, then pin the new resolution.

        ``{"ref": ..., "add": [...], "remove": [...]}``. ``add``/``remove``
        replace the session's delta (the + menu sends the whole delta).
        """

        _session_or_404(sid)
        body = await json_body(request, route="PATCH /v1/sessions/{sid}/agent")
        ref = str(body.get("ref") or "").strip() or None
        delta = (
            await _call(_delta_from, body)
            if "add" in body or "remove" in body
            else session_delta(app, sid)
        )
        resolution = await _call(pin_session_agent, app, sid, ref=ref, delta=delta)
        sess = app.state.sessions.get(sid)
        metadata = sess.metadata if sess is not None and isinstance(sess.metadata, Mapping) else {}
        return {
            "session_id": sid,
            "delta": dict(metadata.get(AGENT_DELTA_KEY) or {}),
            "subagent": False,
            "resolution": resolution_view(resolution),
        }
