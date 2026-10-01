"""Typed errors + retry/terminal classification for the Codex transport (A.7).

No silent fallback: every failure this provider can raise is one of the typed
exceptions below, carrying a machine-readable ``reason`` alongside the human
message, so the gact streaming layer can record it the same way it records
every other ``stream_fallback`` reason (``gact/streaming.py``).
"""

from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass

from clio_agent.providers.codex.constants import (
    RETRY_BASE_DELAY_MS,
    RETRY_MAX_DELAY_MS,
    RETRYABLE_STATUS_CODES,
    USAGE_LIMIT_MARKERS,
)
from clio_agent.providers.terminal_signal import (
    TerminalProviderSignal,
    find_terminal_signal,
    is_terminal_provider_error,
    recover_message,
)


class CodexError(RuntimeError):
    """Base class for every typed Codex-provider failure."""

    reason: str = "codex_error"


class CodexAuthError(CodexError):
    """The OAuth login/refresh flow failed (A.3/A.4)."""

    reason = "codex_auth_failed"


class CodexCredentialMissingError(CodexAuthError):
    """No stored credential -- the user has never signed in, or logged out."""

    reason = "codex_credential_missing"


class CodexRefreshFailedError(CodexAuthError):
    """A 401 refresh attempt failed -- the credential must be treated as invalid."""

    reason = "codex_auth_refresh_failed"


