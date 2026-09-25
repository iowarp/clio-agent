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

from typing import Any

__all__ = [
    "PLAN_LIMIT_HTTP_STATUS",
    "ClaudeCodePlanLimitError",
    "plan_limit_from_rate_limit_event",
    "plan_limit_from_result",
]

#: The HTTP status the CLI reports on a ``ResultMessage`` whose turn failed
#: because the account's usage/rate window was exhausted.
PLAN_LIMIT_HTTP_STATUS = 429


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
    ) -> None:
        super().__init__(message)
        #: One of ``claude_agent_sdk.types.RateLimitType`` (e.g. ``"five_hour"``,
        #: ``"seven_day"``), or ``None`` when the signal was the 429 result path
        #: (which carries no rate-limit-type field).
        self.rate_limit_type = rate_limit_type
        #: Unix timestamp the window resets at, or ``None`` when unknown.
        self.resets_at = resets_at


def plan_limit_from_rate_limit_event(event: Any) -> ClaudeCodePlanLimitError | None:
    """Classify an SDK ``RateLimitEvent`` -- ``None`` unless its status is ``"rejected"``.

    Args:
        event: A ``claude_agent_sdk.types.RateLimitEvent`` (duck-typed here so
            hermetic tests can pass a fake without importing the real SDK).

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
        f"Claude subscription usage limit reached{detail}. "
        "Wait for the window to reset, or switch to a direct API key provider.",
        rate_limit_type=rate_limit_type,
        resets_at=resets_at,
    )


def plan_limit_from_result(result_message: Any) -> ClaudeCodePlanLimitError | None:
    """Classify a terminal ``ResultMessage`` -- ``None`` unless it is a 429.

    Args:
        result_message: A ``claude_agent_sdk.types.ResultMessage`` (duck-typed).

    Returns:
        A typed :class:`ClaudeCodePlanLimitError` when ``is_error`` and
        ``api_error_status == 429``, else ``None``.
    """
    if not getattr(result_message, "is_error", False):
        return None
    if getattr(result_message, "api_error_status", None) != PLAN_LIMIT_HTTP_STATUS:
        return None
    text = str(getattr(result_message, "result", "") or "").strip()
    detail = f": {text}" if text else ""
    return ClaudeCodePlanLimitError(
        f"Claude subscription usage limit reached (HTTP {PLAN_LIMIT_HTTP_STATUS}){detail}. "
        "Wait for the window to reset, or switch to a direct API key provider."
    )
