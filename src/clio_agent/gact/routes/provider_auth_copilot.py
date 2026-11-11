"""GitHub Copilot adapter for the generic ``POST /v1/providers/{id}/auth`` interface.

Copilot has no sign-in of its own. The Copilot row hands sign-in to the same
GitHub App device flow Settings > Sources uses
(:mod:`clio_agent.gact.storage.github_oauth`) and writes the same account-vault
entry, so one GitHub sign-in in CLIO covers sources, CLIO's ``gh`` and Copilot.

``start`` returns the device code, or, when CLIO is already signed in to
GitHub, a flow that is complete as soon as it is polled (the account is reused,
not asked for again). ``status``/``complete`` poll GitHub for the code's
approval. There is no Copilot logout: signing out of GitHub happens once, in
Settings > Sources, and that drops the Copilot sessions too
(:func:`install_copilot_token_source`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.provider_catalog_snapshot import invalidate_provider
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderPreset
from clio_agent.providers.dependencies import (
    ProviderDependencyInstallError,
    ensure_copilot_support,
)

__all__ = [
    "copilot_complete",
    "copilot_handshake_gate",
    "copilot_readiness",
    "copilot_start",
    "copilot_status",
    "install_copilot_token_source",
]

#: Flow ids for a start that reused the already signed-in GitHub account.
READY_FLOW_PREFIX = "github-account-ready:"
_GITHUB = "github"


def _error(status_code: int, *, error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def _storage(app: Any) -> Any:
    service = getattr(app.state, "connected_storage", None)
    if service is None:
        raise _error(
            503,
            error="github_sign_in_unavailable",
            message="GitHub sign-in is not available on this CLIO.",
        )
    return service


def _account(service: Any) -> Any:
    return service.auth.account_login(service.principal, service.store.clio_id, _GITHUB)


def _github_grant_changed() -> None:
    """GitHub signed out, refused the token or was granted again: Copilot is unverified.

    Releases the sessions holding the old token and marks the cached Copilot model
    list as belonging to the previous grant, so the row is never shown ready for an
    account that has not been checked.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from clio_agent.providers.copilot.sessions import SESSION_POOL  # noqa: PLC0415
    from clio_agent.providers.model_discovery.overlay import update_entry_fields  # noqa: PLC0415

    SESSION_POOL.release_all()
    update_entry_fields("copilot", {"account_changed_at": datetime.now(timezone.utc).isoformat()})


def install_copilot_token_source(service: Any) -> None:
    """Point the Copilot provider at this CLIO's GitHub account (once, at startup).

    The token is read on every use through the vault's own refresh path, so a
    sign-in, refresh or sign-out takes effect without a restart. Dropping the
    GitHub clients (sign-out, a rejected token, a new grant) releases the
    Copilot sessions and marks its model list unverified.
    """
    from clio_agent.providers.copilot import auth  # noqa: PLC0415

    def token() -> tuple[str, int]:
        return service.auth.account_token(service.principal, service.store.clio_id, _GITHUB)

    auth.set_token_source(token)
    service.github.on_client_dropped(_github_grant_changed)


def _refresh_copilot(app: Any, service: Any, presets: list[LMProviderPreset]) -> None:
    """A new GitHub grant: drop clients holding the old one, re-check the Copilot rows."""
    service.github.clear()
    for preset in (p for p in presets if p.provider == "copilot"):
        invalidate_provider(app, preset.id)


def copilot_readiness(app: Any, *, ignore_startup: bool = False) -> tuple[str, str, bool, str]:
    """``(status, message, verified, default_model)`` for the Copilot row.

    Ready only when CLIO is signed in to GitHub AND a ``list_models()`` read under
    that sign-in has succeeded and is still fresh: a signed-in account without a
    seat (or with the CLI policy off) never shows as ready. Reads only local
    state, never the network.
    """
    from clio_agent.providers import model_discovery  # noqa: PLC0415
    from clio_agent.providers.copilot.errors import COPILOT_SIGNED_OUT_MESSAGE  # noqa: PLC0415
    from clio_agent.providers.support_restore import missing_support_status  # noqa: PLC0415

    if importlib.util.find_spec("copilot") is None:
        return (*missing_support_status("copilot"), False, "")
    service = getattr(app.state, "connected_storage", None)
    if service is None or not service.auth.account_connected(
        service.principal, service.store.clio_id, _GITHUB
    ):
        return "auth_required", COPILOT_SIGNED_OUT_MESSAGE, False, ""
    startup_check = getattr(app.state, "provider_catalog_startup_task", None)
    if not ignore_startup and startup_check is not None and not startup_check.done():
        return "auth_check_required", "GitHub Copilot models are being checked", False, ""
    try:
        overlay = model_discovery.overlay_models_wire("copilot", "copilot")
    except model_discovery.OverlayMalformedError as exc:
        return "unavailable", f"GitHub Copilot model catalog is invalid: {exc}", False, ""
    if overlay and overlay.get("models") and not overlay.get("staleness"):
        return (
            "ready",
            "GitHub Copilot models verified for the signed-in GitHub account",
            True,
            str(overlay.get("default_model") or ""),
        )
    return (
        "auth_check_required",
        "Signed in to GitHub. Check the provider to verify Copilot access.",
        False,
        "",
    )