class CodexResponseError(CodexError):
    """The Codex backend rejected or failed a turn (SSE ``error``/``response.failed``).

    ``code`` is the backend's own error code (``response.error.code``) when
    one was present; ``status_code`` is the transport HTTP status when applicable.
    """

    reason = "codex_response_failed"

    def __init__(
        self, message: str, *, code: str | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


#: CLIO's own stable marker text, prepended to every ``CodexPlanLimitError``
#: message (mirroring ``CLAUDE_CODE_SIGNED_OUT_MESSAGE``, whose docstring notes
#: the SAME reason: LiteLLM re-wraps a mid-stream failure it does not
#: recognize as ``litellm.MidStreamFallbackError: litellm.APIConnectionError:
#: <original>\nTraceback (most recent call last): ...`` -- see
#: ``clio_agent.providers.claude_code_plan_limit``'s module docstring for the
#: full mechanism, BerriAI/litellm#4201). The backend's OWN words are not
#: stable (they vary by account/plan), so without this marker a plan-limit hit
#: could not be told apart from any other wrapped failure once re-wrapped --
#: :func:`contains_codex_plan_limit` / :func:`codex_plan_limit_message`
#: recover it by locating this marker, never by re-interpreting the backend's
#: prose.
CODEX_PLAN_LIMIT_MESSAGE_MARKER = "Codex account usage limit reached"


class CodexPlanLimitError(CodexResponseError):
    """A 429 whose text names the Codex plan window -- terminal, never retried."""

    reason = "codex_plan_limit"

    def __init__(
        self, message: str, *, code: str | None = None, status_code: int | None = None
    ) -> None:
        #: The Codex backend's own words (kept verbatim, unprefixed).
        self.detail = message
        super().__init__(
            f"{CODEX_PLAN_LIMIT_MESSAGE_MARKER}: {message}", code=code, status_code=status_code
        )


class CodexRetryExhaustedError(CodexError):
    """Every retry attempt was consumed, or the server asked for longer than the cap."""

    reason = "codex_retry_exhausted"


class CodexTransportError(CodexError):
    """A network/transport-level failure (connection refused, timeout, ...)."""

    reason = "codex_transport_error"


class CodexUnsupportedInputError(CodexError):
    """A message part the Direct transport cannot deliver (refused, never dropped).

    The Direct transport carries text, ``input_image`` and PDF ``input_file``
    parts; anything else in a file part (another media type, a bare
    ``file_id`` the stateless backend cannot resolve, an oversized document)
    fails the turn loudly instead of reaching the model without it.
    """

    reason = "codex_unsupported_input"


#: Shown wherever a refresh/handshake failure turns out to be an auth
#: rejection rather than a generic transport failure. No "on the connected
#: agent" -- that phrasing named the deleted local-CLI transport; the direct
#: subscription provider authenticates against openai.com, not anything
#: running on the backend host.
CODEX_AUTHENTICATION_ERROR_MESSAGE = "Codex sign-in is required"

#: The plain-language refusal for a Codex selection that still names a model
#: variant (a bind request's or a model reference's ``variant``): Codex has one
#: way to connect, so any variant is a stale client selection.
CODEX_VARIANT_REMOVED_MESSAGE = (
    "Model variants are no longer used for Codex (it always connects directly); "
    "choose the model again from the model picker."
)

_AUTH_FAILURE_MARKERS: tuple[str, ...] = (
    "401",
    "unauthorized",
    "access token rejected",
    "sign-in is required",
    "sign in again",
    "credential",
    "refresh failed",
)


def contains_codex_authentication_error(error: BaseException | str) -> bool:
    """Whether an error/failure string names an auth rejection.

    Distinguishes "the account needs to sign in again" from a generic
    network/transport failure, so a refresh probe can report ``auth:
    rejected`` instead of ``auth: deferred``.
    """

    lowered = str(error).casefold()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


def is_usage_limit_text(text: str) -> bool:
    """Whether ``text`` (a 429 response body, or an in-stream error message) names
    the account's plan window rather than a transient rate limit (A.7)."""

    lowered = (text or "").casefold()
    return any(marker in lowered for marker in USAGE_LIMIT_MARKERS)


def is_retryable_status(status_code: int, body_text: str = "") -> bool:
    """Whether a response with this status should be retried with backoff.

    A 429 is retried UNLESS its body identifies a terminal plan-limit
    condition (see :func:`is_usage_limit_text`).
    """

    if status_code == 429:
        return not is_usage_limit_text(body_text)
    return status_code in RETRYABLE_STATUS_CODES


@dataclass(frozen=True)
class RetryDecision:
    """The outcome of consulting the retry policy for one failed attempt."""

    should_retry: bool
    delay_ms: float = 0.0
    exceeded_cap: bool = False


def parse_retry_after_ms(headers: dict[str, str]) -> float | None:
    """Read ``retry-after-ms`` first, then ``retry-after`` (seconds or an HTTP-date).

    Header lookup is case-insensitive; callers may pass a dict with any
    casing (httpx.Headers already normalizes to lower-case, but a plain dict
    from a test fixture might not).
    """

    lowered = {k.casefold(): v for k, v in headers.items()}
    raw_ms = lowered.get("retry-after-ms")
    if raw_ms is not None:
        try:
            return max(0.0, float(raw_ms))
        except ValueError:
            pass
    raw_s = lowered.get("retry-after")
    if raw_s is None:
        return None
    try:
        return max(0.0, float(raw_s) * 1000.0)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw_s)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return max(0.0, (parsed.timestamp() - time.time()) * 1000.0)


def next_retry_delay_ms(
    *,
    attempt: int,
    headers: dict[str, str] | None = None,
    max_delay_ms: float = RETRY_MAX_DELAY_MS,
    base_delay_ms: float = RETRY_BASE_DELAY_MS,
) -> RetryDecision:
    """Exponential backoff honoring a server-requested delay, capped (A.7).

    ``attempt`` is 0-indexed (the first retry is ``attempt=0``). Honors
    ``retry-after-ms`` first, then ``retry-after``, before falling back to
    ``base_delay_ms * 2**attempt``. A server-requested delay exceeding
    ``max_delay_ms`` refuses the retry (``exceeded_cap=True``) rather than
    waiting an unbounded amount of time.
    """

    server_delay = parse_retry_after_ms(headers or {})
    if server_delay is not None:
        if server_delay > max_delay_ms:
            return RetryDecision(should_retry=False, delay_ms=server_delay, exceeded_cap=True)
        return RetryDecision(should_retry=True, delay_ms=server_delay)
    computed = min(base_delay_ms * (2**attempt), max_delay_ms)
    return RetryDecision(should_retry=True, delay_ms=computed)


