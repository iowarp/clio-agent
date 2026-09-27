"""Describe failed turns (streamed and non-streamed) for traces and users.

A missing Codex sign-in or a missing Claude Code SDK is not a transport
hiccup: the non-streaming retry would fail the same way, so streaming surfaces
the stable user-facing message instead of degrading. Every other failure is
described by its real (unwrapped) cause.
"""

from __future__ import annotations

import json
from typing import Any

from clio_agent.providers.claude_code_errors import (
    CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
    CLAUDE_CODE_SIGNED_OUT_MESSAGE,
    contains_claude_code_dependency_error,
    contains_claude_code_signed_out,
)
from clio_agent.providers.codex.errors import (
    CODEX_AUTHENTICATION_ERROR_MESSAGE,
    contains_codex_authentication_error,
)

# Leaf-scan order for an exception group that matched nothing at the top
# level. The top-level check already recurses for Claude Code (its detector
# walks ``.exceptions``) but not for Codex auth, so a group holding both
# kinds of leaf reports the Claude Code message.
CLI_PROVIDER_FAILURE_MESSAGES: tuple[str, ...] = (
    CODEX_AUTHENTICATION_ERROR_MESSAGE,
    CLAUDE_CODE_SIGNED_OUT_MESSAGE,
    CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
)

#: ``details.reason`` of a ``provider_error`` whose provider refused the
#: sign-in (#1454): the client offers that provider's sign-in action.
PROVIDER_AUTH_REQUIRED_REASON = "provider_auth_required"


def cli_provider_stream_failure(exc: BaseException, *, provider_id: str) -> str | None:
    """Return the user-facing message for a known CLI-provider failure.

    Only the CLI provider that is actually configured can own one of these
    messages: the Codex auth detector matches any text naming "401" or
    "unauthorized", so unscoped it reported an OpenRouter 401 as "Codex
    sign-in is required" (rel18).

    Args:
        exc: The exception raised by a streamed provider call.
        provider_id: The configured provider's catalog id.

    Returns:
        The stable message when the configured provider is Codex and ``exc``
        is its authentication failure, or Claude Code and ``exc`` is its
        missing dependency; otherwise ``None``.
    """
    from clio_agent.gact.providers.config import _provider_runtime_kind  # noqa: PLC0415

    auth = provider_auth_failure(exc, provider_id=provider_id)
    if auth is not None:
        return auth
    kind = _provider_runtime_kind(provider_id)
    if kind == "claude_code" and contains_claude_code_dependency_error(exc):
        return CLAUDE_CODE_INSTALL_FAILED_MESSAGE
    return None


def provider_auth_failure(exc: BaseException, *, provider_id: str) -> str | None:
    """The one-line message when the configured provider refused the sign-in.

    Scoped to the configured provider's kind, like
    :func:`cli_provider_stream_failure`: the Codex detector matches any "401"
    text, so it only applies when Codex is the provider that ran.

    Args:
        exc: The exception the turn's provider call raised.
        provider_id: The configured provider's catalog id ("" when unknown).

    Returns:
        The provider's sign-in message, or ``None`` when ``exc`` is not a
        refused sign-in of that provider.
    """
    from clio_agent.gact.providers.config import _provider_runtime_kind  # noqa: PLC0415

    kind = _provider_runtime_kind(provider_id) if provider_id else ""
    if kind == "codex" and contains_codex_authentication_error(exc):
        return CODEX_AUTHENTICATION_ERROR_MESSAGE
    if kind == "claude_code" and contains_claude_code_signed_out(exc):
        return CLAUDE_CODE_SIGNED_OUT_MESSAGE
    return None


def _turn_provider_id(state: Any) -> str:
    """The provider the turn ran on, as its accepted user message recorded it."""
    metadata = getattr(getattr(state, "user_msg", None), "metadata", None)
    model = metadata.get("effective_model") if isinstance(metadata, dict) else None
    return str(model.get("provider_id") or "") if isinstance(model, dict) else ""


def _auth_error_info(provider_id: str, message: str, details: dict[str, Any]) -> Any:
    """The typed ``provider_error`` for a refused sign-in: one line + the provider."""
    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    return ErrorInfo(
        error="provider_error",
        message=message,
        details={
            **details,
            "reason": PROVIDER_AUTH_REQUIRED_REASON,
            "provider_id": provider_id,
            "provider_label": provider_label(provider_id),
            "recovery_actions": ["provider_sign_in", "retry"],
        },
        recoverable=True,
    )


def streamed_turn_error_info(state: Any, exc: BaseException, partial_answer: str) -> Any:
    """The typed error for a turn whose streamed provider call failed.

    Args:
        state: The turn's ``TurnState`` (its user message names the provider).
        exc: The ``_StreamingOutputError`` the streaming pump raised; its
            ``__cause__`` is the provider's own exception.
        partial_answer: Text the stream already showed ("" when none).

    Returns:
        The :class:`~clio_agent.gact.types.ErrorInfo` for the failed turn.
    """
    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    original = exc.__cause__ or exc
    details = {
        "original_error": type(original).__name__,
        "partial_output": bool(partial_answer),
        "stream_source": ("live" if partial_answer else "batch"),
    }
    provider_id = _turn_provider_id(state)
    auth = provider_auth_failure(original, provider_id=provider_id)
    if auth is not None:
        return _auth_error_info(provider_id, auth, details)
    return ErrorInfo(error="provider_error", message=str(exc), details=details, recoverable=True)


def _provider_error_leaf(exc: BaseException) -> Any | None:
    """The first ``dspy.LMProviderError`` carrying an HTTP status in ``exc``'s tree.

    Walks exception groups (``streamify``'s task group) and explicit causes.
    """
    from dspy.utils.exceptions import LMProviderError  # noqa: PLC0415

    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, LMProviderError) and getattr(current, "status", None):
            return current
        pending.extend(getattr(current, "exceptions", None) or ())
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


