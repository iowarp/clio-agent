"""Stable, user-facing Claude Code dependency errors."""

from __future__ import annotations

from typing import Any

CLAUDE_CODE_NOT_INSTALLED_MESSAGE = "Claude Code support is not installed on the connected agent."
CLAUDE_CODE_INSTALL_FAILED_MESSAGE = (
    "CLIO could not install Claude Code support. Check the connected agent's "
    "internet connection and try again."
)
#: The one line a signed-out Claude subscription shows the user (#1454). A
#: sign-in on another machine or network can end this one, so the line says so.
CLAUDE_CODE_SIGNED_OUT_MESSAGE = (
    "Claude Code is signed out. Sign in to Claude Code again, then resend. "
    "Signing in on another machine can end this session."
)


class ClaudeCodeSignedOutError(RuntimeError):
    """The Claude subscription refused the credential (signed out, expired, revoked).

    Classified from the SDK's typed ``AssistantMessage.error`` /
    ``ResultMessage.api_error_status`` (see ``claude_code_result_errors``).
    Terminal: retrying cannot succeed until the user signs in again, so the LM
    retry layer never re-issues it (``lm.io_logging``). ``str()`` always
    starts with :data:`CLAUDE_CODE_SIGNED_OUT_MESSAGE`, CLIO's own constant, so
    the classification survives LiteLLM re-wrapping the exception as text.
    """

    def __init__(self, *, detail: str, model: str) -> None:
        super().__init__(f"{CLAUDE_CODE_SIGNED_OUT_MESSAGE} (model={model}: {detail})")
        #: The CLI's own words (for example "Not logged in, please run /login").
        self.detail = detail
        self.model = model


def contains_claude_code_signed_out(value: Any) -> bool:
    """Whether an exception (or group, or message) carries a Claude sign-out.

    Matches CLIO's own :data:`CLAUDE_CODE_SIGNED_OUT_MESSAGE` constant, never
    the provider's prose, walking exception groups and explicit causes.
    """

    if isinstance(value, ClaudeCodeSignedOutError) or CLAUDE_CODE_SIGNED_OUT_MESSAGE in str(value):
        return True
    children = list(getattr(value, "exceptions", None) or ())
    cause = getattr(value, "__cause__", None)
    if cause is not None:
        children.append(cause)
    return any(contains_claude_code_signed_out(child) for child in children)


def contains_claude_code_dependency_error(value: Any) -> bool:
    """Return whether an exception or message represents a missing Claude SDK."""

    text = str(value).lower()
    markers = (
        "no module named 'claude_agent_sdk'",
        'no module named "claude_agent_sdk"',
        "requires the claude-agent-sdk package",
        "claude-agent-sdk",
        "claude code support is not installed",
        "could not install claude code support",
        "claude code support installation failed",
    )
    if any(marker in text for marker in markers):
        return True
    return any(
        contains_claude_code_dependency_error(child)
        for child in (getattr(value, "exceptions", None) or ())
    )


__all__ = [
    "CLAUDE_CODE_INSTALL_FAILED_MESSAGE",
    "CLAUDE_CODE_NOT_INSTALLED_MESSAGE",
    "CLAUDE_CODE_SIGNED_OUT_MESSAGE",
    "ClaudeCodeSignedOutError",
    "contains_claude_code_dependency_error",
    "contains_claude_code_signed_out",
]
