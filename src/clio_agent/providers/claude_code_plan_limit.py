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

__all__ = [
    "PLAN_LIMIT_HTTP_STATUS",
    "PLAN_LIMIT_MESSAGE_MARKER",
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

#: LiteLLM appends the full traceback after this literal line when it cannot
#: map an exception type (see the marker's docstring above); the clean
#: sentence :func:`claude_code_plan_limit_message` recovers ends right before it.
_TRACEBACK_MARKER = "Traceback (most recent call last):"

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


def _exception_tree(value: Any) -> list[Any]:
    """``value`` and every node reachable from it via ``.exceptions``/``__cause__``.

    The same shape ``contains_claude_code_signed_out`` walks in
    ``claude_code_errors.py``, generalized to a flat list so both
    :func:`find_claude_code_plan_limit` and the text search in
    :func:`contains_claude_code_plan_limit` / :func:`claude_code_plan_limit_message`
    share one traversal: an ``ExceptionGroup``'s own ``str()`` is just the
    opaque "unhandled errors in a TaskGroup (N sub-exceptions)" wrapper (the
    real text lives on its ``.exceptions`` leaves), so a text search that
    only looked at ``str(value)`` would miss a marker one level down.
    """
    seen: set[int] = set()
    pending = [value]
    nodes: list[Any] = []
    while pending:
        node = pending.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        nodes.append(node)
        pending.extend(getattr(node, "exceptions", None) or ())
        cause = getattr(node, "__cause__", None)
        if cause is not None:
            pending.append(cause)
    return nodes


def find_claude_code_plan_limit(value: Any) -> ClaudeCodePlanLimitError | None:
    """The first :class:`ClaudeCodePlanLimitError` in ``value``'s tree, if any.

    Walks exception groups and explicit causes so a direct (unwrapped)
    plan-limit error is found without relying on its text having survived
    re-wrapping, recovering its structured fields (``rate_limit_type``,
    ``resets_at``, ``model``) when the object itself is still reachable
    (never guaranteed once LiteLLM has re-wrapped it -- callers needing the
    fields only should fall back to ``None`` gracefully).
    """
    for node in _exception_tree(value):
        if isinstance(node, ClaudeCodePlanLimitError):
            return node
    return None


def contains_claude_code_plan_limit(value: Any) -> bool:
    """Whether an exception (or group, or message) carries a Claude plan-limit hit.

    Matches CLIO's own :data:`PLAN_LIMIT_MESSAGE_MARKER`, never the provider's
    or LiteLLM's prose -- the marker text is CLIO's, so it survives LiteLLM
    re-wrapping the exception as text (see the marker's docstring).
    """
    return any(
        isinstance(node, ClaudeCodePlanLimitError) or PLAN_LIMIT_MESSAGE_MARKER in str(node)
        for node in _exception_tree(value)
    )


def claude_code_plan_limit_message(value: Any) -> str | None:
    """The clean, typed one-line message for a plan-limit failure, or ``None``.

    Recovers CLIO's own sentence (built by :func:`_plan_limit_message`) even
    after LiteLLM has wrapped it in ``MidStreamFallbackError``/
    ``APIConnectionError`` and appended a full traceback: that wrapping always
    keeps the original exception's ``str()`` as a literal substring (LiteLLM's
    own documented behavior for an exception type it does not recognize,
    BerriAI/litellm#4201), so slicing from :data:`PLAN_LIMIT_MESSAGE_MARKER` to
    the next :data:`_TRACEBACK_MARKER` (or the end of the text) recovers
    exactly the sentence this module raised -- never a re-interpretation of
    LiteLLM's or the provider's own words.

    Args:
        value: The exception (or exception group) a failed turn raised.

    Returns:
        The clean sentence, or ``None`` when no plan-limit signal is present.
    """
    for node in _exception_tree(value):
        if isinstance(node, ClaudeCodePlanLimitError):
            return str(node)
        text = str(node)
        start = text.find(PLAN_LIMIT_MESSAGE_MARKER)
        if start != -1:
            end = text.find(_TRACEBACK_MARKER, start)
            return text[start : end if end != -1 else len(text)].strip()
    return None


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
