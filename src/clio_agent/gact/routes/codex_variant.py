"""Codex provider TRANSPORT selection (S1b): ``sdk`` vs ``direct`` readiness.

Owns the SDK transport's live readiness probe and the dispatch between it and
the direct transport's own check for the ``PUT /v1/providers/lm`` bind path --
kept out of ``gact/routes/providers.py`` (a baselined god-file under the
#714/#774 file-size ratchet) per the no-accretion ground rule: a fix that
adds more than a trivial amount of code goes in an owner module, not appended
to a god file. ``codex_variant`` ITSELF is validated by
``LMProviderConfig.__post_init__`` (same as the sibling ``codex_transport``/
``claude_code_transport`` fields) -- the route's existing generic
"failed to configure LM" handler turns that into the 400.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.config import LMProviderConfig
    from clio_agent.gact.lm_provider_types import LMProviderRequest


async def codex_sdk_readiness() -> tuple[str, str, bool, str]:
    """Return ``(status, message, verified, default_model)`` for the SDK transport.

    Unlike the direct transport's readiness (a cheap overlay/credential-store
    read), the SDK transport has no cheap local check -- its availability is
    asked live of the SDK itself, which is exactly what a bind attempt is
    for. The result is recorded to the overlay so a passive catalog read
    reflects it immediately rather than reporting "not checked yet" right
    after a successful bind (:mod:`clio_agent.gact.provider_catalog`).
    """
    from clio_agent.providers import model_discovery
    from clio_agent.providers.codex.sdk_discovery import discover_codex_sdk_async

    result = await discover_codex_sdk_async()
    try:
        model_discovery.record_refresh(result)
    except model_discovery.OverlayMalformedError:
        pass  # best-effort cache write; the bind decision below is unaffected
    if result.failed_reason:
        code = result.failed_reason.split(":", 1)[0]
        status = {
            "codex_sdk_not_installed": "install_required",
            "codex_sdk_signed_out": "auth_required",
        }.get(code, "unavailable")
        return status, result.failed_reason, False, ""
    return "ready", "Codex SDK sign-in and models verified", True, result.default_model


async def resolve_codex_readiness(
    variant: str, direct_readiness: Callable[[], tuple[str, str, bool, str]]
) -> tuple[str, str, bool, str]:
    """Dispatch a bind attempt's readiness check to the bound transport.

    ``direct_readiness`` is the caller's own (synchronous, overlay-backed)
    direct-transport check -- passed in rather than imported here so this
    module never depends on ``gact/routes/providers.py``'s closures.
    """
    return await codex_sdk_readiness() if variant == "sdk" else direct_readiness()


async def apply_codex_readiness_gate(
    cfg: "LMProviderConfig",
    req: "LMProviderRequest",
    direct_readiness: Callable[[], tuple[str, str, bool, str]],
) -> None:
    """Verify the bound codex transport is ready (raising a 401/503), else fill a default model.

    The exact behavior ``gact/routes/providers.py``'s bind route had inline
    before S1b added the ``sdk`` transport -- moved here (not just the new
    dispatch) so the whole codex readiness gate has one owner, per the
    no-accretion ground rule.
    """
    status, message, verified, default_model = await resolve_codex_readiness(
        cfg.codex_variant, direct_readiness
    )
    if not verified:
        raise HTTPException(
            status_code=401 if status in {"auth_required", "auth_check_required"} else 503,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error=(
                        "codex_auth_required"
                        if status in {"auth_required", "auth_check_required"}
                        else "codex_unavailable"
                    ),
                    message=message,
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    if not req.model and default_model:
        cfg.model = default_model


__all__ = ["apply_codex_readiness_gate", "codex_sdk_readiness", "resolve_codex_readiness"]
