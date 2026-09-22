"""User-facing error normalization for the Codex provider."""

from __future__ import annotations

CODEX_AUTHENTICATION_ERROR_MESSAGE = (
    "Codex is unavailable because authentication credentials are missing. "
    "Sign in to Codex and try again."
)


def normalize_codex_error_message(message: str) -> str:
    """Replace a missing-authentication response with an actionable message."""
    normalized = message.casefold()
    if (
        "401" in normalized
        and "unauthorized" in normalized
        and ("missing bearer" in normalized or "missing authentication" in normalized)
    ):
        return CODEX_AUTHENTICATION_ERROR_MESSAGE
    return message


def contains_codex_authentication_error(error: BaseException | str) -> bool:
    """Return whether an error or wrapper contains the Codex auth failure."""
    message = str(error)
    normalized = message.casefold()
    if CODEX_AUTHENTICATION_ERROR_MESSAGE.casefold() in normalized:
        return True
    identifies_codex = any(
        marker in normalized for marker in ("codex", "cdx-", "api.openai.com/v1/responses")
    )
    return identifies_codex and normalize_codex_error_message(message) == (
        CODEX_AUTHENTICATION_ERROR_MESSAGE
    )


__all__ = [
    "CODEX_AUTHENTICATION_ERROR_MESSAGE",
    "contains_codex_authentication_error",
    "normalize_codex_error_message",
]