def _unverified_handshake(error: str, *, connectivity: str, auth: str, at: str) -> dict[str, Any]:
    return {
        "models": [],
        "source": "unavailable",
        "error": error,
        "connectivity": connectivity,
        "auth": auth,
        "latency_ms": None,
        "generated_at": at,
    }


async def copilot_handshake_gate(
    app: Any, preset: LMProviderPreset, *, refresh: bool
) -> dict[str, Any] | None:
    """The Copilot handshake answer when it is not verified, else ``None``.

    ``refresh`` re-reads ``list_models()`` under the current GitHub sign-in. A
    refused sign-in reports ``auth: rejected``; missing support names the install;
    any other failure keeps the runtime's own words.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from clio_agent.providers import model_discovery  # noqa: PLC0415
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415
    from clio_agent.providers.copilot.errors import (  # noqa: PLC0415
        COPILOT_INSTALL_FAILED_MESSAGE,
        COPILOT_SIGNED_OUT_MESSAGE,
        contains_copilot_auth_required,
        contains_copilot_dependency_error,
    )

    status, message, verified, _ = copilot_readiness(app)
    if refresh and status not in {"auth_required", "install_required", "support_restoring"}:
        provider = get_provider(preset.id)
        results = await model_discovery.refresh_all(presets=[provider]) if provider else []
        result = results[0] if results else {}
        failure = str(result.get("failed_reason") or "")
        if failure:
            refused = contains_copilot_auth_required(failure)
            missing = contains_copilot_dependency_error(failure)
            return _unverified_handshake(
                COPILOT_SIGNED_OUT_MESSAGE
                if refused
                else COPILOT_INSTALL_FAILED_MESSAGE
                if missing
                else failure,
                connectivity="ok" if refused else "unreachable",
                auth="rejected" if refused else "deferred",
                at=str(result.get("generated_at") or ""),
            )
        status, message, verified, _ = copilot_readiness(app, ignore_startup=True)
    if verified:
        return None
    return _unverified_handshake(
        message,
        connectivity="unreachable" if status == "install_required" else "skipped",
        auth="missing" if status in {"auth_required", "install_required"} else "deferred",
        at=datetime.now(timezone.utc).isoformat(),
    )


async def copilot_start(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    """Start (or reuse) CLIO's GitHub sign-in for the Copilot row."""
    del preset, presets
    from clio_agent.gact.storage.auth import application  # noqa: PLC0415

    service = _storage(app)
    try:
        installed = await asyncio.to_thread(ensure_copilot_support)
    except ProviderDependencyInstallError as exc:
        raise _error(
            503,
            error="dependency_install_failed",
            message=f"CLIO could not install GitHub Copilot support on the connected agent: {exc}",
        ) from exc
    prefix = "Installed GitHub Copilot support on this agent. " if installed else ""
    record = _account(service)
    if not body.get("force") and await asyncio.to_thread(service.auth.connected, record):
        name = service.auth.account_name(service.principal, service.store.clio_id, _GITHUB)
        return {
            "flow_id": READY_FLOW_PREFIX + secrets.token_urlsafe(12),
            "instructions": prefix
            + f"Using CLIO's GitHub sign-in{f' ({name})' if name else ''}. Checking Copilot.",
        }
    try:
        started = await asyncio.to_thread(service.auth.start, record, application(_GITHUB))
    except (ValueError, PermissionError) as exc:
        raise _error(502, error="github_auth_failed", message=str(exc)) from exc
    return {
        "flow_id": started["flow_id"],
        "device": {
            "user_code": started["user_code"],
            "verification_url": started["authorization_url"],
            "interval": int(started["interval"]),
        },
        "instructions": prefix
        + "Enter the code on GitHub. This one GitHub sign-in covers GitHub sources and Copilot.",
    }


async def _poll(app: Any, flow_id: str, presets: list[LMProviderPreset]) -> tuple[str, str]:
    """``(state, reason)`` for a Copilot sign-in flow (polls GitHub at its own interval)."""
    service = _storage(app)
    record = _account(service)
    if flow_id.startswith(READY_FLOW_PREFIX):
        if await asyncio.to_thread(service.auth.connected, record):
            return "complete", ""
        return "failed", "CLIO is no longer signed in to GitHub. Sign in again."
    try:
        approved = await asyncio.to_thread(service.auth.complete, record, flow_id, "")
    except (ValueError, PermissionError) as exc:
        return "failed", str(exc)
    if not approved:
        return "pending", ""
    _refresh_copilot(app, service, presets)
    return "complete", ""


async def copilot_status(
    preset: LMProviderPreset, flow_id: str, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    """Poll the device code: pending until GitHub reports it approved."""
    del preset
    state, reason = await _poll(app, flow_id, presets)
    return {"state": state, "reason": reason}


async def copilot_complete(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    """The device flow has nothing to paste: ``complete`` is one more poll."""
    del preset
    state, reason = await _poll(app, str(body.get("flow_id", "")), presets)
    if state == "failed":
        raise _error(401, error="github_auth_failed", message=reason)
    return {
        "is_authenticated": state == "complete",
        "instructions": "Signed in to GitHub. Checking Copilot."
        if state == "complete"
        else "Waiting for GitHub to confirm the code...",
    }
