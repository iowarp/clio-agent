"""Tier-2 agent registry + session agent-overlay routes for the GACT server (#714).

This concern owns two related surfaces:

* **Agent registry CRUD + list + extract** under ``/v1/agents`` -- the
  capability-routed catalog the TUI sidebar renders. ``GET /v1/agents`` (with an
  optional ``?tier=`` filter) merges the built-in tier-1/2 experts, the active
  Agent Blueprint graph, expert packs, and user/skill-registered agents;
  ``GET /v1/agents/{id}`` resolves one row; ``POST/PUT/DELETE /v1/agents`` manage
  *user* agents only (built-in ids are reserved and immutable). ``POST
  /v1/agents/extract`` harvests the most-common tool pattern from past session
  logs into a new user agent (the heuristic baseline; real SIMBA compilation is
  deferred).
* **Session agent overlays** under ``/v1/sessions/{sid}/agent-overlay`` --
  read/replace the per-session patch that overrides an active Agent Blueprint's
  expert fields (prompt/provider/model/tools/...) *for that session only*, plus
  ``.../export`` which materializes the overlaid hierarchy into a reusable Agent
  Blueprint on disk. The overlay validation is preserved exactly: it re-resolves
  the session's base blueprint rows, applies the patch, and reports per-field +
  per-hierarchy errors so an invalid overlay is rejected before it is stored.

The shared row-resolution machinery (``agent_rows``, ``agent_with_capability_refs``,
``base_session_agent_blueprint_rows``, ``apply_agent_overlay_rows``,
``prompt_registry_for_request``) has callers that remain in
:func:`clio_agent.gact.app.build_app`, so it stays single-sourced there and
travels here on :class:`~clio_agent.gact.routes.deps.GactDeps`. The
overlay-validation + blueprint-export helpers are private to this concern (no
other caller) and live in this module. Handlers reach ``app.state`` directly and
never import :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response

from clio_agent.gact.agent_blueprints import (
    validate_agent_blueprint_path,
    validate_agent_hierarchy,
)
from clio_agent.gact.agents.resolution import (
    _agent_overlay_patchable_fields,
    _merge_agent_def_rows,
    _runtime_session_agent_overlay,
    _runtime_workspace_catalog_cwd,
)
from clio_agent.gact.types import (
    AgentDef,
    ErrorEnvelope,
    ErrorInfo,
    ListAgentsResponse,
    Session,
)
from clio_agent.tools.catalog import TOOL_CATALOG

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

# Built-in expert ids reserved for CLIO's core experts: a user agent may not
# shadow them via create/update/delete/extract.
_RESERVED_AGENT_IDS = frozenset({"main", "data", "analysis", "visualization"})


def _known_agent_overlay_tool_names(app: FastAPI) -> set[str]:
    """Tool names an overlay may legitimately assign to an expert.

    Combines the static tool catalog, the three agent-callable memory tools, the
    live DSPy tool executor's tools, and any enabled external-MCP server tools.
    """

    names = set(TOOL_CATALOG)
    names.update(
        {
            "memory_search_sessions",
            "memory_read_session_summary",
            "memory_read_context_frame",
        }
    )
    tool_executor = getattr(getattr(app.state, "agent", None), "tool_executor", None)
    if tool_executor is not None and hasattr(tool_executor, "to_dspy_tools"):
        try:
            for tool in tool_executor.to_dspy_tools():
                name = str(getattr(tool, "name", "") or "").strip()
                if name:
                    names.add(name)
        except Exception:  # noqa: BLE001 - a broken executor must not block validation
            pass
    for server in (getattr(app.state, "external_mcp_servers", {}) or {}).values():
        if not isinstance(server, Mapping):
            continue
        for tool in server.get("tools") or []:
            if isinstance(tool, Mapping):
                name = str(tool.get("name") or tool.get("id") or "").strip()
            else:
                name = str(getattr(tool, "name", "") or getattr(tool, "id", "") or "").strip()
            if name:
                names.add(name)
    return names


def _known_agent_overlay_provider_ids() -> set[str]:
    """Provider ids an overlay may legitimately assign as ``default_provider``."""

    from clio_agent.providers.registry import as_lm_presets  # noqa: PLC0415

    presets = as_lm_presets()
    providers = {str(row.id) for row in presets if str(row.id).strip()}
    providers.update({str(row.provider) for row in presets if str(row.provider).strip()})
    return providers


def _overlay_validation_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    agent_id: str = "",
    field: str = "",
) -> None:
    """Append a structured overlay-validation error row in place."""

    row: dict[str, Any] = {"code": code, "message": message}
    if agent_id:
        row["agent_id"] = agent_id
    if field:
        row["field"] = field
    errors.append(row)


def _validate_session_agent_overlay_payload(
    app: FastAPI,
    deps: "GactDeps",
    overlay: Mapping[str, Any],
    *,
    session_id: str,
    workspace_id: str = "",
) -> dict[str, Any]:
    """Validate a session agent overlay against the session's active blueprint.

    Re-resolves the session's base blueprint rows, then for each agent patch
    checks: the agent id exists in the blueprint, every field is editable, tools
    resolve, the referenced prompt/provider exist, and field types are correct.
    Finally it applies the overlay and validates the resulting hierarchy. Returns
    the ``enabled`` flag plus the collected errors/diagnostics -- behavior is
    byte-identical to the original ``build_app`` closure.
    """

    errors: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    base_rows = deps.base_session_agent_blueprint_rows(
        session_id=session_id, workspace_id=workspace_id
    )
    base_ids = {row.id for row in base_rows}
    agents = overlay.get("agents")
    if not base_rows:
        _overlay_validation_error(
            errors,
            code="missing_active_agent_blueprint",
            message="session has no active Agent Blueprint to overlay",
        )
    if agents is None:
        agents = {}
    if not isinstance(agents, Mapping):
        _overlay_validation_error(
            errors,
            code="invalid_agents",
            message="agent overlay 'agents' must be an object",
            field="agents",
        )
        agents = {}
    patchable = _agent_overlay_patchable_fields()
    known_tools = _known_agent_overlay_tool_names(app)
    known_providers = _known_agent_overlay_provider_ids()
    prompt_registry = deps.prompt_registry_for_request(
        session_id=session_id,
        workspace_id=workspace_id,
    )
    for raw_agent_id, raw_patch in agents.items():
        agent_id = str(raw_agent_id)
        if agent_id not in base_ids:
            _overlay_validation_error(
                errors,
                code="unknown_agent_id",
                message=f"overlay references unknown expert: {agent_id}",
                agent_id=agent_id,
                field="agents",
            )
            continue
        if not isinstance(raw_patch, Mapping):
            _overlay_validation_error(
                errors,
                code="invalid_agent_patch",
                message=f"overlay patch for {agent_id} must be an object",
                agent_id=agent_id,
                field="agents",
            )
            continue
        unknown_fields = sorted(str(key) for key in raw_patch if str(key) not in patchable)
        for field_name in unknown_fields:
            _overlay_validation_error(
                errors,
                code="unknown_field",
                message=f"overlay field is not editable: {field_name}",
                agent_id=agent_id,
                field=field_name,
            )
        if "tools" in raw_patch:
            raw_tools = raw_patch.get("tools")
            if not isinstance(raw_tools, list) or any(
                not isinstance(item, str) for item in raw_tools
            ):
                _overlay_validation_error(
                    errors,
                    code="invalid_tools",
                    message="tools must be a list of tool ids",
                    agent_id=agent_id,
                    field="tools",
                )
            else:
                for tool_name in raw_tools:
                    if tool_name not in known_tools:
                        _overlay_validation_error(
                            errors,
                            code="unknown_tool",
                            message=f"unknown tool: {tool_name}",
                            agent_id=agent_id,
                            field="tools",
                        )
        if "prompt_id" in raw_patch or "prompt_profile" in raw_patch:
            prompt_id = str(raw_patch.get("prompt_id") or "").strip()
            if prompt_id and prompt_registry.resolve(prompt_id) is None:
                _overlay_validation_error(
                    errors,
                    code="unknown_prompt",
                    message=f"prompt not found: {prompt_id}",
                    agent_id=agent_id,
                    field="prompt_id",
                )
        if "default_provider" in raw_patch:
            provider_id = str(raw_patch.get("default_provider") or "").strip()
            if provider_id and provider_id not in known_providers:
                _overlay_validation_error(
                    errors,
                    code="unknown_provider",
                    message=f"provider not found: {provider_id}",
                    agent_id=agent_id,
                    field="default_provider",
                )
        for string_field in (
            "title",
            "description",
            "system_prompt",
            "prompt_id",
            "prompt_profile",
            "default_provider",
            "default_model",
            "parent_id",
            "specialization",
        ):
            if (
                string_field in raw_patch
                and raw_patch.get(string_field) is not None
                and not isinstance(raw_patch.get(string_field), str)
            ):
                _overlay_validation_error(
                    errors,
                    code="invalid_field_type",
                    message=f"{string_field} must be a string",
                    agent_id=agent_id,
                    field=string_field,
                )
        for list_field in ("keywords", "skills", "commands"):
            if list_field in raw_patch:
                value = raw_patch.get(list_field)
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    _overlay_validation_error(
                        errors,
                        code="invalid_field_type",
                        message=f"{list_field} must be a list of strings",
                        agent_id=agent_id,
                        field=list_field,
                    )
        if "tier" in raw_patch and not isinstance(raw_patch.get("tier"), int):
            _overlay_validation_error(
                errors,
                code="invalid_field_type",
                message="tier must be an integer",
                agent_id=agent_id,
                field="tier",
            )
        if "enabled" in raw_patch and not isinstance(raw_patch.get("enabled"), bool):
            _overlay_validation_error(
                errors,
                code="invalid_field_type",
                message="enabled must be a boolean",
                agent_id=agent_id,
                field="enabled",
            )
        if "parameters" in raw_patch and not isinstance(raw_patch.get("parameters"), Mapping):
            _overlay_validation_error(
                errors,
                code="invalid_field_type",
                message="parameters must be an object",
                agent_id=agent_id,
                field="parameters",
            )
    applied_rows = deps.apply_agent_overlay_rows(base_rows, overlay, session_id=session_id)
    validated_rows = validate_agent_hierarchy(_merge_agent_def_rows(applied_rows))
    for row in validated_rows:
        for error in row.validation_errors:
            _overlay_validation_error(
                errors,
                code="invalid_hierarchy",
                message=error,
                agent_id=row.id,
            )
    if agents:
        diagnostics.append(
            {
                "code": "overlay_scope",
                "message": "overlay applies only to this session until explicitly exported",
                "session_id": session_id,
            }
        )
    return {
        "enabled": not errors,
        "validation_errors": errors,
        "diagnostics": diagnostics,
        "agent_count": len(base_rows),
        "overlay_agent_count": len(agents),
    }


def _agent_blueprint_export_root(
    app: FastAPI,
    *,
    scope: str,
    session_id: str,
    workspace_id: str = "",
) -> Path:
    """Resolve the on-disk root a session-overlay export writes the blueprint to."""

    if scope == "workspace":
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id, session_id=session_id)
        return (cwd or Path.cwd()) / ".clio" / "agent-blueprints"
    if scope == "global":
        from clio_agent import paths  # noqa: PLC0415

        return paths.user_config_dir() / "agent-blueprints"
    raise ValueError("scope must be workspace or global")


def _frontmatter_scalar(value: Any) -> str:
    """Render a value as a JSON-quoted YAML frontmatter scalar."""

    return json.dumps(str(value or ""))


def _frontmatter_list_lines(key: str, values: list[str]) -> list[str]:
    """Render a YAML frontmatter list block (empty when there are no values)."""

    if not values:
        return []
    lines = [f"{key}:"]
    lines.extend(f"  - {_frontmatter_scalar(value)}" for value in values)
    return lines


def _agent_blueprint_expert_markdown(row: AgentDef) -> str:
    """Render one expert ``AgentDef`` as an Agent Blueprint expert markdown file."""

    lines = [
        "---",
        f"id: {_frontmatter_scalar(row.id)}",
        f"title: {_frontmatter_scalar(row.title or row.id)}",
    ]
    for key, value in (
        ("description", row.description),
        ("parent_id", row.parent_id),
        ("prompt_id", row.prompt_id),
        ("prompt_profile", row.prompt_profile),
        ("provider", row.default_provider),
        ("model", row.default_model),
        ("specialization", row.specialization),
    ):
        if value:
            lines.append(f"{key}: {_frontmatter_scalar(value)}")
    lines.append(f"tier: {int(row.tier or 1)}")
    if row.enabled is False:
        lines.append("enabled: false")
    lines.extend(_frontmatter_list_lines("tools", list(row.tools or [])))
    lines.extend(_frontmatter_list_lines("skills", list(row.skills or [])))
    lines.extend(_frontmatter_list_lines("commands", list(row.commands or [])))
    lines.extend(_frontmatter_list_lines("keywords", list(row.keywords or [])))
    for key, value in sorted((row.parameters or {}).items()):
        lines.append(f"param_{key}: {_frontmatter_scalar(value)}")
    lines.extend(["---", row.system_prompt or row.description or row.title or row.id, ""])
    return "\n".join(lines)


def _write_exported_agent_blueprint(
    *,
    root: Path,
    blueprint_id: str,
    title: str,
    session_id: str,
    rows: list[AgentDef],
) -> None:
    """Materialize an overlaid expert hierarchy as an Agent Blueprint on disk."""

    root.mkdir(parents=True, exist_ok=True)
    experts_root = root / "experts"
    experts_root.mkdir(parents=True, exist_ok=True)
    roots = [row for row in rows if not row.parent_id]
    root_expert = roots[0].id if roots else (rows[0].id if rows else "")
    manifest = [
        "---",
        f"id: {_frontmatter_scalar(blueprint_id)}",
        'version: "0.1.0"',
        f"title: {_frontmatter_scalar(title or blueprint_id)}",
        f"root_expert: {_frontmatter_scalar(root_expert)}",
        "---",
        f"Exported from session overlay {session_id}.",
        "",
    ]
    (root / "AGENT.md").write_text("\n".join(manifest), encoding="utf-8")
    for row in rows:
        (experts_root / f"{row.id}.md").write_text(
            _agent_blueprint_expert_markdown(row),
            encoding="utf-8",
        )


def register_agents_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the Tier-2 agent registry + session agent-overlay routes on ``app``.

    Handlers are defined inside this factory so they close over the ``app``
    argument FastAPI's decorators require, and reach the shared row-resolution
    closures (``agent_rows``/``agent_with_capability_refs``/overlay helpers/
    ``prompt_registry_for_request``) plus the destructive-action guard and
    workspace-session mirror through ``deps`` rather than any ``build_app`` local.
    """

    @app.post("/v1/agents/extract", response_model=AgentDef, status_code=201)
    async def extract_agent(request: Request) -> AgentDef:
        """Extract a new dynamic agent from past sessions.

        Body: ``{session_ids: [..], agent_id: ".."}``. Walks the
        message logs of the listed sessions, harvests the most-
        common tool names called, and registers a user agent
        whose tools list reflects that pattern. Real DSPy SIMBA
        compilation is deferred — this is the heuristic baseline.
        """

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is treated as empty
            body = {}
        if not isinstance(body, dict):
            body = {}
        sids = [s for s in (body.get("session_ids") or []) if isinstance(s, str)]
        new_id = (body.get("agent_id") or "").strip()
        if not sids or not new_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="required: session_ids[] + agent_id",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if new_id in _RESERVED_AGENT_IDS:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(f"agent id {new_id!r} is built-in; pick a different one"),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # Walk the message logs.
        tool_counts: Counter[str] = Counter()
        sample_questions: list[str] = []
        for sid in sids:
            for m in app.state.messages.get(sid, []):
                if m.role == "user":
                    text = next(
                        (p.text for p in m.parts if p.type == "text" and p.text),
                        "",
                    )
                    if text:
                        sample_questions.append(text)
                if m.role == "assistant":
                    md = m.metadata or {}
                    for call in md.get("tools_called", []) or []:
                        name = (
                            call.get("name")
                            if isinstance(call, dict)
                            else getattr(call, "name", "")
                        )
                        if name:
                            tool_counts[name] += 1
        top_tools = [t for t, _ in tool_counts.most_common(5)]
        keywords = sorted(
            {w.strip(".,").lower() for q in sample_questions[:5] for w in q.split() if len(w) >= 4}
        )[:8]
        payload = {
            "id": new_id,
            "title": f"Extracted from {len(sids)} session(s)",
            "description": (
                f"Auto-extracted agent from {len(sids)} session log(s). "
                f"Common tools: {', '.join(top_tools) if top_tools else '(none)'}"
            ),
            "tier": 2,
            "specialization": "extracted",
            "keywords": keywords,
            "tools": top_tools,
        }
        agent = app.state.user_agents.upsert(payload)
        return AgentDef(**agent.to_wire())

    @app.get("/v1/sessions/{sid}/agent-overlay")
    async def get_session_agent_overlay(sid: str) -> dict[str, Any]:
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
        overlay = _runtime_session_agent_overlay(app, sid)
        validation = _validate_session_agent_overlay_payload(
            app,
            deps,
            overlay,
            session_id=sid,
            workspace_id=getattr(sess, "workspace_id", ""),
        )
        return {"session_id": sid, "agent_overlay": overlay, "validation": validation}

    @app.put("/v1/sessions/{sid}/agent-overlay")
    async def put_session_agent_overlay(sid: str, req: dict[str, Any]) -> dict[str, Any]:
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
        overlay = req.get("agent_overlay", req)
        if not isinstance(overlay, Mapping):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="agent overlay must be an object",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        validation = _validate_session_agent_overlay_payload(
            app,
            deps,
            overlay,
            session_id=sid,
            workspace_id=getattr(sess, "workspace_id", ""),
        )
        if not validation["enabled"]:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="agent overlay is invalid",
                        details={"validation": validation},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        updated = app.state.sessions.update(
            sid,
            metadata_patch={"agent_blueprint_overlay": dict(overlay)},
        )
        deps.mirror_workspace_session(app, sid)
        return {
            "session_id": sid,
            "agent_overlay": dict(overlay),
            "validation": validation,
            "session": Session(**updated.to_wire()).model_dump(exclude_none=True)
            if updated
            else None,
        }

    @app.post("/v1/sessions/{sid}/agent-overlay/export", status_code=201)
    async def export_session_agent_overlay(
        sid: str, req: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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
        body = req or {}
        blueprint_id = str(body.get("blueprint_id") or body.get("agent_blueprint_id") or "").strip()
        if not blueprint_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", blueprint_id):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="blueprint_id must use letters, numbers, dots, underscores, and hyphens",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        scope = str(body.get("scope") or "workspace").strip()
        if scope not in {"workspace", "global"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="scope must be workspace or global",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        workspace_id = str(body.get("workspace_id") or getattr(sess, "workspace_id", "") or "")
        overlay = _runtime_session_agent_overlay(app, sid)
        validation = _validate_session_agent_overlay_payload(
            app,
            deps,
            overlay,
            session_id=sid,
            workspace_id=workspace_id,
        )
        if not validation["enabled"]:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="agent overlay is invalid",
                        details={"validation": validation},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        base_rows = deps.base_session_agent_blueprint_rows(
            session_id=sid, workspace_id=workspace_id
        )
        if not base_rows:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="session has no active Agent Blueprint to export",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = validate_agent_hierarchy(
            _merge_agent_def_rows(deps.apply_agent_overlay_rows(base_rows, overlay, session_id=sid))
        )
        if any(row.validation_errors for row in rows):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="effective overlay hierarchy is invalid",
                        details={
                            "validation_errors": [
                                f"{row.id}: {error}"
                                for row in rows
                                for error in row.validation_errors
                            ]
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        export_root = (
            _agent_blueprint_export_root(
                app,
                scope=scope,
                session_id=sid,
                workspace_id=workspace_id,
            )
            / blueprint_id
        )
        if export_root.exists() and not bool(body.get("overwrite", False)):
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="conflict",
                        message=f"agent blueprint already exists: {blueprint_id}",
                        details={"path": str(export_root)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if export_root.exists():
            shutil.rmtree(export_root)
        _write_exported_agent_blueprint(
            root=export_root,
            blueprint_id=blueprint_id,
            title=str(body.get("title") or blueprint_id),
            session_id=sid,
            rows=rows,
        )
        validation_after = validate_agent_blueprint_path(export_root, scope=scope)
        if not validation_after.get("enabled", False):
            raise HTTPException(
                status_code=500,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="exported Agent Blueprint did not validate",
                        details={"validation": validation_after},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return {
            "session_id": sid,
            "workspace_id": workspace_id,
            "scope": scope,
            "source_session_id": sid,
            "agent_blueprint": validation_after.get("agent_blueprint"),
            "agents": validation_after.get("agents", []),
            "path": str(export_root),
            "validation": validation_after,
        }

    @app.get("/v1/agents", response_model=ListAgentsResponse)
    async def list_agents(
        tier: Optional[int] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> ListAgentsResponse:
        """SPEC §6.5 + v0.2 §4.3.1: optional ?tier=N filter.

        Combines built-in tier-1/2 experts with any user-registered
        agents (iowarp/clio-agent#19). Built-ins always come first
        so the TUI's sidebar groups consistently.
        """

        rows = deps.agent_rows(session_id=session_id or "", workspace_id=workspace_id or "")
        if tier is not None:
            rows = [a for a in rows if a.tier == tier]
        return ListAgentsResponse(agents=rows)

    @app.get("/v1/agents/{agent_id}", response_model=AgentDef)
    async def get_agent(
        agent_id: str,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> AgentDef:
        """SPEC §6.5 detail endpoint for built-in/user/skill agents."""

        for row in deps.agent_rows(session_id=session_id or "", workspace_id=workspace_id or ""):
            if row.id == agent_id:
                return row
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"agent not found: {agent_id}",
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.post("/v1/agents", response_model=AgentDef, status_code=201)
    async def create_agent(req: dict[str, Any]) -> AgentDef:
        """iowarp/clio-agent#19: register a new dynamic agent.

        The agent is stored as an AgentDef row + persisted to disk;
        future GET /v1/agents calls include it. Built-in id collision
        is rejected so users can't shadow CLIO's core experts.
        Source is forced to "user" regardless of what the client sent.
        """

        agent_id = req.get("id", "")
        if agent_id in _RESERVED_AGENT_IDS:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {agent_id!r} is reserved for a "
                            "built-in expert; pick a different id"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if not agent_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: id",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # Force user-source so a malicious client can't claim builtin.
        req = dict(req)
        req["source"] = "user"
        agent = app.state.user_agents.upsert(req)
        return deps.agent_with_capability_refs(AgentDef(**agent.to_wire()))

    @app.put("/v1/agents/{agent_id}", response_model=AgentDef)
    async def update_agent(agent_id: str, req: dict[str, Any]) -> AgentDef:
        """Replace an existing user agent. Built-ins are immutable."""

        if agent_id in _RESERVED_AGENT_IDS:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {agent_id!r} is a built-in; "
                            "rebuild CLIO to change its definition"
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.user_agents.get(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"agent not found: {agent_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Force the URL id to win over the body to avoid the user
        # silently renaming via PUT. Force user source.
        body = dict(req)
        body["id"] = agent_id
        body["source"] = "user"
        agent = app.state.user_agents.upsert(body)
        return deps.agent_with_capability_refs(AgentDef(**agent.to_wire()))

    @app.delete("/v1/agents/{agent_id}")
    async def delete_agent(agent_id: str) -> Response:
        """Drop a user-registered agent. Built-ins are immutable."""

        if agent_id in _RESERVED_AGENT_IDS:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(f"agent id {agent_id!r} is a built-in and cannot be removed"),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.user_agents.get(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"agent not found: {agent_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        deps.guard_direct_destructive_action(
            app,
            tool_name="gact.agent.delete",
            args={"agent_id": agent_id},
            summary=f"delete agent {agent_id}",
            reason="user_requested_agent_delete",
        )
        app.state.user_agents.delete(agent_id)
        return Response(status_code=204)