def _provider_own_words(message: str) -> str:
    """The provider's own error text inside LiteLLM's wrapped message.

    LiteLLM formats a provider HTTP error as ``litellm.<Class>: <Class>:
    <Provider>Exception - <body>``. When ``<body>`` is the OpenAI-style JSON
    error envelope (``{"error": {"message": ...}}``) its ``message`` is the
    provider's own words; otherwise the text after LiteLLM's wrapper prefix
    is. Formatting only -- the text itself is never reinterpreted.
    """
    start = message.find("{")
    if start != -1:
        try:
            body, _end = json.JSONDecoder().raw_decode(message[start:])
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        text = error.get("message") if isinstance(error, dict) else None
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())
    _wrapper, sep, tail = message.partition("Exception - ")
    return " ".join((tail if sep else message).split())


def provider_label(provider_id: str) -> str:
    """The catalog display name for ``provider_id`` (the id itself when uncatalogued)."""
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    preset = get_provider(provider_id)
    return preset.label if preset is not None else provider_id


def provider_failure_message(exc: BaseException, *, provider_label: str) -> str | None:
    """One plain user-facing line for a provider's HTTP error response.

    A provider that answered with an HTTP error (404 no endpoint, 429 rate
    limit, 402 billing, ...) is the real, final answer for this request: the
    user needs the provider's own words, not the streaming wrapper
    ("live streaming failed before emitting output: ExceptionGroup[...]").
    The full unwrapped detail still goes to the trace via
    :func:`describe_stream_exc`.

    Args:
        exc: The exception raised by a streamed provider call.
        provider_label: The configured provider's display name.

    Returns:
        ``"<provider>: <provider's message> (HTTP <status>)"``, or ``None``
        when no provider HTTP error is in ``exc``'s tree.
    """
    leaf = _provider_error_leaf(exc)
    if leaf is None:
        return None
    text = _provider_own_words(str(getattr(leaf, "message", "") or leaf))
    return f"{provider_label}: {text} (HTTP {leaf.status})"


def turn_failure_message(exc: BaseException, provider_id: str, otherwise: str) -> str:
    """The user-facing message for a failed turn, streamed or not.

    The one formatter both failure paths share: a provider HTTP error is the
    provider's own words on one line (:func:`provider_failure_message`),
    labelled with the configured provider, or with the provider the error
    names when none is configured; any other failure keeps ``otherwise``.

    Args:
        exc: The exception the turn's provider call raised.
        provider_id: The configured provider's catalog id ("" when unknown).
        otherwise: The caller's message for a failure that is not a provider
            HTTP error.

    Returns:
        The message the failed turn's error carries.
    """
    leaf = _provider_error_leaf(exc)
    named = provider_id or str(getattr(leaf, "provider", "") or "")
    message = provider_failure_message(exc, provider_label=provider_label(named))
    return message if message is not None else otherwise


def agent_forward_error_info(state: Any, exc: BaseException) -> Any:
    """The typed ``agent_error`` for a non-streamed forward that raised.

    Args:
        state: The turn's ``TurnState``; its accepted user message records
            the provider the turn ran on (``effective_model``).
        exc: The exception the forward raised.

    Returns:
        The :class:`~clio_agent.gact.types.ErrorInfo` for the failed turn.
    """
    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    provider_id = _turn_provider_id(state)
    auth = provider_auth_failure(exc, provider_id=provider_id)
    if auth is not None:
        return _auth_error_info(provider_id, auth, {"original_error": type(exc).__name__})
    return ErrorInfo(
        error="agent_error",
        message=turn_failure_message(
            exc, provider_id=provider_id, otherwise=f"agent.forward raised: {exc}"
        ),
        details={"original_error": type(exc).__name__},
        recoverable=True,
    )


def describe_stream_exc(exc: BaseException, *, provider_id: str) -> str:
    """Format a streaming exception for logging, UNWRAPPING ``ExceptionGroup``.

    ``streamify`` runs the agent forward inside an anyio task group, so a failure
    surfaces as ``ExceptionGroup`` whose ``str()`` is only the opaque wrapper
    ("unhandled errors in a TaskGroup (1 sub-exception)"); the real cause lives
    in ``.exceptions``. Recurse into the leaves so the captured detail names the
    actual provider/transport error instead of the wrapper. A known CLI-provider
    failure anywhere in the tree is returned as its stable user-facing message.

    Args:
        exc: The exception raised by a streamed provider call.
        provider_id: The configured provider's catalog id (scopes the
            CLI-provider messages, see :func:`cli_provider_stream_failure`).

    Returns:
        The user-facing CLI-provider message, or ``Type: detail`` (groups as
        ``Group[leaf; leaf]``).
    """
    known = cli_provider_stream_failure(exc, provider_id=provider_id)
    if known is not None:
        return known
    group = getattr(exc, "exceptions", None)
    if group:
        leaves = [describe_stream_exc(sub, provider_id=provider_id) for sub in group]
        for message in CLI_PROVIDER_FAILURE_MESSAGES:
            if message in leaves:
                return message
        return f"{type(exc).__name__}[{'; '.join(leaves)}]"
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "CLI_PROVIDER_FAILURE_MESSAGES",
    "PROVIDER_AUTH_REQUIRED_REASON",
    "provider_auth_failure",
    "streamed_turn_error_info",
    "cli_provider_stream_failure",
    "describe_stream_exc",
    "agent_forward_error_info",
    "provider_failure_message",
    "provider_label",
    "turn_failure_message",
]