def raise_for_backend_error(
    *, code: str | None, message: str | None, status_code: int | None = None
) -> None:
    """Raise the typed error for a backend-reported ``error``/``response.failed`` event.

    Classifies a terminal plan-limit condition even when it arrives as an
    in-stream event rather than an HTTP 429 -- the Codex backend can fail a
    turn mid-stream with the same usage-limit wording.
    """

    text = message or "Codex backend request failed"
    if is_usage_limit_text(text):
        raise CodexPlanLimitError(text, code=code, status_code=status_code)
    raise CodexResponseError(text, code=code, status_code=status_code)


def _codex_plan_limit_extra_details(node: object) -> dict[str, object]:
    if isinstance(node, CodexPlanLimitError):
        return {"code": node.code, "status_code": node.status_code}
    return {}


#: This signal's registry entry (see
#: :mod:`clio_agent.providers.terminal_signal_catalog`) -- the SAME table
#: entry :func:`contains_codex_plan_limit` / :func:`codex_plan_limit_message`
#: below use, kept as this module's own thin wrappers around the shared
#: mechanism for backward-compatible names.
CODEX_PLAN_LIMIT_SIGNAL = TerminalProviderSignal(
    reason="codex_plan_limit",
    marker=CODEX_PLAN_LIMIT_MESSAGE_MARKER,
    provider_id="codex",
    exception_type=CodexPlanLimitError,
    recovery_actions=("switch_model", "retry"),
    extra_details=_codex_plan_limit_extra_details,
)

_SIGNALS = (CODEX_PLAN_LIMIT_SIGNAL,)


def contains_codex_plan_limit(value: object) -> bool:
    """Whether an exception (or group, or message) carries a Codex plan-limit hit.

    Matches CLIO's own :data:`CODEX_PLAN_LIMIT_MESSAGE_MARKER`, never the
    backend's prose directly -- the marker text is CLIO's, so it survives
    LiteLLM re-wrapping the exception as text (see the marker's docstring).
    """
    return is_terminal_provider_error(value, _SIGNALS)


def codex_plan_limit_message(value: object) -> str | None:
    """The clean, typed one-line message for a Codex plan-limit failure, or ``None``.

    Recovers CLIO's own marker + the backend's own words even after LiteLLM
    has wrapped it and appended a full traceback, the same way
    :func:`clio_agent.providers.claude_code_plan_limit.claude_code_plan_limit_message`
    recovers its Claude Code counterpart.
    """
    found = find_terminal_signal(value, _SIGNALS)
    if found is None:
        return None
    _signal, node = found
    return recover_message(node, CODEX_PLAN_LIMIT_MESSAGE_MARKER)


__all__ = [
    "CODEX_VARIANT_REMOVED_MESSAGE",
    "CODEX_AUTHENTICATION_ERROR_MESSAGE",
    "CODEX_PLAN_LIMIT_MESSAGE_MARKER",
    "CODEX_PLAN_LIMIT_SIGNAL",
    "CodexAuthError",
    "CodexCredentialMissingError",
    "CodexError",
    "CodexPlanLimitError",
    "CodexRefreshFailedError",
    "CodexResponseError",
    "CodexRetryExhaustedError",
    "CodexTransportError",
    "RetryDecision",
    "codex_plan_limit_message",
    "contains_codex_authentication_error",
    "contains_codex_plan_limit",
    "is_retryable_status",
    "is_usage_limit_text",
    "next_retry_delay_ms",
    "parse_retry_after_ms",
    "raise_for_backend_error",
]
