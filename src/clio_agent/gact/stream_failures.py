"""Describe a failed turn for traces and users.

A refused sign-in or a missing Claude Code SDK surfaces as its stable
user-facing message; a provider HTTP error as the provider's own words; every
other failure by its real cause.
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
from clio_agent.providers.terminal_signal import find_terminal_signal, recover_message
from clio_agent.providers.terminal_signal_catalog import TERMINAL_PROVIDER_SIGNALS

#: ``details.reason`` of a ``provider_error`` whose provider refused the
#: sign-in (#1454): the client offers that provider's sign-in action.
PROVIDER_AUTH_REQUIRED_REASON = "provider_auth_required"

#: ``details.reason`` values a caller can see from
#: :data:`clio_agent.providers.terminal_signal_catalog.TERMINAL_PROVIDER_SIGNALS`
#: (kept here too, as stable public re-exports for callers that only need the
#: string, not the table) -- unconditional on the turn's configured provider,
#: see :func:`turn_failure_message`.
CLAUDE_CODE_PLAN_LIMIT_REASON = "claude_code_plan_limit"
CODEX_PLAN_LIMIT_REASON = "codex_plan_limit"
PROVIDER_SAFETY_REFUSAL_REASON = "provider_safety_refusal"


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


def _terminal_signal_error_info(exc: BaseException, details: dict[str, Any]) -> Any | None:
    """The typed ``provider_error`` for any registered terminal signal, or ``None``.

    ONE lookup against :data:`~clio_agent.providers.terminal_signal_catalog
    .TERMINAL_PROVIDER_SIGNALS` replaces a per-signal chain of
    ``if <signal>_message(exc): ...`` -- adding a new terminal signal to the
    table (a new provider module's :class:`TerminalProviderSignal`) needs no
    change here.

    Args:
        exc: The exception the turn's provider call raised (searched
            regardless of the turn's configured provider -- see
            :func:`turn_failure_message`).
        details: The caller's base ``ErrorInfo.details`` (extended, not
            replaced).

    Returns:
        The :class:`~clio_agent.gact.types.ErrorInfo`, or ``None`` when ``exc``
        carries no registered terminal signal.
    """
    found = find_terminal_signal(exc, TERMINAL_PROVIDER_SIGNALS)
    if found is None:
        return None
    signal, node = found
    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    return ErrorInfo(
        error="provider_error",
        message=recover_message(node, signal.marker),
        details={
            **details,
            "reason": signal.reason,
            "provider_id": signal.provider_id,
            "provider_label": provider_label(signal.provider_id),
            "recovery_actions": list(signal.recovery_actions),
            **signal.extra_details(node),
        },
        recoverable=True,
    )


def _provider_error_leaf(exc: BaseException) -> Any | None:
    """The first ``dspy.LMProviderError`` carrying an HTTP status in ``exc``'s tree.

    Walks exception groups and explicit causes.
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
    user needs the provider's own words, not a wrapper around them.

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

    The one formatter both failure paths share: any registered terminal
    provider signal (:data:`~clio_agent.providers.terminal_signal_catalog
    .TERMINAL_PROVIDER_SIGNALS` -- a Claude Code or Codex plan/usage-limit
    hit, a Claude Code safety-filter refusal, ...) is CLIO's own clean
    sentence recovered from the exception tree regardless of ``provider_id``
    (#1529 -- one of these can surface a turn after the session's configured
    provider has already moved on, and the user still needs to know what
    actually happened); otherwise a provider HTTP error is the provider's own
    words on one line (:func:`provider_failure_message`), labelled with the
    configured provider, or with the provider the error names when none is
    configured; any other failure keeps ``otherwise``.

    Args:
        exc: The exception the turn's provider call raised.
        provider_id: The configured provider's catalog id ("" when unknown).
        otherwise: The caller's message for a failure that is not a provider
            HTTP error.

    Returns:
        The message the failed turn's error carries.
    """
    found = find_terminal_signal(exc, TERMINAL_PROVIDER_SIGNALS)
    if found is not None:
        signal, node = found
        return recover_message(node, signal.marker)
    leaf = _provider_error_leaf(exc)
    named = provider_id or str(getattr(leaf, "provider", "") or "")
    message = provider_failure_message(exc, provider_label=provider_label(named))
    return message if message is not None else otherwise


def forward_error_info(state: Any, exc: BaseException, partial_answer: str) -> Any:
    """The typed error for a turn whose forward raised.

    Args:
        state: The turn's ``TurnState``; its accepted user message records the
            provider the turn ran on (``effective_model``).
        exc: The exception the forward raised.
        partial_answer: Answer text the turn already showed ("" when none).

    Returns:
        A ``provider_error`` for a refused sign-in, a known CLI-provider failure or
        a provider HTTP error (in the provider's own words); ``agent_error`` for
        anything else.
    """
    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    terminal = _terminal_signal_error_info(exc, {"original_error": type(exc).__name__})
    if terminal is not None:
        return terminal
    provider_id = _turn_provider_id(state)
    details: dict[str, Any] = {
        "original_error": type(exc).__name__,
        "partial_output": bool(partial_answer),
    }
    auth = provider_auth_failure(exc, provider_id=provider_id)
    if auth is not None:
        return _auth_error_info(provider_id, auth, details)
    known = cli_provider_stream_failure(exc, provider_id=provider_id)
    if known is not None:
        return ErrorInfo(error="provider_error", message=known, details=details, recoverable=True)
    provider_message = turn_failure_message(exc, provider_id=provider_id, otherwise="")
    if provider_message:
        return ErrorInfo(
            error="provider_error", message=provider_message, details=details, recoverable=True
        )
    return ErrorInfo(
        error="agent_error",
        message=f"agent.forward raised: {exc}",
        details=details,
        recoverable=True,
    )


__all__ = [
    "CLAUDE_CODE_PLAN_LIMIT_REASON",
    "CODEX_PLAN_LIMIT_REASON",
    "PROVIDER_SAFETY_REFUSAL_REASON",
    "PROVIDER_AUTH_REQUIRED_REASON",
    "provider_auth_failure",
    "cli_provider_stream_failure",
    "forward_error_info",
    "provider_failure_message",
    "provider_label",
    "turn_failure_message",
]
