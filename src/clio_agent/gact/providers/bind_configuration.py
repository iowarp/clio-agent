"""Prepare default model binds, reusing a verified binding for effort-only edits."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

from clio_agent.gact.providers import response_settings
from clio_agent.gact.providers.auth import _is_placeholder_api_key, _resolve_argonne_runtime_api_key
from clio_agent.gact.providers.config import _effective_lm_config, requested_thinking_level
from clio_agent.gact.routes.codex_readiness import apply_codex_readiness_gate, await_startup_check
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderRequest

if TYPE_CHECKING:
    from clio_agent.config import LMProviderConfig

Readiness = Callable[[], tuple[str, str, bool, str]]
logger = logging.getLogger(__name__)


def _thinking_configuration(app: FastAPI, req: LMProviderRequest) -> LMProviderConfig | None:
    """Copy a live binding only when the request changes nothing except effort."""
    from clio_agent.config import LMProviderConfig  # noqa: PLC0415

    bound = getattr(getattr(app.state, "agent", None), "_provider_config", None)
    if not isinstance(bound, LMProviderConfig) or "thinking_level" not in req.model_fields_set:
        return None
    current = _effective_lm_config(app)
    fields = (
        "provider_id",
        "provider",
        "api_base",
        "model",
        "provider_options",
        *response_settings.SETTING_FIELDS,
        "thinking_budget",
        "turn_timeout_s",
    )
    for name in fields:
        default = LMProviderRequest.model_fields[name].default
        if getattr(req, name) != current.get(name, default):
            return None
    if not _is_placeholder_api_key(req.api_key) and req.api_key != bound.api_key:
        return None
    transport = {"codex": bound.codex_transport, "claude_code": bound.claude_code_transport}.get(
        req.provider
    )
    requested_transport = req.transport or {"codex": "websocket", "claude_code": "sdk"}.get(
        req.provider
    )
    if requested_transport != transport:
        return None

    from clio_agent.providers import thinking_levels  # noqa: PLC0415
    from clio_agent.providers.capabilities.dialects import claude_code  # noqa: PLC0415

    cfg = copy.copy(bound)
    level = claude_code.shipped_default_thinking_level(
        cfg.provider,
        cfg.model,
        req.thinking_level,
        cfg.thinking_budget,
    )
    cfg.thinking_level = (
        thinking_levels.validate_thinking_level(level) if level is not None else None
    )
    return cfg


async def prepare_bind_configuration(
    app: FastAPI,
    req: LMProviderRequest,
    codex_readiness: Readiness,
    claude_readiness: Readiness,
) -> tuple[LMProviderConfig, bool]:
    """Return the new binding and whether discovery facts remain unchanged.

    An effort-only edit preserves credentials, transport and discovered limits.
    Any other change follows the complete readiness/handshake path.
    """
    if cfg := _thinking_configuration(app, req):
        return cfg, True

    from clio_agent.config import LMProviderConfig  # noqa: PLC0415

    # Argonne / ALCF: if the TUI didn't ship an api_key, mint
    # one from the user's stored Globus session. ``LMProviderConfig``
    # will do this lazily inside __post_init__ too, but we resolve
    # eagerly here so the bound ``cfg`` (and the main agent's LMs built
    # from it) carry the real token, and so a missing token surfaces the
    # actionable structured 401 below instead of a later opaque LM error.
    # The request's "x" default is a no-auth placeholder, not a key: leave it
    # empty so the config resolves a saved or CLIO-managed deployment key (F011b).
    resolved_api_key = "" if _is_placeholder_api_key(req.api_key) else req.api_key
    if req.provider == "argonne" and _is_placeholder_api_key(resolved_api_key):
        auth_exc: Exception | None
        try:
            resolved_api_key = _resolve_argonne_runtime_api_key()
        except Exception as exc:
            logger.exception("Provider authentication failed reason=argonne_auth_required")
            resolved_api_key = ""
            auth_exc = exc
        else:
            auth_exc = None
        if not resolved_api_key:
            raise HTTPException(
                status_code=401,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="argonne_auth_required",
                        message=(
                            "ALCF provider selected but no Globus token "
                            "is available. Run "
                            "`python -m clio_agent.providers.argonne_auth "
                            "authenticate` once, or pass api_key in this "
                            "request."
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from auth_exc

    is_codex, is_cc = req.provider == "codex", req.provider == "claude_code"
    if is_codex or is_cc:
        await await_startup_check(app)
    cfg = LMProviderConfig(
        provider=req.provider,  # type: ignore[arg-type]  # str validated at boundary
        provider_id=req.provider_id,
        api_base=req.api_base,
        model=req.model,
        api_key=resolved_api_key,
        provider_options=req.provider_options,
        **response_settings.config_kwargs(req),
        thinking_budget=req.thinking_budget,
        thinking_level=requested_thinking_level(app, req),  # #895: see its provenance rule
        # Per-provider transport (v0.8.0): only the bound provider's field reads req.transport.
        codex_transport=(req.transport or "websocket") if is_codex else "websocket",  # type: ignore[arg-type]  # LMProviderConfig validates
        claude_code_transport=(req.transport or "sdk") if is_cc else "sdk",  # type: ignore[arg-type]  # LMProviderConfig validates; deleted values 400 typed
    )
    if not cfg.api_key:
        # Nothing saved or managed resolved: keep the request's placeholder, as
        # keyless transports (codex, claude_code, local no-auth servers) expect.
        cfg.api_key = req.api_key or "x"
    if is_cc:
        status, message, verified, default_model = claude_readiness()
        if not verified:
            from clio_agent.providers import model_discovery  # noqa: PLC0415
            from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

            provider = get_provider(req.provider_id or req.provider)
            if provider is not None:
                await model_discovery.refresh_all(presets=[provider])
            status, message, verified, default_model = claude_readiness()
        if not verified:
            raise HTTPException(
                status_code=401 if status in {"auth_check_required", "auth_required"} else 503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error=(
                            "claude_code_install_required"
                            if status in {"install_required", "support_restoring"}
                            else "claude_code_auth_required"
                        ),
                        message=message,
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if not req.model and default_model:
            cfg.model = default_model
    if is_codex:
        await apply_codex_readiness_gate(cfg, req, codex_readiness)
    # Per-provider handshake folds per-model config into cfg (context-aware
    # max_tokens, capability flags); a handshake failure keeps the static config.
    handshake_report = None
    try:
        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )

        handshake_report = await run_handshake(
            HandshakeContext(
                provider_id=req.provider_id or req.provider,
                provider_kind=req.provider,
                api_base=req.api_base,
                api_key="" if _is_placeholder_api_key(cfg.api_key) else cfg.api_key,
                target_model=req.model,
                auth_mode="active",
            ),
            force=True,
        )
        cfg.apply_handshake(handshake_report, user_set_max_tokens=(req.max_tokens or 0) > 0)
    except Exception:
        logger.exception("Provider discovery failed reason=lm_handshake_failed")
        handshake_report = None
    app.state.lm_handshake_report = handshake_report
    return cfg, False
