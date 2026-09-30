"""Typed plan/usage-limit classification for the ``claude_code`` SDK transport (B17).

The Agent SDK exposes two STRUCTURED signals for a Claude subscription plan or
usage-window limit -- never prose the caller would have to keyword-match:

* ``RateLimitEvent.rate_limit_info.status == "rejected"``: the CLI emits this
  message whenever the account's rate-limit status transitions; ``"rejected"``
  means the current window's limit has been hit (see
  ``claude_agent_sdk.types.RateLimitInfo`` -- ``status``, ``rate_limit_type``,
  ``resets_at``, ``utilization``).
* ``ResultMessage.is_error and ResultMessage.api_error_status == 429``: the
  terminal result of a turn that failed because the account's usage/rate
  window was exhausted.

Both are checked here, never a substring search over ``ResultMessage.result``
prose (#775 -- "find the SDK's structured error fields ... never prose keyword
matching where a structured field exists").
"""

from __future__ import annotations

import re
from typing import Any

from clio_agent.providers.terminal_signal import (
    TerminalProviderSignal,
    find_terminal_signal,
    is_terminal_provider_error,
    recover_message,
)

__all__ = [
    "PLAN_LIMIT_HTTP_STATUS",
    "PLAN_LIMIT_MESSAGE_MARKER",
    "PLAN_LIMIT_SIGNAL",
    "ClaudeCodePlanLimitError",
    "claude_code_plan_limit_message",
    "contains_claude_code_plan_limit",
    "find_claude_code_plan_limit",
    "plan_limit_message_model",
    "plan_limit_from_rate_limit_event",
    "plan_limit_from_result",
]

#: The HTTP status the CLI reports on a ``ResultMessage`` whose turn failed
#: because the account's usage/rate window was exhausted.
PLAN_LIMIT_HTTP_STATUS = 429

#: CLIO's own stable marker text, the start of every message this module
#: raises. LiteLLM re-wraps a mid-stream failure as
#: ``litellm.MidStreamFallbackError: litellm.APIConnectionError: <original>\n
#: Traceback (most recent call last): ...`` (its own documented behavior for
#: an exception type it does not recognize, BerriAI/litellm#4201) -- CLIO
#: cannot change that third-party wrapping. This marker is CLIO's, so it
#: survives verbatim inside the wrapped text exactly the way
#: ``CLAUDE_CODE_SIGNED_OUT_MESSAGE`` survives in ``claude_code_errors.py``:
#: :func:`contains_claude_code_plan_limit` / :func:`claude_code_plan_limit_message`
#: recover the clean, typed sentence from the mangled text by locating this
#: marker, never by re-interpreting the provider's/LiteLLM's own prose.
PLAN_LIMIT_MESSAGE_MARKER = "Claude subscription usage limit reached"

#: Recovers the ``model`` :func:`_plan_limit_message` embedded, from the clean
#: sentence alone -- the SAME stable format (`` for <model>. Wait for the
#: window...``) used by :func:`plan_limit_message_model` when the original
#: :class:`ClaudeCodePlanLimitError` object itself is no longer reachable
#: (LiteLLM's re-wrap does not guarantee it, see :func:`find_claude_code_plan_limit`).
_MODEL_SUFFIX_RE = re.compile(r" for (?P<model>.+?)\. Wait for the window to reset")


