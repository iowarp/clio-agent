"""Authentication, catalog and handshake routes for individual LM providers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.provider_catalog_snapshot import invalidate_provider
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.routes.provider_auth import handle_auth_action
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderPreset
from clio_agent.providers.dependencies import (
    ProviderDependencyInstallError,
    ensure_claude_code_support,
)

Readiness = Callable[..., tuple[str, str, bool, str]]


def register_provider_catalog_routes(
    app: FastAPI,
    presets: list[LMProviderPreset],
    provider_models: dict[str, list[dict[str, str]]],
    chatgpt_readiness: Readiness,
    claude_code_readiness: Readiness,
) -> None:
    """Register provider auth, model listing, support install and checks."""

    _LM_PRESETS = presets
    _PROVIDER_MODELS = provider_models
    _chatgpt_readiness = chatgpt_readiness
    _claude_code_readiness = claude_code_readiness

    @app.post("/v1/providers/{provider_id}/auth")
    async def auth_provider(provider_id: str, request: Request) -> dict[str, Any]:
        """The generic provider sign-in API (start/complete/status/logout).

        Dispatched by provider kind in :mod:`clio_agent.gact.routes.provider_auth`
        -- ALCF (Globus OAuth) and the direct ChatGPT provider both go through
        this one interface. Any other provider (cloud / local, api_key / no
        auth) gets a 405 with a hint pointing to PUT /v1/providers/lm.
        """

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        body = await json_body(request, route="POST /v1/providers/{provider_id}/auth")
        action = str(body.get("action", "start")).strip().lower()
        return await handle_auth_action(preset=preset, action=action, body=body, app=app, presets=_LM_PRESETS)

    @app.get("/v1/providers/{provider_id}/models")
    async def list_provider_models(provider_id: str, api_base: str = "") -> dict[str, Any]:
        """Read the startup catalog; explicit provider checks own live verification."""
        from clio_agent.providers import model_discovery  # noqa: PLC0415
        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )

        # provider_id names a preset's own id, never its wire KIND (#1418):
        # nine presets share kind "openai", so a kind-based fallback here
        # silently returned the FIRST such preset (by catalog order) rather
        # than the one actually configured.
        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            # Last-ditch static for known provider ids only.
            models = _PROVIDER_MODELS.get(provider_id)
            if models is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"unknown provider: {provider_id}",
                            details={"available": sorted(_PROVIDER_MODELS)},
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )
            return {"models": models, "source": "static_catalog"}

        if preset.provider in {"chatgpt", "claude_code"}:
            _, message, verified, _ = (
                _chatgpt_readiness() if preset.provider == "chatgpt" else _claude_code_readiness()
            )
            try:
                overlay = model_discovery.overlay_models_wire(preset.id, preset.provider)
            except model_discovery.OverlayMalformedError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="overlay_malformed", message=str(exc), recoverable=True
                        )
                    ).model_dump(exclude_none=True),
                ) from exc
            if preset.provider == "claude_code":
                from clio_agent.providers.model_discovery.claude_code_catalog import (  # noqa: PLC0415
                    cached_claude_code_candidates,
                )

                candidates, catalog_error = cached_claude_code_candidates()
                if candidates is None or catalog_error:
                    return {
                        "models": [],
                        "source": "unavailable",
                        "error": catalog_error or "Claude Code model catalog has not loaded yet",
                    }
                verified_models = (
                    {row["id"]: row for row in (overlay or {}).get("models", [])}
                    if verified
                    else {}
                )
                models = [
                    {
                        **verified_models.get(row["id"], row),
                        "availability": (
                            "available" if row["id"] in verified_models else "candidate"
                        ),
                    }
                    for row in candidates
                ]
                default_model = str((overlay or {}).get("default_model") or "")
                if default_model not in verified_models or default_model not in {
                    row["id"] for row in candidates
                }:
                    default_model = ""
                return {
                    "models": models,
                    "source": "github_catalog",
                    "default_model": default_model,
                    **({"error": message} if not verified else {}),
                }
            if verified and overlay is not None:
                return overlay
            return {"models": [], "source": "unavailable", "error": message}

        ctx = HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=(api_base or preset.api_base or ""),
            api_key=model_discovery.resolve_cloud_api_key(preset.provider),
            auth_mode="passive",
            allow_external_sources=True,
        )
        report = await run_handshake(ctx)
        wire = report.to_models_wire()
        return wire

    @app.post("/v1/providers/{provider_id}/install")
    async def install_provider_support(provider_id: str) -> dict[str, Any]:
        """Install optional runtime support for a provider on the connected agent."""

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"unknown provider: {provider_id}")
        if preset.provider != "claude_code":
            raise HTTPException(
                status_code=405,
                detail=f"provider '{provider_id}' has no installable runtime support",
            )
        from clio_agent.providers.claude_code_errors import (  # noqa: PLC0415
            CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
        )

        try:
            installed = await asyncio.to_thread(ensure_claude_code_support)
        except ProviderDependencyInstallError as exc:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="claude_code_install_failed",
                        message=CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
                        details={"diagnostic": str(exc)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        return {
            "provider_id": preset.id,
            "installed": installed,
            "instructions": "Claude Code support is installed. Check the provider to verify sign-in.",
        }

    @app.get("/v1/providers/{provider_id}/handshake")
    async def provider_handshake(
        provider_id: str, api_base: str = "", refresh: bool = False
    ) -> dict[str, Any]:
        """Async provider handshake: connectivity + auth + per-model config.

        Report-only (no runtime mutation). Runs the per-provider handshake and
        returns the discovered context windows, reasoning/tool capabilities and
        provenance alongside the legacy model list (``to_models_wire`` shape).
        Cached for the handshake TTL; ``refresh=true`` forces a re-probe. Argonne
        resolves its own stored token (passive, never interactive).
        """
        from clio_agent.providers import model_discovery  # noqa: PLC0415
        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )

        # provider_id names a preset's own id, never its wire KIND (#1418) --
        # see the matching comment on list_provider_models above.
        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if refresh:
            # An explicit check is new evidence: the catalog snapshot must not keep
            # serving what the provider looked like before it.
            invalidate_provider(app, preset.id)
        if preset.provider == "chatgpt":
            status, message, verified, _ = _chatgpt_readiness()
            if refresh and status in {"auth_check_required", "ready"}:
                from clio_agent.providers.catalog import get_provider  # noqa: PLC0415
                from clio_agent.providers.chatgpt.errors import (  # noqa: PLC0415
                    CHATGPT_AUTHENTICATION_ERROR_MESSAGE,
                    contains_chatgpt_authentication_error,
                )

                provider = get_provider(preset.id)
                results = (
                    await model_discovery.refresh_all(presets=[provider])
                    if provider is not None
                    else []
                )
                result = results[0] if results else {}
                failure = str(result.get("failed_reason") or "")
                if failure:
                    auth_failure = contains_chatgpt_authentication_error(failure)
                    return {
                        "models": [],
                        "source": "unavailable",
                        "error": (CHATGPT_AUTHENTICATION_ERROR_MESSAGE if auth_failure else failure),
                        "connectivity": "ok" if auth_failure else "unreachable",
                        "auth": "rejected" if auth_failure else "deferred",
                        "latency_ms": None,
                        "generated_at": str(result.get("generated_at") or ""),
                    }
                status, message, verified, _ = _chatgpt_readiness(ignore_startup=True)
            if not verified:
                return {
                    "models": [],
                    "source": "unavailable",
                    "error": message,
                    "connectivity": "skipped",
                    "auth": "missing" if status == "auth_required" else "deferred",
                    "latency_ms": None,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
        if preset.provider == "claude_code":
            status, message, verified, _ = _claude_code_readiness()
            if refresh:
                from clio_agent.providers.catalog import get_provider  # noqa: PLC0415
                from clio_agent.providers.claude_code_errors import (  # noqa: PLC0415
                    CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
                    contains_claude_code_dependency_error,
                )

                provider = get_provider(preset.id)
                results = (
                    await model_discovery.refresh_all(presets=[provider])
                    if provider is not None
                    else []
                )
                result = results[0] if results else {}
                failure = str(result.get("failed_reason") or "")
                if failure:
                    return {
                        "models": [],
                        "source": "unavailable",
                        "error": (
                            CLAUDE_CODE_INSTALL_FAILED_MESSAGE
                            if contains_claude_code_dependency_error(failure)
                            else failure
                        ),
                        "connectivity": "unreachable",
                        "auth": "deferred",
                        "latency_ms": None,
                        "generated_at": str(result.get("generated_at") or ""),
                    }
                status, message, verified, _ = _claude_code_readiness(ignore_startup=True)
            if not verified:
                return {
                    "models": [],
                    "source": "unavailable",
                    "error": message,
                    "connectivity": "unreachable" if status == "install_required" else "skipped",
                    "auth": "missing" if status == "install_required" else "deferred",
                    "latency_ms": None,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
        ctx = HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=(api_base or preset.api_base or ""),
            api_key=model_discovery.resolve_cloud_api_key(preset.provider),
            auth_mode="passive",
            allow_external_sources=True,
        )
        report = await run_handshake(ctx, force=refresh)
        out = report.to_models_wire()
        out["connectivity"] = report.connectivity.value
        out["auth"] = report.auth.value
        out["latency_ms"] = report.latency_ms
        out["generated_at"] = report.generated_at
        return out
