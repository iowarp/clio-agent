"""GitHub Copilot sign-in: CLIO's own GitHub account, never the runtime's stored login.

The GACT server registers one token source at startup (the connected-storage
vault's GitHub account, see ``gact/routes/provider_auth_copilot.py``). The
provider reads it for the client's ``github_token`` and answers the runtime's
per-session ``github_token_provider`` callback from it, so a refreshed GitHub
token reaches a long-lived session without a restart. With no source, or a
signed-out account, every read raises :class:`CopilotAuthRequiredError`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from clio_agent.providers.copilot.errors import (
    COPILOT_SIGNED_OUT_MESSAGE,
    CopilotAuthRequiredError,
)

#: Returns ``(access_token, seconds_of_remaining_life)``; raises ``PermissionError``
#: when the GitHub account is signed out or its refresh was refused.
TokenSource = Callable[[], tuple[str, int]]

_LOCK = threading.Lock()
_SOURCE: TokenSource | None = None


def set_token_source(source: TokenSource | None) -> None:
    """Install (or clear) the GitHub credential source for this process."""
    global _SOURCE
    with _LOCK:
        _SOURCE = source


def github_credential() -> tuple[str, int]:
    """The signed-in GitHub token and its remaining lifetime in seconds."""
    with _LOCK:
        source = _SOURCE
    if source is None:
        raise CopilotAuthRequiredError(
            COPILOT_SIGNED_OUT_MESSAGE, detail="github_sign_in_unavailable"
        )
    try:
        token, expires_in = source()
    except PermissionError as exc:
        raise CopilotAuthRequiredError(COPILOT_SIGNED_OUT_MESSAGE, detail=str(exc)) from exc
    if not token:
        raise CopilotAuthRequiredError(COPILOT_SIGNED_OUT_MESSAGE, detail="github_token_empty")
    return token, max(1, int(expires_in))


def github_token() -> str:
    """The signed-in GitHub token (see :func:`github_credential`)."""
    return github_credential()[0]


def signed_in() -> bool:
    """Whether a GitHub token can be read right now (refreshing it when due)."""
    try:
        github_credential()
    except CopilotAuthRequiredError:
        return False
    return True


async def token_provider(args: Any) -> dict[str, Any]:
    """The runtime's per-session ``github_token_provider`` callback.

    A signed-out account answers ``cancelled``: the runtime then rejects the
    session instead of falling back to any ambient login.
    """
    try:
        token, expires_in = await asyncio.to_thread(github_credential)
    except CopilotAuthRequiredError:
        return {"kind": "cancelled"}
    return {"kind": "token", "accessToken": token, "expiresIn": expires_in}


__all__ = [
    "TokenSource",
    "github_credential",
    "github_token",
    "set_token_source",
    "signed_in",
    "token_provider",
]
