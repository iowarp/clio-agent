"""Generic provider sign-in API: start / complete / status / logout.

One ``POST /v1/providers/{provider_id}/auth`` endpoint (dispatched here by
provider kind) replaces the old ALCF-only path that 405'd every other
provider. Each provider kind plugs in through a small adapter; adding a new
subscription/OAuth provider means one new adapter here, never a route change.

``start`` returns the methods available: ``{flow_id, browser?:
{authorization_url, loopback}, device?: {user_code, verification_url,
interval}}``. ``complete`` takes a paste. ``status`` is a poll returning
pending/complete/failed with a reason. ``logout`` deletes the stored
credential. ALCF (argonne) and the direct Codex provider both go through
this one interface -- there is no provider-specific branch left in the route
itself (:mod:`clio_agent.gact.routes.provider_catalog_routes`).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.lm_provider_types import preset_api_key_env
from clio_agent.gact.provider_catalog_snapshot import invalidate_provider
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderPreset
from clio_agent.providers.dependencies import ProviderDependencyInstallError, ensure_argonne_support

__all__ = ["handle_auth_action", "supports_logout"]

StartHandler = Callable[
    [LMProviderPreset, dict[str, Any], Any, list[LMProviderPreset]], Awaitable[dict[str, Any]]
]
CompleteHandler = StartHandler
StatusHandler = Callable[[LMProviderPreset, str, Any, list[LMProviderPreset]], dict[str, Any]]
LogoutHandler = Callable[[LMProviderPreset, Any, list[LMProviderPreset]], Awaitable[dict[str, Any]]]


def _error(
    status_code: int, *, error: str, message: str, recoverable: bool = True
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, recoverable=recoverable)
        ).model_dump(exclude_none=True),
    )


# -- argonne (ALCF / Globus) --------------------------------------------------


async def _argonne_start(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del presets
    from clio_agent.providers import argonne_auth  # noqa: PLC0415

    # `force` is the wire signal for "the user explicitly clicked
    # Sign in / Sign in again" (never set implicitly). ALCF has no
    # pending-flow reuse to protect (unlike Codex's single-active-flow
    # registry): every explicit click here forces Globus's own
    # `prompt=login`, so "Sign in again" for `argonne_reauthentication_required`
    # never silently re-uses a browser session that produced the rejected
    # credential in the first place.
    force = bool(body.get("force"))
    try:
        installed_support = await asyncio.to_thread(ensure_argonne_support)
    except ProviderDependencyInstallError as exc:
        raise _error(
            503,
            error="dependency_install_failed",
            message=f"CLIO could not install ALCF sign-in support on the connected agent: {exc}",
        ) from exc
    try:
        pending = await asyncio.to_thread(argonne_auth.begin_authentication, force_login=force)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed 502
        raise _error(
            502,
            error="argonne_auth_failed",
            message=f"Could not complete Globus authentication: {exc}",
        ) from exc
    return {
        "flow_id": pending.flow_id,
        "browser": {"authorization_url": pending.authorization_url, "loopback": False},
        "instructions": (
            ("Installed ALCF sign-in support on this agent. " if installed_support else "")
            + f"Continue in {preset.auth_label or 'Globus'}, then paste the authorization code here."
        ),
    }


async def _argonne_complete(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset
    from clio_agent.providers import argonne_auth  # noqa: PLC0415

    flow_id = str(body.get("flow_id", ""))
    code = str(body.get("authorization_code") or body.get("paste") or "")
    try:
        await asyncio.to_thread(argonne_auth.complete_authentication, flow_id, code)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed 502
        raise _error(
            502,
            error="argonne_auth_failed",
            message=f"Could not complete Globus authentication: {exc}",
        ) from exc
    # The Globus tokens are shared by every ALCF cluster, so every argonne
    # provider's catalog evidence was produced under the old (signed-out)
    # credential; the next catalog read re-probes them.
    for argonne_preset in (p for p in presets if p.provider == "argonne"):
        invalidate_provider(app, argonne_preset.id)
    return {
        "is_authenticated": True,
        "instructions": "ALCF sign-in complete. Checking available models.",
    }


def _argonne_status(
    preset: LMProviderPreset, flow_id: str, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset, app, presets
    from clio_agent.providers import argonne_auth  # noqa: PLC0415

    # ALCF's flow has no async background half: "pending" means only "still
    # awaiting complete_authentication" -- the caller already learned the true
    # outcome from complete's own (synchronous) response.
    pending = argonne_auth.flow_is_pending(flow_id)
    return {"state": "pending" if pending else "complete", "reason": ""}


async def _argonne_logout(
    preset: LMProviderPreset, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset
    from clio_agent.providers import argonne_auth  # noqa: PLC0415

    try:
        await asyncio.to_thread(argonne_auth.sign_out)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed 502
        raise _error(
            502,
            error="argonne_logout_failed",
            message=f"Could not fully sign out of ALCF: {exc}",
        ) from exc
    # The Globus tokens are shared by every ALCF cluster, so every argonne
    # provider's cached catalog/handshake evidence was produced under the
    # now-revoked credential; the next catalog read re-probes them.
    for argonne_preset in (p for p in presets if p.provider == "argonne"):
        invalidate_provider(app, argonne_preset.id)
    return {"is_authenticated": False, "instructions": "Signed out of ALCF."}


# -- codex (direct Codex backend) ------------------------------------------


async def _codex_start(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset, app, presets
    from clio_agent.providers.codex import login_flow  # noqa: PLC0415
    from clio_agent.providers.codex.oauth import OAuthError  # noqa: PLC0415

    method = str(body.get("method") or "browser").strip().lower()
    # `force` is the wire signal for "the user explicitly clicked Sign in" --
    # never set implicitly by a submenu opening or a status poll. See
    # `login_flow.start_login`: without it, a still-pending flow for the same
    # method is returned AS-IS (no second listener, no new PKCE state) rather
    # than started twice, which is what produced a real "state mismatch"
    # failure when two flows both raced for the one loopback port.
    force = bool(body.get("force"))
    try:
        methods = await asyncio.to_thread(login_flow.start_login, method=method, force=force)
    except OAuthError as exc:
        raise _error(502, error="codex_auth_failed", message=str(exc)) from exc
    result: dict[str, Any] = {"flow_id": methods.flow_id, "instructions": methods.instructions}
    if methods.browser is not None:
        result["browser"] = methods.browser
    if methods.device is not None:
        result["device"] = methods.device
    return result


async def _codex_complete(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset, app, presets
    from clio_agent.providers.codex import login_flow  # noqa: PLC0415

    flow_id = str(body.get("flow_id", ""))
    flow = login_flow.get_login_flow(flow_id)
    if flow is None:
        raise _error(
            404,
            error="flow_not_found",
            message="This sign-in attempt has expired. Start sign-in again.",
        )
    paste = str(body.get("paste") or body.get("authorization_code") or "")
    await asyncio.to_thread(flow.submit_paste, paste)
    status, reason = flow.status()
    if status == "failed":
        raise _error(401, error="codex_auth_failed", message=reason or "Codex sign-in failed.")
    return {
        "is_authenticated": status == "complete",
        "instructions": "Finishing Codex sign-in...",
    }


def _codex_status(
    preset: LMProviderPreset, flow_id: str, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset
    from clio_agent.providers.codex import login_flow  # noqa: PLC0415
    from clio_agent.providers.codex.credentials import CodexCredentialStore  # noqa: PLC0415

    flow = login_flow.get_login_flow(flow_id)
    if flow is None:
        return {
            "state": "failed",
            "reason": "This sign-in attempt has expired. Start sign-in again.",
        }
    state, reason = flow.status()
    if state == "complete":
        credential = flow.credential()
        if credential is not None:
            CodexCredentialStore().save(credential)
            for codex_preset in (p for p in presets if p.provider == "codex"):
                invalidate_provider(app, codex_preset.id)
        login_flow.drop_login_flow(flow_id)
    elif state == "failed":
        login_flow.drop_login_flow(flow_id)
    return {"state": state, "reason": reason}


async def _codex_logout(
    preset: LMProviderPreset, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset
    from clio_agent.providers.codex.credentials import CodexCredentialStore  # noqa: PLC0415

    CodexCredentialStore().logout()
    for codex_preset in (p for p in presets if p.provider == "codex"):
        invalidate_provider(app, codex_preset.id)
    return {"is_authenticated": False, "instructions": "Signed out of Codex."}


# -- api_key (any cloud provider: OpenAI, Anthropic, OpenRouter, ...) --------
#
# Unlike the OAuth/subscription flows above, this is not dispatched by
# provider kind: every `requires_api_key` preset shares the same mechanic
# (`resolve_cloud_api_key` reads the SAME env var this writes -- see
# `clio_agent.providers.model_discovery.overlay`), so one generic pair of
# actions covers all of them. This exists so the picker's inline "Save key"
# can make a provider checkable WITHOUT the side effect PUT /v1/providers/lm
# has of also binding it as the active default -- saving OpenRouter's key
# must not switch the running agent onto OpenRouter.


def _save_api_key(preset: LMProviderPreset, app: Any, api_key: str) -> dict[str, Any]:
    if not preset.requires_api_key:
        raise _error(
            405,
            error="unsupported",
            message=f"provider '{preset.id}' does not use an API key.",
            recoverable=False,
        )
    if not api_key:
        raise _error(
            400, error="invalid_request", message="api_key is required.", recoverable=False
        )
    os.environ[preset_api_key_env(preset)] = api_key
    invalidate_provider(app, preset.id)
    return {
        "is_authenticated": True,
        "instructions": f"Saved the {preset.label} API key. Checking available models.",
    }


def _clear_api_key(preset: LMProviderPreset, app: Any) -> dict[str, Any]:
    if not preset.requires_api_key:
        raise _error(
            405,
            error="unsupported",
            message=f"provider '{preset.id}' does not use an API key.",
            recoverable=False,
        )
    os.environ.pop(preset_api_key_env(preset), None)
    invalidate_provider(app, preset.id)
    return {"is_authenticated": False, "instructions": f"Removed the {preset.label} API key."}


_START: dict[str, StartHandler] = {"argonne": _argonne_start, "codex": _codex_start}
_COMPLETE: dict[str, CompleteHandler] = {"argonne": _argonne_complete, "codex": _codex_complete}
_STATUS: dict[str, StatusHandler] = {"argonne": _argonne_status, "codex": _codex_status}
_LOGOUT: dict[str, LogoutHandler] = {"argonne": _argonne_logout, "codex": _codex_logout}

_NO_AUTH_FLOW_MESSAGE = (
    "provider '{id}' uses {kind} auth; pass api_key directly to PUT /v1/providers/lm."
)


def supports_logout(provider_kind: str) -> bool:
    """Whether ``provider_kind`` has a real ``POST .../auth {action: logout}`` handler.

    The ONE source of truth for "does Sign out do anything" -- Claude Code's
    subscription is the user's own Claude CLI login, which CLIO does not own
    and cannot revoke, so it (correctly) has no entry in :data:`_LOGOUT` and
    this returns ``False`` for it. The picker/Settings action panel reads
    this off the preset (``LMProviderPreset.supports_logout``) rather than
    inferring it from ``auth_method`` client-side, which cannot tell
    Claude Code's subscription apart from Codex's or ALCF's.
    """
    return provider_kind in _LOGOUT


async def handle_auth_action(
    *,
    preset: LMProviderPreset,
    action: str,
    body: dict[str, Any],
    app: Any,
    presets: list[LMProviderPreset],
) -> dict[str, Any]:
    """Dispatch one ``POST /v1/providers/{id}/auth`` action by provider kind."""

    if action == "start":
        start_handler = _START.get(preset.provider)
        if start_handler is None:
            raise _error(
                405,
                error="unsupported",
                message=_NO_AUTH_FLOW_MESSAGE.format(
                    id=preset.id, kind="api_key" if preset.requires_api_key else "no"
                ),
                recoverable=False,
            )
        return {"provider_id": preset.id, **await start_handler(preset, body, app, presets)}
    if action == "complete":
        complete_handler = _COMPLETE.get(preset.provider)
        if complete_handler is None:
            raise _error(
                405,
                error="unsupported",
                message=f"provider '{preset.id}' has no sign-in to complete.",
            )
        return {"provider_id": preset.id, **await complete_handler(preset, body, app, presets)}
    if action == "status":
        status_handler = _STATUS.get(preset.provider)
        if status_handler is None:
            raise _error(
                405, error="unsupported", message=f"provider '{preset.id}' has no sign-in to poll."
            )
        flow_id = str(body.get("flow_id", ""))
        return {"provider_id": preset.id, **status_handler(preset, flow_id, app, presets)}
    if action == "logout":
        logout_handler = _LOGOUT.get(preset.provider)
        if logout_handler is None:
            raise _error(
                405, error="unsupported", message=f"provider '{preset.id}' has no stored sign-in."
            )
        return {"provider_id": preset.id, **await logout_handler(preset, app, presets)}
    if action == "save_api_key":
        api_key = str(body.get("api_key") or "").strip()
        return {"provider_id": preset.id, **_save_api_key(preset, app, api_key)}
    if action == "clear_api_key":
        return {"provider_id": preset.id, **_clear_api_key(preset, app)}
    raise _error(
        400,
        error="invalid_action",
        message=f"unknown authentication action: {action}",
        recoverable=False,
    )
