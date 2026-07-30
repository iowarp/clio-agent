"""Agent-blueprint (and expert-pack) lifecycle routes for the GACT server (#714).

This concern owns the marketplace/install surface for agent blueprints under
``/v1/agent-blueprints`` plus the session-scoped activation routes:

* ``GET/POST/.../DELETE /v1/agent-blueprints/sources*`` -- the registry of
  remote/local blueprint *sources* (git repos or on-disk roots) the user can
  install from. The source ledger persists to ``agent-blueprint-sources.json``
  under the user config dir; ``refresh`` clones/inspects a source to enumerate
  its available blueprints.
* ``GET /v1/agent-blueprints`` / ``GET /v1/agent-blueprints/{id}`` -- discover
  installed blueprints (workspace/global/builtin) and resolve one to its agent
  hierarchy + MCP descriptors.
* ``POST /v1/agent-blueprints/validate`` -- validate a blueprint root on disk.
* ``POST /v1/agent-blueprints/install`` / ``.../{id}/update`` /
  ``DELETE /v1/agent-blueprints/{id}`` -- the install/update/uninstall engine.
* ``POST /v1/agent-blueprints/{id}/mcp/{descriptor_id}/enable`` -- enable a
  blueprint-declared MCP descriptor, optionally probing the live server.
* ``POST /v1/expert-packs/install`` / ``.../{id}/update`` /
  ``DELETE /v1/expert-packs/{id}`` -- thin aliases of the blueprint lifecycle
  (iowarp/clio-agent#663): a blueprint and a pack share ONE install/update/delete
  engine, distinguished by the installed row's ``kind`` field. They delegate to
  the blueprint handlers so there is exactly one implementation.
* ``GET/POST /v1/sessions/{sid}/agent-blueprint`` -- read/set the active
  blueprint for a session (by installed id or by an on-disk path).

The disk-reading lifecycle primitives live in
:mod:`clio_agent.gact.agent_blueprints` (single source); the session-state reads
reuse the byte-identical ``_runtime_*`` helpers in
:mod:`clio_agent.gact.agents.resolution`. The ``build_app``-local seam the
session-set route needs -- the activation-metadata builder -- travels on
:class:`~clio_agent.gact.routes.deps.GactDeps`.
Handlers reach ``app.state`` directly and never import :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException

from clio_agent.gact.agent_blueprints import (
    DEFAULT_AGENT_BLUEPRINT_ID,
    discover_agent_blueprints,
    install_agent_blueprint,
    load_agent_blueprints,
    load_mcp_descriptors,
    parse_agent_blueprint_root,
    uninstall_agent_blueprint,
    update_installed_agent_blueprint,
    validate_agent_blueprint_path,
    validate_agent_hierarchy,
)
from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_path,
    _runtime_session_agent_overlay,
    _runtime_workspace_catalog_cwd,
)
from clio_agent.gact.permission_gate import _normalize_mcp_tool_annotations
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Session

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _agent_blueprint_sources_path() -> Path:
    """Return the on-disk path of the blueprint-source registry JSON."""

    from clio_agent import paths  # noqa: PLC0415

    return paths.user_config_dir() / "agent-blueprint-sources.json"


def _source_registry_id(source: str, ref: str = "") -> str:
    """Derive a stable ``src_*`` id from a source URL/path and optional ref."""

    digest = hashlib.sha256(f"{source}\n{ref}".encode("utf-8")).hexdigest()[:12]
    return f"src_{digest}"


def _load_agent_blueprint_sources() -> list[dict[str, Any]]:
    """Load the persisted blueprint-source rows (empty list if absent/corrupt)."""

    path = _agent_blueprint_sources_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable/invalid sources file yields no rows
        return []
    rows = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _save_agent_blueprint_sources(rows: list[dict[str, Any]]) -> None:
    """Persist the blueprint-source rows to the user config dir."""

    path = _agent_blueprint_sources_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sources": rows}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _agent_blueprint_candidates(root: Path) -> list[dict[str, Any]]:
    """Enumerate installable blueprint roots discovered under ``root``."""

    candidates: list[Path] = []
    if (root / "AGENT.md").exists():
        candidates.append(root)
    if root.is_dir():
        candidates.extend(sorted(path for path in root.iterdir() if (path / "AGENT.md").exists()))
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        parsed = parse_agent_blueprint_root(candidate, scope="marketplace")
        rows.append(
            {
                "id": parsed.id,
                "title": parsed.title,
                "version": parsed.version,
                "enabled": parsed.enabled,
                "validation_errors": list(parsed.validation_errors),
                "definition_path": str(parsed.root_path),
            }
        )
    return rows


def _refresh_agent_blueprint_source(row: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect a source (clone if remote) and refresh its available-blueprint list."""

    source = str(row.get("source") or "").strip()
    ref = str(row.get("ref") or "").strip()
    refreshed = dict(row)
    refreshed["ref"] = ref
    refreshed["available_blueprints"] = []
    if not source:
        return {**refreshed, "status": "error", "error": "source is empty"}
    source_path = Path(source).expanduser()
    refreshed["source_kind"] = "path" if source_path.exists() else "git"
    refreshed["status"] = "ready"
    refreshed["error"] = ""
    try:
        if source_path.exists():
            try:
                refreshed["commit"] = subprocess.check_output(
                    ["git", "-C", str(source_path), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:  # noqa: BLE001 - display commit left blank when git rev-parse unavailable
                refreshed["commit"] = ""
            refreshed["available_blueprints"] = _agent_blueprint_candidates(source_path)
            return refreshed
        with tempfile.TemporaryDirectory(prefix="clio-agent-blueprint-source-") as tmp:
            clone_target = Path(tmp) / "repo"
            cmd = ["git", "clone", "--depth", "1"]
            if ref:
                cmd.extend(["--branch", ref])
            cmd.extend([source, str(clone_target)])
            env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            }
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                env=env,
            )
            refreshed["commit"] = subprocess.check_output(
                ["git", "-C", str(clone_target), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            refreshed["available_blueprints"] = _agent_blueprint_candidates(clone_target)
            return refreshed
    except Exception as exc:  # noqa: BLE001
        refreshed["status"] = "error"
        refreshed["error"] = str(exc)
        return refreshed


def register_blueprints_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the agent-blueprint + expert-pack lifecycle routes on ``app``.

    Handlers are defined inside this factory so they close over the ``app``
    argument FastAPI's decorators require, and reach the activation-metadata
    builder through ``deps`` rather than any
    ``build_app`` local. The expert-pack routes call the blueprint handlers
    directly so a single implementation backs both surfaces.
    """

    @app.get("/v1/agent-blueprints/sources")
    async def list_agent_blueprint_sources() -> dict[str, Any]:
        return {"sources": _load_agent_blueprint_sources()}

    @app.post("/v1/agent-blueprints/sources", status_code=201)
    async def add_agent_blueprint_source(req: dict[str, Any]) -> dict[str, Any]:
        source = str(req.get("source") or req.get("url") or "").strip()
        if not source:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="source or url is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        ref = str(req.get("ref") or "").strip()
        source_id = str(req.get("id") or "").strip() or _source_registry_id(source, ref)
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "id": source_id,
            "name": str(req.get("name") or source),
            "source": source,
            "ref": ref,
            "pinned_commit": str(req.get("pinned_commit") or "").strip(),
            "status": "unknown",
            "added_at": now,
            "updated_at": now,
        }
        if bool(req.get("refresh", True)):
            row = _refresh_agent_blueprint_source(row)
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
        rows = [
            existing
            for existing in _load_agent_blueprint_sources()
            if existing.get("id") != source_id
        ]
        rows.append(row)
        _save_agent_blueprint_sources(rows)
        return {"source": row}

    @app.post("/v1/agent-blueprints/sources/{source_id}/refresh")
    async def refresh_agent_blueprint_source(source_id: str) -> dict[str, Any]:
        rows = _load_agent_blueprint_sources()
        for index, row in enumerate(rows):
            if row.get("id") == source_id:
                refreshed = _refresh_agent_blueprint_source(row)
                refreshed["updated_at"] = datetime.now(timezone.utc).isoformat()
                rows[index] = refreshed
                _save_agent_blueprint_sources(rows)
                return {"source": refreshed}
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"agent blueprint source not found: {source_id}",
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.delete("/v1/agent-blueprints/sources/{source_id}")
    async def delete_agent_blueprint_source(source_id: str) -> dict[str, Any]:
        rows = _load_agent_blueprint_sources()
        kept = [row for row in rows if row.get("id") != source_id]
        if len(kept) == len(rows):
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"agent blueprint source not found: {source_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _save_agent_blueprint_sources(kept)
        return {"deleted": {"id": source_id}}

    @app.get("/v1/agent-blueprints")
    async def list_agent_blueprints(workspace_id: Optional[str] = None) -> dict[str, Any]:
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id or "")
        blueprints = [row.to_wire() for row in discover_agent_blueprints(cwd=cwd)]
        return {"agent_blueprints": blueprints}

    @app.get("/v1/agent-blueprints/{blueprint_id:path}")
    async def get_agent_blueprint(
        blueprint_id: str,
        workspace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id or "")
        for blueprint in discover_agent_blueprints(cwd=cwd):
            if blueprint.id == blueprint_id:
                agents = validate_agent_hierarchy(
                    load_agent_blueprints(cwd=cwd, blueprint_id=blueprint_id),
                    blueprint=blueprint,
                )
                return {
                    "agent_blueprint": blueprint.to_wire(),
                    "agents": [row.model_dump(exclude_none=True) for row in agents],
                    "mcp_descriptors": load_mcp_descriptors(
                        blueprint.root,
                        scope=blueprint.scope,
                        blueprint_id=blueprint.id,
                    ),
                }
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"agent blueprint not found: {blueprint_id}",
                    details={
                        "agent_blueprint_id": blueprint_id,
                        "workspace_id": workspace_id or "",
                    },
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.post("/v1/agent-blueprints/validate")
    async def validate_agent_blueprint(req: dict[str, Any]) -> dict[str, Any]:
        path = str(req.get("path") or "").strip()
        if not path:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="path is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return validate_agent_blueprint_path(Path(path), scope=str(req.get("scope") or "session"))

    @app.post("/v1/agent-blueprints/install", status_code=201)
    async def install_agent_blueprint_route(req: dict[str, Any]) -> dict[str, Any]:
        source_id = str(req.get("source_id") or "").strip()
        source_row: dict[str, Any] = {}
        if source_id:
            source_row = next(
                (row for row in _load_agent_blueprint_sources() if row.get("id") == source_id),
                {},
            )
            if not source_row:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"agent blueprint source not found: {source_id}",
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )
        source = str(
            req.get("source") or req.get("url") or req.get("path") or source_row.get("source") or ""
        ).strip()
        if not source:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="source, url, or path is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        scope = str(req.get("scope") or "workspace").strip()
        if scope not in {"global", "workspace"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="scope must be global or workspace",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=str(req.get("workspace_id") or ""))
        try:
            return install_agent_blueprint(
                source=source,
                scope=scope,  # type: ignore[arg-type]
                cwd=cwd or Path.cwd(),
                ref=str(req.get("ref") or source_row.get("ref") or ""),
                blueprint_id=str(req.get("blueprint_id") or ""),
                pinned_commit=str(
                    req.get("pinned_commit") or source_row.get("pinned_commit") or ""
                ),
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message=f"agent blueprint install failed: {exc}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

    @app.post("/v1/agent-blueprints/{blueprint_id:path}/update")
    async def update_agent_blueprint_route(
        blueprint_id: str,
        req: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = req or {}
        scope = str(body.get("scope") or "workspace").strip()
        if scope not in {"global", "workspace"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="scope must be global or workspace",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=str(body.get("workspace_id") or ""))
        try:
            return update_installed_agent_blueprint(
                blueprint_id=blueprint_id,
                scope=scope,  # type: ignore[arg-type]
                cwd=cwd or Path.cwd(),
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message=f"agent blueprint update failed: {exc}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

    @app.delete("/v1/agent-blueprints/{blueprint_id:path}")
    async def delete_agent_blueprint_route(
        blueprint_id: str,
        scope: str = "workspace",
        workspace_id: str = "",
    ) -> dict[str, Any]:
        if blueprint_id == DEFAULT_AGENT_BLUEPRINT_ID and scope == "global":
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="default registry agent blueprint cannot be deleted",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if scope not in {"global", "workspace"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="scope must be global or workspace",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id)
        try:
            return uninstall_agent_blueprint(
                blueprint_id=blueprint_id,
                scope=scope,  # type: ignore[arg-type]
                cwd=cwd or Path.cwd(),
            )
        except OSError as exc:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=str(exc),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

    # ---- /v1/expert-packs/* — thin aliases of the agent-blueprint lifecycle
    # (iowarp/clio-agent#663). A blueprint (structured workflow with a root
    # orchestrator) and a pack (loose collection of experts) share ONE
    # install/update/delete engine; the installed row's ``kind`` field
    # distinguishes them. These delegate to the blueprint route handlers so
    # there is exactly one implementation, one provenance model, one set of
    # structured error envelopes.
    @app.post("/v1/expert-packs/install", status_code=201)
    async def install_expert_pack_route(req: dict[str, Any]) -> dict[str, Any]:
        """Install an expert pack from a source URL/path/ref into workspace or
        global scope. Alias of ``POST /v1/agent-blueprints/install``; the
        returned rows carry ``kind`` (blueprint|pack)."""
        return await install_agent_blueprint_route(req)

    @app.post("/v1/expert-packs/{pack_id:path}/update")
    async def update_expert_pack_route(
        pack_id: str,
        req: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an installed expert pack from recorded source provenance.
        Alias of ``POST /v1/agent-blueprints/{id}/update``."""
        return await update_agent_blueprint_route(pack_id, req)

    @app.delete("/v1/expert-packs/{pack_id:path}")
    async def delete_expert_pack_route(
        pack_id: str,
        scope: str = "workspace",
        workspace_id: str = "",
    ) -> dict[str, Any]:
        """Delete an installed expert pack from workspace or global scope.
        Alias of ``DELETE /v1/agent-blueprints/{id}``."""
        return await delete_agent_blueprint_route(pack_id, scope, workspace_id)

    @app.post("/v1/agent-blueprints/{blueprint_id:path}/mcp/{descriptor_id}/enable")
    async def enable_agent_blueprint_mcp_descriptor(
        blueprint_id: str,
        descriptor_id: str,
        req: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = req or {}
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=str(body.get("workspace_id") or ""))
        blueprint = next(
            (row for row in discover_agent_blueprints(cwd=cwd) if row.id == blueprint_id),
            None,
        )
        if blueprint is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"agent blueprint not found: {blueprint_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        descriptors = load_mcp_descriptors(
            blueprint.root,
            scope=blueprint.scope,
            blueprint_id=blueprint.id,
        )
        descriptor = next((row for row in descriptors if row["id"] == descriptor_id), None)
        if descriptor is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"MCP descriptor not found: {descriptor_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if descriptor.get("validation_errors"):
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="MCP descriptor is invalid",
                        details={"validation_errors": descriptor.get("validation_errors", [])},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        sid = f"agent_blueprint_mcp_{blueprint_id}_{descriptor_id}"
        if not hasattr(app.state, "external_mcp_servers"):
            app.state.external_mcp_servers = {}
        spec: dict[str, Any] = {"transport": descriptor.get("transport")}
        if descriptor.get("command"):
            spec["command"] = descriptor["command"]
        if descriptor.get("args"):
            spec["args"] = list(descriptor.get("args") or [])
        if descriptor.get("url"):
            spec["url"] = descriptor["url"]
        declared_tools: list[dict[str, Any]] = [
            {
                **tool,
                "status": "enabled_pending_probe",
                "enabled": False,
                "server_id": sid,
                "descriptor_id": descriptor_id,
                "agent_blueprint_id": blueprint_id,
            }
            for tool in descriptor.get("tools") or []
            if isinstance(tool, Mapping)
        ]
        probe = bool(body.get("probe", True))
        status = "enabled_pending_probe"
        connect_error = ""
        tools = declared_tools
        if probe:
            try:
                from fastmcp import Client  # noqa: PLC0415

                from clio_agent.tools.mcp_config import (  # noqa: PLC0415
                    transport_from_spec,
                )

                # Probe the stored spec through the single canonical helper so the
                # install-probe accepts exactly what call/list/reconnect accept.
                transport = transport_from_spec(spec)
                async with Client(transport) as client:
                    live_tools = await client.list_tools()
                tools = []
                declared_by_name = {
                    str(tool.get("name") or tool.get("id") or ""): tool
                    for tool in declared_tools
                    if str(tool.get("name") or tool.get("id") or "")
                }
                for live_tool in live_tools:
                    tool_name = str(getattr(live_tool, "name", "") or "")
                    if not tool_name:
                        continue
                    declared = declared_by_name.get(tool_name, {})
                    tools.append(
                        {
                            **declared,
                            "id": tool_name,
                            "name": tool_name,
                            "description": getattr(live_tool, "description", "")
                            or declared.get("description")
                            or "",
                            "status": "ready",
                            "enabled": True,
                            "server_id": sid,
                            "descriptor_id": descriptor_id,
                            "agent_blueprint_id": blueprint_id,
                            "input_schema": getattr(live_tool, "input_schema", None)
                            or getattr(live_tool, "inputSchema", None)
                            or declared.get("input_schema")
                            or {},
                            "output_schema": getattr(live_tool, "output_schema", None)
                            or getattr(live_tool, "outputSchema", None)
                            or declared.get("output_schema")
                            or {},
                            "annotations": _normalize_mcp_tool_annotations(live_tool),
                        }
                    )
                declared_names = {
                    str(tool.get("name") or tool.get("id") or "")
                    for tool in declared_tools
                    if str(tool.get("name") or tool.get("id") or "")
                }
                missing = sorted(declared_names - {str(tool.get("name") or "") for tool in tools})
                for tool_name in missing:
                    declared = declared_by_name.get(tool_name, {})
                    tools.append(
                        {
                            **declared,
                            "id": tool_name,
                            "name": tool_name,
                            "status": "missing_after_probe",
                            "enabled": False,
                            "server_id": sid,
                            "descriptor_id": descriptor_id,
                            "agent_blueprint_id": blueprint_id,
                        }
                    )
                status = (
                    "ready" if tools and any(tool.get("enabled") for tool in tools) else "no_tools"
                )
            except Exception as exc:  # noqa: BLE001
                connect_error = repr(exc)
                status = "error"
        app.state.external_mcp_servers[sid] = {
            "id": sid,
            "name": descriptor.get("name") or descriptor_id,
            "status": status,
            "transport": descriptor.get("transport") or "unknown",
            "tools": tools,
            "spec": spec,
            "source": "agent_blueprint",
            "agent_blueprint_id": blueprint_id,
            "descriptor_id": descriptor_id,
        }
        if connect_error:
            app.state.external_mcp_servers[sid]["error"] = connect_error
        return {
            "id": sid,
            "name": descriptor.get("name") or descriptor_id,
            "status": status,
            "transport": descriptor.get("transport") or "unknown",
            "tools_count": len(tools),
            "tools": tools,
            "spec": spec,
            "source": "agent_blueprint",
            "agent_blueprint_id": blueprint_id,
            "descriptor_id": descriptor_id,
            **({"error": connect_error} if connect_error else {}),
        }

    @app.get("/v1/sessions/{sid}/agent-blueprint")
    async def get_session_agent_blueprint(sid: str) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        blueprint_id = deps.active_session_agent_blueprint_id(sid)
        blueprint_path = _runtime_active_agent_blueprint_path(app, sid)
        cwd = _runtime_workspace_catalog_cwd(app, session_id=sid)
        blueprint = next(
            (row for row in discover_agent_blueprints(cwd=cwd) if row.id == blueprint_id),
            None,
        )
        blueprint_wire: dict[str, Any] | None = (
            blueprint.to_wire() if blueprint is not None else None
        )
        if blueprint is None and blueprint_path is not None:
            validation = validate_agent_blueprint_path(blueprint_path, scope="session")
            raw_blueprint = validation.get("agent_blueprint")
            blueprint_wire = raw_blueprint if isinstance(raw_blueprint, dict) else None
        return {
            "session_id": sid,
            "workspace_id": getattr(sess, "workspace_id", ""),
            "active_agent_blueprint_id": blueprint_id,
            "active_agent_blueprint_path": str(blueprint_path)
            if blueprint_path is not None
            else "",
            "agent_blueprint": blueprint_wire,
            "agent_overlay": _runtime_session_agent_overlay(app, sid),
            "activation": {
                key: str(value)
                for key, value in (getattr(sess, "metadata", {}) or {}).items()
                if str(key).startswith("active_agent_blueprint_")
            },
        }

    @app.post("/v1/sessions/{sid}/agent-blueprint")
    async def set_session_agent_blueprint(sid: str, req: dict[str, Any]) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        blueprint_id = str(req.get("blueprint_id") or req.get("agent_blueprint_id") or "").strip()
        blueprint_path = str(req.get("path") or req.get("blueprint_path") or "").strip()
        cwd = _runtime_workspace_catalog_cwd(app, session_id=sid)
        if blueprint_path:
            validation = validate_agent_blueprint_path(Path(blueprint_path), scope="session")
            if not validation.get("enabled", False):
                raise HTTPException(
                    status_code=400,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="validation_error",
                            message="agent blueprint path is invalid",
                            details={
                                "path": blueprint_path,
                                "validation_errors": validation.get("validation_errors", []),
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            blueprint_wire = validation["agent_blueprint"]
            install_root = Path(str(blueprint_wire.get("root") or blueprint_path)).expanduser()
            activation_metadata = deps.agent_blueprint_activation_metadata(
                blueprint_wire=blueprint_wire,
                install_root=install_root,
                scope="session",
            )
            updated = app.state.sessions.update(
                sid,
                metadata_patch={
                    **activation_metadata,
                    "active_agent_blueprint_path": str(Path(blueprint_path).expanduser()),
                    "active_expert_pack_id": "",
                    "active_expert_pack_path": "",
                },
            )
        else:
            if not blueprint_id:
                raise HTTPException(
                    status_code=400,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="validation_error",
                            message="blueprint_id or path is required",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            blueprint = next(
                (row for row in discover_agent_blueprints(cwd=cwd) if row.id == blueprint_id), None
            )
            if blueprint is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"agent blueprint not found: {blueprint_id}",
                            details={"agent_blueprint_id": blueprint_id, "session_id": sid},
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )
            blueprint_wire = blueprint.to_wire()
            activation_metadata = deps.agent_blueprint_activation_metadata(
                blueprint_wire=blueprint_wire,
                install_root=blueprint.root,
                scope=blueprint.scope,
            )
            updated = app.state.sessions.update(
                sid,
                metadata_patch={
                    **activation_metadata,
                    "active_agent_blueprint_path": "",
                    "active_expert_pack_id": "",
                    "active_expert_pack_path": "",
                },
            )
        return {
            "session_id": sid,
            "workspace_id": getattr(sess, "workspace_id", ""),
            "active_agent_blueprint_id": str(blueprint_wire.get("id") or ""),
            "active_agent_blueprint_path": str(blueprint_path),
            "agent_blueprint": blueprint_wire,
            "session": Session(**updated.to_wire()).model_dump(exclude_none=True)
            if updated
            else None,
        }
