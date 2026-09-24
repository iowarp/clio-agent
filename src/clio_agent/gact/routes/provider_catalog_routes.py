"""Authentication, catalog and handshake routes for individual LM providers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.provider_catalog_snapshot import invalidate_provider
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderPreset
from clio_agent.providers.dependencies import (
    ProviderDependencyInstallError,
    ensure_argonne_support,
    ensure_claude_code_support,
)

Readiness = Callable[..., tuple[str, str, bool, str]]


def register_provider_catalog_routes(
    app: FastAPI,
    presets: list[LMProviderPreset],
    provider_models: dict[str, list[dict[str, str]]],
    codex_readiness: Readiness,
    claude_code_readiness: Readiness,
) -> None:
    """Register provider auth, model listing, support install and checks."""

    _LM_PRESETS = presets
    _PROVIDER_MODELS = provider_models
    _codex_readiness = codex_readiness
    _claude_code_readiness = claude_code_readiness

    @app.post("/v1/providers/{provider_id}/auth")
    async def auth_provider(provider_id: str, request: Request) -> dict[str, Any]:
        """SPEC §6.12 — kick off provider-specific auth.

        For argonne_*, ``action=start`` returns the Globus login URL and an
        opaque flow id. ``action=complete`` exchanges the one-time code on the
        connected agent, where the refresh token must live. This works for both
        local and remote agents without trying to open a terminal on that host.

        Other providers (cloud / local) use api_key / no-auth and
        return 405 with a hint pointing to PUT /v1/providers/lm.
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

        if preset.provider != "argonne":
            raise HTTPException(
                status_code=405,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="unsupported",
                        message=(
                            f"provider '{provider_id}' uses "
                            f"{'api_key' if preset.requires_api_key else 'no'} "
                            "auth; pass api_key directly to PUT /v1/providers/lm."
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        try:
            installed_support = await asyncio.to_thread(ensure_argonne_support)
        except ProviderDependencyInstallError as exc:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_install_failed",
                        message=(
                            "CLIO could not install ALCF sign-in support on the connected agent: "
                            f"{exc}"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        body = await json_body(request, route="POST /v1/providers/{provider_id}/auth")
        action = str(body.get("action", "start")).strip().lower()
        try:
            from clio_agent.providers import argonne_auth  # noqa: PLC0415

            if action == "complete":
                flow_id = str(body.get("flow_id", ""))
                authorization_code = str(body.get("authorization_code", ""))
                await asyncio.to_thread(
                    argonne_auth.complete_authentication,
                    flow_id,
                    authorization_code,
                )
                # The Globus tokens are shared by every ALCF cluster, so every
                # argonne provider's catalog evidence was produced under the old
                # (signed-out) credential; the next catalog read re-probes them.
                for argonne_preset in (p for p in _LM_PRESETS if p.provider == "argonne"):
                    invalidate_provider(app, argonne_preset.id)
                return {
                    "is_authenticated": True,
                    "provider_id": provider_id,
                    "instructions": "ALCF sign-in complete. Checking available models.",
                }
            if action != "start":
                raise ValueError(f"unknown authentication action: {action}")

            pending = await asyncio.to_thread(argonne_auth.begin_authentication)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="argonne_auth_failed",
                        message=f"Could not complete Globus authentication: {exc}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        return {
            "is_authenticated": False,
            "provider_id": provider_id,
            "instructions": (
                ("Installed ALCF sign-in support on this agent. " if installed_support else "")
                + f"Continue in {preset.auth_label or 'Globus'}, then paste the authorization "
                "code here."
            ),
            "authorization_url": pending.authorization_url,
            "flow_id": pending.flow_id,
        }

    @app.get("/v1/providers/{provider_id}/models")
    async def list_provider_models(provider_id: str, api_base: str = "") -> dict[str, Any]:
        """Read the startup catalog; explicit provider checks own live verification."""
        from clio_agent.providers import model_discovery  # noqa: PLC0415
        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            preset = next((p for p in _LM_PRESETS if p.provider == provider_id), None)
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

        if preset.provider in {"codex", "claude_code"}:
            _, message, verified, _ = (
                _codex_readiness() if preset.provider == "codex" else _claude_code_readiness()
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

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            preset = next((p for p in _LM_PRESETS if p.provider == provider_id), None)
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
        if preset.provider == "codex":
            status, message, verified, _ = _codex_readiness()
            if refresh and status in {"auth_check_required", "ready"}:
                from clio_agent.providers.catalog import get_provider  # noqa: PLC0415
                from clio_agent.providers.codex_errors import (  # noqa: PLC0415
                    CODEX_AUTHENTICATION_ERROR_MESSAGE,
                    contains_codex_authentication_error,
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
                    auth_failure = contains_codex_authentication_error(failure)
                    return {
                        "models": [],
                        "source": "unavailable",
                        "error": (CODEX_AUTHENTICATION_ERROR_MESSAGE if auth_failure else failure),
                        "connectivity": "ok" if auth_failure else "unreachable",
                        "auth": "rejected" if auth_failure else "deferred",
                        "latency_ms": None,
                        "generated_at": str(result.get("generated_at") or ""),
                    }
                status, message, verified, _ = _codex_readiness(ignore_startup=True)
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
