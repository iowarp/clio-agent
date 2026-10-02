"""The Codex bind readiness gate for ``PUT /v1/providers/lm``.

Kept out of ``gact/routes/providers.py`` (a baselined god-file under the
#714/#774 file-size ratchet) per the no-accretion ground rule: a fix that adds
more than a trivial amount of code goes in an owner module, not appended to a
god file. Codex has one transport (direct); its readiness is the caller's own
overlay/credential-store check.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.config import LMProviderConfig
    from clio_agent.gact.lm_provider_types import LMProviderRequest


logger = logging.getLogger(__name__)

#: How long a bind waits for the startup model check still in flight.
STARTUP_CHECK_WAIT_S = 60.0


async def await_startup_check(app: Any) -> None:
    """Let a bind made right after launch wait for the startup model check (bounded).

    Without this, choosing a model in the first seconds answered "models are being
    checked" (401) although the check was about to finish.
    """
    task = getattr(app.state, "provider_catalog_startup_task", None)
    if task is not None and not task.done():
        done, _pending = await asyncio.wait({task}, timeout=STARTUP_CHECK_WAIT_S)
        if not done:
            # Not silent (#1577): the bind goes on with what the check has recorded so far.
            logger.warning(
                "provider bind stopped waiting for the startup model check "
                "reason=startup_check_still_running waited_s=%.0f",
                STARTUP_CHECK_WAIT_S,
            )


async def apply_codex_readiness_gate(
    cfg: "LMProviderConfig",
    req: "LMProviderRequest",
    readiness: Callable[[], tuple[str, str, bool, str]],
) -> None:
    """Verify Codex is ready (raising a typed 401/503), else fill the default model.

    ``readiness`` is the caller's own (synchronous, overlay-backed) check -- passed
    in rather than imported so this module never depends on
    ``gact/routes/providers.py``'s closures.
    """
    status, message, verified, default_model = readiness()
    if status == "auth_check_required":
        # Signed in but never checked (no startup check ran): check now, as the
        # Claude Code bind does, instead of refusing a working sign-in.
        from clio_agent.providers import model_discovery  # noqa: PLC0415
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        provider = get_provider("codex")
        if provider is not None:
            await model_discovery.refresh_all(presets=[provider])
        status, message, verified, default_model = readiness()
    if not verified:
        auth = status in {"auth_required", "auth_check_required"}
        raise HTTPException(
            status_code=401 if auth else 503,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="codex_auth_required" if auth else "codex_unavailable",
                    message=message,
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    if not req.model and default_model:
        cfg.model = default_model


__all__ = ["apply_codex_readiness_gate", "await_startup_check"]
