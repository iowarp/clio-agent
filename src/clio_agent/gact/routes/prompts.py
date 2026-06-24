"""Prompt registry routes for the GACT server (#714).

This concern owns the CLIO prompt-management vendor surface under ``/v1/prompts``
-- a browse/render/validate/save/reload API over the layered
:class:`~clio_agent.prompts.PromptRegistry` (builtin + global/workspace/session
overlay sources). These are a CLIO extension, not core GACT v0.2 routes; the TUI
uses them to inspect prompts, profiles, validation state and provenance without
knowing where prompt files live on disk:

* ``GET /v1/prompts`` -- list builtin + external prompt rows, their disk sources,
  and any session-agent prompt overlay.
* ``GET /v1/prompts/{prompt_id:path}`` -- resolve one prompt (optionally a named
  profile) to its effective definition.
* ``POST /v1/prompts/{prompt_id:path}/render`` -- render a prompt against the
  live template context (agent tree, active pack/blueprint, invocable commands)
  plus any caller-supplied context overrides.
* ``POST /v1/prompts/{prompt_id:path}/validate`` -- validate inline prompt text
  (or an on-disk row) for front-matter/template errors.
* ``POST /v1/prompts/reload`` -- re-scan the prompt source roots from disk.
* ``PUT /v1/prompts/{prompt_id:path}`` -- save prompt text to a writable scope
  (global/workspace/session).

The request-scoped registry/overlay/render-context builders couple to a deep web
of other ``build_app`` closures (session overlays, agent rows, command catalog),
so they stay built in ``build_app`` and travel here as the
``prompt_registry_for_request`` / ``prompt_agent_overlay_for_request`` /
``prompt_render_context_for_request`` seams on
:class:`~clio_agent.gact.routes.deps.GactDeps`. Handlers reach ``app.state``
directly and never import :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.prompts import parse_prompt_text

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_prompts_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the ``/v1/prompts`` registry routes on ``app``.

    Handlers are defined inside this factory so they close over the ``app``
    argument FastAPI's decorators require, and reach the request-scoped prompt
    registry/overlay/render-context builders through ``deps`` rather than any
    ``build_app`` local.
    """

    @app.get("/v1/prompts")
    async def list_prompts(
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """List built-in and external prompt definitions.

        This is a CLIO vendor surface rather than a core GACT v0.2 route. The
        TUI can use it later to browse prompts, profiles, validation state, and
        provenance without knowing where prompt files live on disk.
        """

        registry = deps.prompt_registry_for_request(
            session_id=session_id or "",
            workspace_id=workspace_id or "",
        )
        rows = registry.list()
        payload: dict[str, Any] = {
            "prompts": [asdict(row) for row in rows],
            "sources": [
                {"scope": source.scope, "root": str(source.root)} for source in registry.sources
            ],
        }
        overlay_prompt_sources = deps.prompt_agent_overlay_for_request(session_id or "")
        if overlay_prompt_sources:
            payload["agent_overlay"] = overlay_prompt_sources
        return payload

    @app.get("/v1/prompts/{prompt_id:path}")
    async def get_prompt(
        prompt_id: str,
        profile: str = "",
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        registry = deps.prompt_registry_for_request(
            session_id=session_id or "",
            workspace_id=workspace_id or "",
        )
        resolved = registry.resolve(prompt_id, profile=profile)
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"prompt not found: {prompt_id}",
                        details={"prompt_id": prompt_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"prompt": asdict(resolved)}

    @app.post("/v1/prompts/{prompt_id:path}/render")
    async def render_prompt(prompt_id: str, request: Request, profile: str = "") -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        requested_profile = str(body.get("profile") or profile or "")
        session_id = str(body.get("session_id") or "")
        workspace_id = str(body.get("workspace_id") or "")
        context_override = body.get("context")
        registry = deps.prompt_registry_for_request(
            session_id=session_id, workspace_id=workspace_id
        )
        context = deps.prompt_render_context_for_request(
            session_id=session_id,
            workspace_id=workspace_id,
        )
        if isinstance(context_override, Mapping):
            for key, value in context_override.items():
                context[str(key)] = str(value)
        rendered = registry.render(
            prompt_id,
            profile=requested_profile,
            context=context,
        )
        if rendered is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"prompt not found: {prompt_id}",
                        details={"prompt_id": prompt_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"prompt": asdict(rendered)}

    @app.post("/v1/prompts/{prompt_id:path}/validate")
    async def validate_prompt(prompt_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        text = str(body.get("text") or "")
        profile = str(body.get("profile") or "default")
        if text.strip():
            rendered = f"---\nid: {prompt_id}\nprofile: {profile}\n---\n{text}"
            parsed = parse_prompt_text(rendered, scope="validation", source_path="<request>")
            return {
                "enabled": parsed.enabled,
                "validation_errors": parsed.validation_errors,
                "prompt": asdict(parsed),
            }
        registry = deps.prompt_registry_for_request(
            session_id=str(body.get("session_id") or ""),
            workspace_id=str(body.get("workspace_id") or ""),
        )
        row = registry.get(prompt_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"prompt not found: {prompt_id}",
                        details={"prompt_id": prompt_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {
            "enabled": row.enabled,
            "validation_errors": row.validation_errors,
            "prompt": asdict(row),
        }

    @app.post("/v1/prompts/reload")
    async def reload_prompts(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        app.state.prompt_registry.reload()
        registry = deps.prompt_registry_for_request(
            session_id=str(body.get("session_id") or ""),
            workspace_id=str(body.get("workspace_id") or ""),
        )
        return {"reload": registry.reload()}

    @app.put("/v1/prompts/{prompt_id:path}")
    async def save_prompt(prompt_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        text = str(body.get("text") or "")
        if not text.strip():
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="missing required field: text",
                        details={"prompt_id": prompt_id, "field": "text"},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        session_id = str(body.get("session_id") or "")
        workspace_id = str(body.get("workspace_id") or "")
        scope = str(body.get("scope") or "global")
        try:
            registry = deps.prompt_registry_for_request(
                session_id=session_id,
                workspace_id=workspace_id,
                write_scope=scope,
            )
            row = registry.save(
                prompt_id,
                text=text,
                profile=str(body.get("profile") or "default"),
                title=str(body.get("title") or ""),
                description=str(body.get("description") or ""),
                provider=str(body.get("provider") or ""),
                model=str(body.get("model") or ""),
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=str(exc),
                        details={"prompt_id": prompt_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        return {"prompt": asdict(row)}
