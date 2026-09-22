"""Describe streamed-forward failures for traces and user-facing errors.

A missing Codex sign-in or a missing Claude Code SDK is not a transport
hiccup: the non-streaming retry would fail the same way, so streaming surfaces
the stable user-facing message instead of degrading. Every other failure is
described by its real (unwrapped) cause.
"""

from __future__ import annotations

from clio_agent.providers.claude_code_errors import (
    CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
    contains_claude_code_dependency_error,
)
from clio_agent.providers.codex_errors import (
    CODEX_AUTHENTICATION_ERROR_MESSAGE,
    contains_codex_authentication_error,
)

# Priority order: when an exception group carries both, Codex auth wins.
CLI_PROVIDER_FAILURE_MESSAGES: tuple[str, ...] = (
    CODEX_AUTHENTICATION_ERROR_MESSAGE,
    CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
)


def cli_provider_stream_failure(exc: BaseException) -> str | None:
    """Return the user-facing message for a known CLI-provider failure.

    Args:
        exc: The exception raised by a streamed provider call.

    Returns:
        The stable message when ``exc`` is a Codex authentication failure or a
        missing Claude Code dependency, otherwise ``None``.
    """
    if contains_codex_authentication_error(exc):
        return CODEX_AUTHENTICATION_ERROR_MESSAGE
    if contains_claude_code_dependency_error(exc):
        return CLAUDE_CODE_INSTALL_FAILED_MESSAGE
    return None


def describe_stream_exc(exc: BaseException) -> str:
    """Format a streaming exception for logging, UNWRAPPING ``ExceptionGroup``.

    ``streamify`` runs the agent forward inside an anyio task group, so a failure
    surfaces as ``ExceptionGroup`` whose ``str()`` is only the opaque wrapper
    ("unhandled errors in a TaskGroup (1 sub-exception)"); the real cause lives
    in ``.exceptions``. Recurse into the leaves so the captured detail names the
    actual provider/transport error instead of the wrapper. A known CLI-provider
    failure anywhere in the tree is returned as its stable user-facing message.

    Args:
        exc: The exception raised by a streamed provider call.

    Returns:
        The user-facing CLI-provider message, or ``Type: detail`` (groups as
        ``Group[leaf; leaf]``).
    """
    known = cli_provider_stream_failure(exc)
    if known is not None:
        return known
    group = getattr(exc, "exceptions", None)
    if group:
        leaves = [describe_stream_exc(sub) for sub in group]
        for message in CLI_PROVIDER_FAILURE_MESSAGES:
            if message in leaves:
                return message
        return f"{type(exc).__name__}[{'; '.join(leaves)}]"
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "CLI_PROVIDER_FAILURE_MESSAGES",
    "cli_provider_stream_failure",
    "describe_stream_exc",
]
