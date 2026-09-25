"""Generic provider sign-in API: start / complete / status / logout.

One ``POST /v1/providers/{provider_id}/auth`` endpoint (dispatched here by
provider kind) replaces the old ALCF-only path that 405'd every other
provider. Each provider kind plugs in through a small adapter; adding a new
subscription/OAuth provider means one new adapter here, never a route change.

``start`` returns the methods available: ``{flow_id, browser?:
{authorization_url, loopback}, device?: {user_code, verification_url,
interval}}``. ``complete`` takes a paste. ``status`` is a poll returning
pending/complete/failed with a reason. ``logout`` deletes the stored
credential. ALCF (argonne) and the direct ChatGPT provider both go through
this one interface -- there is no provider-specific branch left in the route
itself (:mod:`clio_agent.gact.routes.provider_catalog_routes`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.provider_catalog_snapshot import invalidate_provider
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderPreset
from clio_agent.providers.dependencies import ProviderDependencyInstallError, ensure_argonne_support

__all__ = ["handle_auth_action"]

StartHandler = Callable[
    [LMProviderPreset, dict[str, Any], Any, list[LMProviderPreset]], Awaitable[dict[str, Any]]
]
CompleteHandler = StartHandler
StatusHandler = Callable[[LMProviderPreset, str, Any, list[LMProviderPreset]], dict[str, Any]]
LogoutHandler = Callable[[LMProviderPreset, Any, list[LMProviderPreset]], dict[str, Any]]


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
    del body, presets
    from clio_agent.providers import argonne_auth  # noqa: PLC0415

    try:
        installed_support = await asyncio.to_thread(ensure_argonne_support)
    except ProviderDependencyInstallError as exc:
        raise _error(
            503,
            error="dependency_install_failed",
            message=f"CLIO could not install ALCF sign-in support on the connected agent: {exc}",
        ) from exc
    try:
        pending = await asyncio.to_thread(argonne_auth.begin_authentication)
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


def _argonne_logout(
    preset: LMProviderPreset, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset, app, presets
    raise _error(
        405,
        error="unsupported",
        message="ALCF sign-out is not supported yet; revoke CLIO's access at globus.org.",
        recoverable=False,
    )


# -- chatgpt (direct Codex backend) ------------------------------------------


async def _chatgpt_start(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset, app, presets
    from clio_agent.providers.chatgpt import login_flow  # noqa: PLC0415
    from clio_agent.providers.chatgpt.oauth import OAuthError  # noqa: PLC0415

    flow = login_flow.create_login_flow()
    method = str(body.get("method") or "browser").strip().lower()
    try:
        methods = await asyncio.to_thread(
            flow.start_device if method == "device" else flow.start_browser
        )
    except OAuthError as exc:
        raise _error(502, error="chatgpt_auth_failed", message=str(exc)) from exc
    result: dict[str, Any] = {"flow_id": methods.flow_id, "instructions": methods.instructions}
    if methods.browser is not None:
        result["browser"] = methods.browser
    if methods.device is not None:
        result["device"] = methods.device
    return result


async def _chatgpt_complete(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset, app, presets
    from clio_agent.providers.chatgpt import login_flow  # noqa: PLC0415

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
        raise _error(401, error="chatgpt_auth_failed", message=reason or "ChatGPT sign-in failed.")
    return {
        "is_authenticated": status == "complete",
        "instructions": "Finishing ChatGPT sign-in...",
    }


def _chatgpt_status(
    preset: LMProviderPreset, flow_id: str, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset
    from clio_agent.providers.chatgpt import login_flow  # noqa: PLC0415
    from clio_agent.providers.chatgpt.credentials import ChatGptCredentialStore  # noqa: PLC0415

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
            ChatGptCredentialStore().save(credential)
            for chatgpt_preset in (p for p in presets if p.provider == "chatgpt"):
                invalidate_provider(app, chatgpt_preset.id)
        login_flow.drop_login_flow(flow_id)
    elif state == "failed":
        login_flow.drop_login_flow(flow_id)
    return {"state": state, "reason": reason}


def _chatgpt_logout(
    preset: LMProviderPreset, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    del preset
    from clio_agent.providers.chatgpt.credentials import ChatGptCredentialStore  # noqa: PLC0415

    ChatGptCredentialStore().logout()
    for chatgpt_preset in (p for p in presets if p.provider == "chatgpt"):
        invalidate_provider(app, chatgpt_preset.id)
    return {"is_authenticated": False, "instructions": "Signed out of ChatGPT."}


_START: dict[str, StartHandler] = {"argonne": _argonne_start, "chatgpt": _chatgpt_start}
_COMPLETE: dict[str, CompleteHandler] = {"argonne": _argonne_complete, "chatgpt": _chatgpt_complete}
_STATUS: dict[str, StatusHandler] = {"argonne": _argonne_status, "chatgpt": _chatgpt_status}
_LOGOUT: dict[str, LogoutHandler] = {"argonne": _argonne_logout, "chatgpt": _chatgpt_logout}

_NO_AUTH_FLOW_MESSAGE = (
    "provider '{id}' uses {kind} auth; pass api_key directly to PUT /v1/providers/lm."
)


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
        return {"provider_id": preset.id, **logout_handler(preset, app, presets)}
    raise _error(
        400,
        error="invalid_action",
        message=f"unknown authentication action: {action}",
        recoverable=False,
    )