class ClaudeCodePlanLimitError(RuntimeError):
    """The Claude subscription's plan/usage limit has been hit for this window.

    Deliberately NOT one of the LM retry layer's transient markers
    (``lm.io_logging._TRANSIENT_PROVIDER_MARKERS``): retrying immediately
    cannot succeed while the window is exhausted, so this must surface as a
    clear, terminal, user-facing message rather than being silently retried.
    """

    def __init__(
        self,
        message: str,
        *,
        rate_limit_type: str | None = None,
        resets_at: int | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        #: One of ``claude_agent_sdk.types.RateLimitType`` (e.g. ``"five_hour"``,
        #: ``"seven_day"``), or ``None`` when the signal was the 429 result path
        #: (which carries no rate-limit-type field).
        self.rate_limit_type = rate_limit_type
        #: Unix timestamp the window resets at, or ``None`` when unknown.
        self.resets_at = resets_at
        #: The claude_code model id the failing call used (#1529: so the
        #: user-facing message can name which model hit the limit), or
        #: ``None`` when the caller did not have one in hand.
        self.model = model


def _plan_limit_message(*, detail: str, model: str | None) -> str:
    """The one stable sentence every plan-limit error raises (see the marker docstring)."""
    named = f" for {model}" if model else ""
    return (
        f"{PLAN_LIMIT_MESSAGE_MARKER}{detail}{named}. "
        "Wait for the window to reset, or switch this session to another model/provider."
    )


def plan_limit_from_rate_limit_event(
    event: Any, *, model: str | None = None
) -> ClaudeCodePlanLimitError | None:
    """Classify an SDK ``RateLimitEvent`` -- ``None`` unless its status is ``"rejected"``.

    Args:
        event: A ``claude_agent_sdk.types.RateLimitEvent`` (duck-typed here so
            hermetic tests can pass a fake without importing the real SDK).
        model: The claude_code model id the call used, when the caller has it
            (#1529: threaded into the message so a user who has since
            switched providers can tell which model actually hit the limit).

    Returns:
        A typed :class:`ClaudeCodePlanLimitError` when the account's window is
        exhausted, else ``None`` (an ``"allowed"``/``"allowed_warning"`` status
        is not a limit hit -- CLIO does not yet surface the warning tier).
    """
    info = getattr(event, "rate_limit_info", None)
    if info is None or getattr(info, "status", None) != "rejected":
        return None
    rate_limit_type = getattr(info, "rate_limit_type", None)
    resets_at = getattr(info, "resets_at", None)
    detail = f" ({rate_limit_type})" if rate_limit_type else ""
    return ClaudeCodePlanLimitError(
        _plan_limit_message(detail=detail, model=model),
        rate_limit_type=rate_limit_type,
        resets_at=resets_at,
        model=model,
    )


def plan_limit_from_result(
    result_message: Any, *, model: str | None = None
) -> ClaudeCodePlanLimitError | None:
    """Classify a terminal ``ResultMessage`` -- ``None`` unless it is a 429.

    Args:
        result_message: A ``claude_agent_sdk.types.ResultMessage`` (duck-typed).
        model: The claude_code model id the call used, when the caller has it
            (#1529, see :func:`plan_limit_from_rate_limit_event`).

    Returns:
        A typed :class:`ClaudeCodePlanLimitError` when ``is_error`` and
        ``api_error_status == 429``, else ``None``.
    """
    if not getattr(result_message, "is_error", False):
        return None
    if getattr(result_message, "api_error_status", None) != PLAN_LIMIT_HTTP_STATUS:
        return None
    text = str(getattr(result_message, "result", "") or "").strip()
    detail = f" (HTTP {PLAN_LIMIT_HTTP_STATUS}{f': {text}' if text else ''})"
    return ClaudeCodePlanLimitError(
        _plan_limit_message(detail=detail, model=model),
        model=model,
    )


def _plan_limit_extra_details(node: Any) -> dict[str, Any]:
    """``model``/``rate_limit_type``/``resets_at`` for the registry's ``extra_details``.

    Full fidelity when ``node`` is the live :class:`ClaudeCodePlanLimitError`
    (not guaranteed once LiteLLM has re-wrapped it); ``model`` alone,
    best-effort parsed from the recovered clean sentence, otherwise (see
    :func:`plan_limit_message_model`).
    """
    if isinstance(node, ClaudeCodePlanLimitError):
        return {
            "model": node.model,
            "rate_limit_type": node.rate_limit_type,
            "resets_at": node.resets_at,
        }
    return {"model": plan_limit_message_model(node), "rate_limit_type": None, "resets_at": None}


#: This signal's registry entry (see
#: :mod:`clio_agent.providers.terminal_signal_catalog`) -- the SAME table
#: entry :func:`find_claude_code_plan_limit` / :func:`contains_claude_code_plan_limit`
#: / :func:`claude_code_plan_limit_message` below use, kept as this module's
#: own thin wrappers around the shared mechanism for backward-compatible names.
PLAN_LIMIT_SIGNAL = TerminalProviderSignal(
    reason="claude_code_plan_limit",
    marker=PLAN_LIMIT_MESSAGE_MARKER,
    provider_id="claude_code",
    exception_type=ClaudeCodePlanLimitError,
    recovery_actions=("switch_model", "retry"),
    extra_details=_plan_limit_extra_details,
)

_SIGNALS = (PLAN_LIMIT_SIGNAL,)


def find_claude_code_plan_limit(value: Any) -> ClaudeCodePlanLimitError | None:
    """The live :class:`ClaudeCodePlanLimitError` in ``value``'s tree, if reachable.

    Thin wrapper over :func:`~clio_agent.providers.terminal_signal.find_terminal_signal`
    that only returns the typed object (never a wrapped node whose text merely
    carried the marker) so existing callers keep the strict return type.
    """
    found = find_terminal_signal(value, _SIGNALS)
    if found is None:
        return None
    _signal, node = found
    return node if isinstance(node, ClaudeCodePlanLimitError) else None


def contains_claude_code_plan_limit(value: Any) -> bool:
    """Whether an exception (or group, or message) carries a Claude plan-limit hit.

    Matches CLIO's own :data:`PLAN_LIMIT_MESSAGE_MARKER`, never the provider's
    or LiteLLM's prose -- the marker text is CLIO's, so it survives LiteLLM
    re-wrapping the exception as text (see the marker's docstring).
    """
    return is_terminal_provider_error(value, _SIGNALS)


def claude_code_plan_limit_message(value: Any) -> str | None:
    """The clean, typed one-line message for a plan-limit failure, or ``None``.

    Recovers CLIO's own sentence (built by :func:`_plan_limit_message`) even
    after LiteLLM has wrapped it in ``MidStreamFallbackError``/
    ``APIConnectionError`` and appended a full traceback (see
    :func:`~clio_agent.providers.terminal_signal.recover_message`).

    Args:
        value: The exception (or exception group) a failed turn raised.

    Returns:
        The clean sentence, or ``None`` when no plan-limit signal is present.
    """
    found = find_terminal_signal(value, _SIGNALS)
    if found is None:
        return None
    _signal, node = found
    return recover_message(node, PLAN_LIMIT_MESSAGE_MARKER)


def plan_limit_message_model(value: Any) -> str | None:
    """The claude_code model a plan-limit failure's message names, or ``None``.

    Args:
        value: The exception (or exception group) a failed turn raised, or a
            plan-limit message already recovered by
            :func:`claude_code_plan_limit_message`.

    Returns:
        The model id (#1529: so a client can show "Claude Sonnet 5
        (subscription)" without the caller having recovered the original
        :class:`ClaudeCodePlanLimitError` object -- LiteLLM's re-wrap does not
        guarantee that object stays reachable, but this module's own message
        format does survive the re-wrap, see
        :func:`claude_code_plan_limit_message`), or ``None`` when the message
        carries no model (the caller had none in hand when it raised).
    """
    leaf = find_claude_code_plan_limit(value)
    if leaf is not None and leaf.model:
        return leaf.model
    message = value if isinstance(value, str) else claude_code_plan_limit_message(value)
    if message is None:
        return None
    match = _MODEL_SUFFIX_RE.search(message)
    return match.group("model") if match else None
