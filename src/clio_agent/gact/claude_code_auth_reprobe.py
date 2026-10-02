"""Notice a Claude Code sign-in made outside CLIO, by asking the CLI when it matters.

Claude Code's sign-in is owned by its own CLI (``claude auth login``), so a
person can sign in again in a terminal while CLIO keeps running. CLIO caches
the provider's readiness in its model-discovery overlay; once a check ran while
the person was signed out, that cache said "not signed in" until someone
pressed Check (the #1455 sequence: a turn failed signed out, the person signed
in in a terminal, and CLIO still showed Claude Code as unavailable).

This owner module re-asks the CLI, never on a timer, only at the moments the
answer is needed:

* a message is accepted for a Claude Code model (``message_submission``);
* a client loads the provider list (``GET /v1/providers/lm``: the composer and
  the model picker both read it).

Each re-ask runs only while the cached state is "not verified", is
single-flight (concurrent callers share one probe), and first runs the cheap
``<binary> auth status`` check; the full provider refresh (catalog + overlay)
runs only when the CLI now reports a sign-in. Every outcome is a typed,
traced reason (``claude_code_auth_reprobe``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

__all__ = [
    "claude_code_overlay_verified",
    "reprobe_claude_code_auth",
    "reprobe_claude_code_for_provider",
    "reprobe_for_message",
    "reprobe_for_provider_list",
]

_LOCK_ATTR = "claude_code_auth_reprobe_lock"


def claude_code_overlay_verified() -> tuple[bool, str, str]:
    """Whether the cached overlay records a verified Claude Code sign-in.

    Returns:
        ``(verified, default_model, malformed_error)``. ``malformed_error`` is
        the overlay's own error text when it cannot be read ("" otherwise).
    """

    from clio_agent.providers import model_discovery  # noqa: PLC0415

    try:
        overlay = model_discovery.overlay_models_wire("claude_code", "claude_code")
    except model_discovery.OverlayMalformedError as exc:
        return False, "", str(exc)
    verified = bool(overlay and overlay.get("models") and not overlay.get("staleness"))
    default = str((overlay or {}).get("default_model") or "") if verified else ""
    return verified, default, ""


def _lock(app: Any) -> asyncio.Lock:
    lock = getattr(app.state, _LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(app.state, _LOCK_ATTR, lock)
    return lock


def _record(outcome: str, *, trigger: str, detail: str = "") -> None:
    logger.info(
        "claude_code_auth_reprobe outcome=%s trigger=%s detail=%s", outcome, trigger, detail
    )
    trace.event("PROVIDER-AUTH", "claude_code_auth_reprobe outcome=%s trigger=%s", outcome, trigger)


async def reprobe_claude_code_auth(app: Any, preset: Any, *, trigger: str) -> bool:
    """Re-ask the Claude Code CLI for its sign-in when CLIO's cache says "not verified".

    Args:
        app: The GACT app (holds the single-flight lock and provider catalog).
        preset: The Claude Code catalog provider (what ``refresh_all`` takes).
        trigger: What asked (``message`` / ``provider_list``), for the trace.

    Returns:
        Whether Claude Code is verified after the call.
    """

    startup = getattr(app.state, "provider_catalog_startup_task", None)
    if startup is not None and not startup.done():
        return False  # the startup check is already asking; its answer lands shortly
    if claude_code_overlay_verified()[0]:
        return True
    async with _lock(app):
        if claude_code_overlay_verified()[0]:
            return True  # a concurrent caller's probe already found the sign-in
        from clio_agent.gact.provider_catalog_snapshot import invalidate_provider  # noqa: PLC0415
        from clio_agent.providers import model_discovery  # noqa: PLC0415
        from clio_agent.providers.model_discovery.claude_code import (  # noqa: PLC0415
            AUTH_CHECK_UNRESPONSIVE,
            CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S,
            _auth_status,
            _resolve_claude_binary,
        )

        try:
            binary = await asyncio.to_thread(_resolve_claude_binary)
        except Exception as exc:  # noqa: BLE001 - not installed is a reported outcome
            _record("binary_unavailable", trigger=trigger, detail=repr(exc))
            return False
        signed_in, reason = await asyncio.to_thread(
            _auth_status, binary, timeout=CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S
        )
        if not signed_in:
            slow = reason.startswith(AUTH_CHECK_UNRESPONSIVE)  # slow is not signed out (#1577)
            _record(
                "auth_check_unresponsive" if slow else "signed_out", trigger=trigger, detail=reason
            )
            return False
        await model_discovery.refresh_all(presets=[preset])
        invalidate_provider(app, preset.id)
        verified = claude_code_overlay_verified()[0]
        _record("signed_in" if verified else "signed_in_refresh_unverified", trigger=trigger)
        return verified


async def reprobe_claude_code_for_provider(app: Any, provider_id: str, *, trigger: str) -> None:
    """Re-ask when ``provider_id`` names a Claude Code provider (no-op otherwise)."""

    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    provider = get_provider(provider_id) if provider_id else None
    if provider is not None and provider.provider_kind == "claude_code":
        await reprobe_claude_code_auth(app, provider, trigger=trigger)


def _message_provider_id(app: Any, sid: str, req: Any) -> str:
    """The provider a message will run on: its model ref, its session's, else the default."""

    from clio_agent.gact.providers.config import (  # noqa: PLC0415
        _effective_lm_config,
        _model_ref_dict,
    )

    session = app.state.sessions.get(sid)
    for candidate in (getattr(req, "model", None), getattr(session, "model", None)):
        if candidate is None:
            continue
        provider_id = str(_model_ref_dict(candidate).get("provider_id") or "").strip()
        if provider_id:
            return provider_id
    return str(_effective_lm_config(app).get("provider_id") or "")


async def reprobe_for_message(app: Any, sid: str, req: Any) -> None:
    """Turn start: re-ask Claude Code before accepting a message that will run on it."""

    await reprobe_claude_code_for_provider(
        app, _message_provider_id(app, sid, req), trigger="message"
    )


async def reprobe_for_provider_list(app: Any) -> None:
    """Provider list load: re-ask when Claude Code is the configured default provider."""

    from clio_agent.gact.providers.config import _effective_lm_config  # noqa: PLC0415

    provider_id = str(_effective_lm_config(app).get("provider_id") or "")
    await reprobe_claude_code_for_provider(app, provider_id, trigger="provider_list")
