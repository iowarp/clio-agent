"""Claude Code adapter for the generic ``POST /v1/providers/{id}/auth`` interface.

Same start / complete / status shape as ALCF and Codex Direct
(:mod:`clio_agent.gact.routes.provider_auth`). The sign-in itself is the Claude
Code CLI's own ``claude auth login`` (see
:mod:`clio_agent.providers.claude_code_login_flow`): ``start`` returns its
authorize URL, ``complete`` hands the pasted code to the CLI, and once the CLI
exits signed in the provider is re-verified through the same path a sign-in
made in a terminal takes (:mod:`clio_agent.gact.claude_code_auth_reprobe`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, LMProviderPreset

__all__ = ["claude_complete", "claude_start", "claude_status"]

_EXPIRED = "This sign-in attempt has ended. Start sign-in again."


def _error(status_code: int, *, error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def _wait_s() -> float:
    from clio_agent.providers.model_discovery.claude_code import (  # noqa: PLC0415
        CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S,
    )

    return CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S


async def _reverify(app: Any, preset: LMProviderPreset) -> bool:
    from clio_agent.gact.claude_code_auth_reprobe import reprobe_claude_code_auth  # noqa: PLC0415
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    provider = get_provider(preset.id)
    if provider is None:
        return False
    return await reprobe_claude_code_auth(app, provider, trigger="sign_in")


async def claude_start(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    """Start ``claude auth login`` and return its sign-in URL (paste the code back)."""

    del preset, body, app, presets
    from clio_agent.providers import claude_code_login_flow as login  # noqa: PLC0415
    from clio_agent.providers.model_discovery.claude_code import (  # noqa: PLC0415
        _resolve_claude_binary,
    )

    try:
        binary = await asyncio.to_thread(_resolve_claude_binary)
        flow = await asyncio.to_thread(login.start_login, binary, wait_s=_wait_s())
    except Exception as exc:  # noqa: BLE001 - not installed / no URL: the CLI's own words
        raise _error(502, error="claude_code_auth_failed", message=str(exc)) from exc
    return {
        "flow_id": flow.flow_id,
        "browser": {"authorization_url": flow.url, "loopback": False},
        "instructions": (
            "Open the sign-in page, approve access, then paste the code it shows here."
        ),
    }


async def claude_complete(
    preset: LMProviderPreset, body: dict[str, Any], app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    """Hand the pasted code to the CLI and report its outcome."""

    del presets
    from clio_agent.providers import claude_code_login_flow as login  # noqa: PLC0415

    flow_id = str(body.get("flow_id", ""))
    flow = login.get_flow(flow_id)
    if flow is None:
        raise _error(404, error="flow_not_found", message=_EXPIRED)
    code = str(body.get("paste") or body.get("authorization_code") or "").strip()
    if not code:
        raise _error(400, error="invalid_request", message="Paste the code the sign-in page shows.")
    try:
        await asyncio.to_thread(flow.submit_code, code)
    except login.ClaudeLoginError as exc:
        raise _error(410, error="flow_not_found", message=str(exc)) from exc
    await asyncio.to_thread(flow.exited.wait, _wait_s())
    # The finished flow stays readable: a status poll still reports this outcome
    # (the CLI's own reason) instead of "ended"; the next start replaces it.
    state, reason = flow.state()
    if state == "failed":
        raise _error(401, error="claude_code_auth_failed", message=reason)
    if state == "pending":
        return {"is_authenticated": False, "instructions": "Finishing Claude Code sign-in..."}
    verified = await _reverify(app, preset)
    return {
        "is_authenticated": verified,
        "instructions": "Signed in to Claude Code." if verified else "Checking Claude Code...",
    }


def claude_status(
    preset: LMProviderPreset, flow_id: str, app: Any, presets: list[LMProviderPreset]
) -> dict[str, Any]:
    """Poll a sign-in: pending until the person pastes the code and the CLI exits."""

    del presets
    from clio_agent.providers import claude_code_login_flow as login  # noqa: PLC0415

    flow = login.get_flow(flow_id)
    if flow is None:
        return {"state": "failed", "reason": _EXPIRED}
    state, reason = flow.state()
    if state == "pending":
        return {"state": "pending", "reason": ""}
    # A finished flow stays readable (its process has exited) until the next start.
    if state == "failed":
        return {"state": "failed", "reason": reason}
    asyncio.get_running_loop().create_task(_reverify(app, preset))
    return {"state": "complete", "reason": ""}
