"""Stable, user-facing Claude Code dependency errors."""

from __future__ import annotations

from typing import Any

CLAUDE_CODE_NOT_INSTALLED_MESSAGE = "Claude Code support is not installed on the connected agent."
CLAUDE_CODE_INSTALL_FAILED_MESSAGE = (
    "CLIO could not install Claude Code support. Check the connected agent's "
    "internet connection and try again."
)


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
    "contains_claude_code_dependency_error",
]
