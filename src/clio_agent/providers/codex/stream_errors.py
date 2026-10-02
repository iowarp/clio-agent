"""Codex direct: classify an in-stream terminal error frame the way the Codex CLI does.

The Codex backend fails a turn inside the stream -- an ``error`` frame (a WebSocket
error, optionally carrying the HTTP ``status`` it stands for) or ``response.failed``
(with ``response.error``) -- and only some of those failures are final. The Codex CLI
retries every such failure with backoff UNLESS its code names a known non-retryable
condition (context window, quota, usage not included, invalid prompt, sign-in); an
overload (``server_is_overloaded``, ``slow_down``) and any code it does not know are
retried.

lm15 maps only the codes it knows (``server_error`` -> ``ServerError``,
``context_length_exceeded`` -> ``ContextLengthError``, ...); every other code becomes
its generic ``ProviderError``, which DSPy never retries -- so a momentary overload
killed the user's turn. And lm15 parses no ``response.failed`` frame at all.

:func:`terminal_error` keeps lm15's classification wherever lm15 classifies the code,
and types a frame lm15 leaves unclassified as :class:`CodexTransientStreamError` (an
lm15 ``ServerError``, which DSPy's own retry policy retries with its own count and
backoff) unless the Codex CLI treats it as final. The classification keys off the
provider's code and status, never off the message prose.

A WebSocket that closes before the response completed is a
:class:`CodexStreamDroppedError` (the Codex CLI re-sends a dropped stream too); DSPy
retries it the same way, except after output the caller already saw.

:class:`RetryLog` makes each retry loud: one warning per failed attempt (reason, code,
attempt of the attempts DSPy allows, or ``not_retried_partial_output``) and one line
when a later attempt recovers.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from dspy.lm15 import ServerError

__all__ = [
    "CODEX_FINAL_ERROR_CODES",
    "CallOutcome",
    "CodexStreamDroppedError",
    "CodexTransientStreamError",
    "RetryLog",
    "terminal_error",
]

logger = logging.getLogger(__name__)

#: Codes the Codex CLI treats as final that lm15 does not classify itself (lm15 already
#: types ``context_length_exceeded``, ``insufficient_quota``, ``invalid_prompt`` and the
#: sign-in codes as non-retryable errors).
CODEX_FINAL_ERROR_CODES = frozenset({"usage_not_included", "usage_limit_reached"})

#: HTTP statuses (carried by a WebSocket ``error`` frame) that stay retryable among the
#: 4xx: a request timeout and a rate limit. Every other 4xx is the request's own fault.
_RETRYABLE_4XX = frozenset({408, 429})

TRANSIENT_REASON = "codex_transient_stream_error"
DROPPED_REASON = "codex_stream_dropped"


class CodexTransientStreamError(ServerError):
    """A transient in-stream Codex failure (overload, unknown code): DSPy retries it.

    lm15's own fields carry the backend's facts: ``provider_code`` is its error code
    (``error.code``, else ``error.type``; ``None`` when the frame had none) and
    ``status`` the HTTP status the frame carried, if any.
    """

    reason = TRANSIENT_REASON


class CodexStreamDroppedError(CodexTransientStreamError):
    """Codex closed the WebSocket before the response completed: DSPy retries it.

    The retry is DSPy's: it re-issues the call (a full send on a fresh socket) unless
    the caller already saw part of the reply, which DSPy never replays.
    """

    reason = DROPPED_REASON


@dataclass(frozen=True)
class _WireEvent:
    """An ``error`` frame in the SSE-like shape lm15's Responses parser reads."""

    event: str
    data: str


def _error_body(payload: dict[str, Any]) -> dict[str, Any]:
    """The frame's error object: ``error`` on an ``error`` frame, else ``response.error``."""
    error = payload.get("error")
    if not isinstance(error, dict):
        response = payload.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    return error if isinstance(error, dict) else {}


def _status(payload: dict[str, Any]) -> int | None:
    status = payload.get("status")
    if status is None:
        status = payload.get("status_code")
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _codex_treats_as_final(code: str, status: int | None) -> bool:
    """Whether the Codex CLI would give up on this frame rather than retry it."""
    if code in CODEX_FINAL_ERROR_CODES:
        return True
    return status is not None and 400 <= status < 500 and status not in _RETRYABLE_4XX


def terminal_error(wire: Any, request: Any, payload: dict[str, Any]) -> Any:
    """Type one terminal error frame (``error`` / ``response.failed``).

    Returns:
        lm15's own stream error event for a code lm15 classifies or the Codex CLI
        treats as final (forwarded to the caller: lm15 raises its typed error), or a
        :class:`CodexTransientStreamError` to raise for a frame lm15 leaves
        unclassified that the Codex CLI would retry.
    """
    body = _error_body(payload)
    frame = json.dumps({"type": "error", "error": body})
    [event] = list(wire.parse_stream_events(request, _WireEvent(event="error", data=frame)))
    code = str(body.get("code") or body.get("type") or "")
    status = _status(payload)
    if event.error.code != "provider" or _codex_treats_as_final(code, status):
        return event
    message = event.error.message or "Codex failed the response"
    return CodexTransientStreamError(message, provider_code=code or None, status=status)


class RetryLog:
    """Counts each call's failed attempts so every retry is logged with its attempt.

    DSPy re-issues a retried call with the SAME request object, so the request's
    identity names the call. An entry holds its request (its id cannot be reused
    while held) and is dropped when the call succeeds, fails for good, or uses its
    last attempt; at most ``capacity`` calls are tracked (a call that never returns
    to the engine cannot grow the table).
    """

    def __init__(self, capacity: int = 256) -> None:
        """An empty log tracking at most ``capacity`` calls."""
        self._lock = threading.Lock()
        self._capacity = capacity
        self._failed: OrderedDict[int, tuple[Any, int, str]] = OrderedDict()

    def failed(
        self,
        request: Any,
        error: CodexTransientStreamError,
        attempts: int,
        *,
        replayable: bool = True,
    ) -> None:
        """Log one transient failure of ``request`` (DSPy allows ``attempts`` in all).

        ``replayable`` is ``False`` when the caller already saw part of this attempt's
        reply: DSPy never replays such a call, so the failure is final.
        """
        with self._lock:
            held = self._failed.pop(id(request), None)
            attempt = held[1] + 1 if held is not None and held[0] is request else 1
            if replayable and attempt < attempts:
                self._failed[id(request)] = (request, attempt, error.reason)
                while len(self._failed) > self._capacity:
                    self._failed.popitem(last=False)
        if not replayable:
            action = "not_retried_partial_output"
        else:
            action = "dspy_retries" if attempt < attempts else "retries_exhausted"
        logger.warning(
            "codex direct transient failure reason=%s code=%s status=%s attempt=%d/%d "
            "action=%s message=%r",
            error.reason,
            error.provider_code or "-",
            error.status if error.status is not None else "-",
            attempt,
            attempts,
            action,
            error.message,
        )

    def settled(self, request: Any, *, ok: bool) -> None:
        """Forget ``request``; when it had failed transiently and ``ok``, log the recovery."""
        with self._lock:
            held = self._failed.pop(id(request), None)
        if ok and held is not None and held[0] is request:
            logger.info(
                "codex direct recovered reason=%s_recovered attempt=%d", held[2], held[1] + 1
            )


class CallOutcome:
    """One engine call's outcome, as its events reach the caller, fed to a :class:`RetryLog`.

    ``saw`` each item (event or exception), ``ended`` at the end of the stream, and
    ``close`` always (a ``finally``): a transient failure is logged with its attempt;
    any other outcome settles the call (a recovery is logged).

    ``listened``: the events go to a streaming listener (DSPy's ``stream`` path). Once a
    delta the listener shows has gone out, DSPy never replays the call -- a transient
    failure after it is final, and logged so.
    """

    def __init__(self, log: RetryLog, request: Any, *, listened: bool = False) -> None:
        """Watch the call that ``request`` names."""
        self._log = log
        self._request = request
        self._listened = listened
        self._shown = False
        self._state = "open"

    def saw(self, item: Any) -> None:
        """Note one item the call produced."""
        if isinstance(item, CodexTransientStreamError):
            from clio_agent.lm.policy import lm_retries  # noqa: PLC0415

            replayable = not self._shown
            self._state = "transient" if replayable else "failed"
            self._log.failed(self._request, item, lm_retries() + 1, replayable=replayable)
        elif isinstance(item, BaseException) or getattr(item, "type", None) == "error":
            self._state = "failed"
        elif self._listened and _shown_delta(item):
            self._shown = True

    def ended(self) -> None:
        """The stream reached its end: a call with no error succeeded."""
        if self._state == "open":
            self._state = "ok"

    def close(self) -> None:
        """Settle the call unless it failed transiently (DSPy's retry continues it)."""
        if self._state != "transient":
            self._log.settled(self._request, ok=self._state == "ok")


def _shown_delta(event: Any) -> bool:
    """Whether DSPy's listener bridge makes ``event`` a chunk the caller sees: every
    delta but opaque continuation state (``dspy.clients.engines.streaming.ListenerBridge``).
    """
    if getattr(event, "type", None) != "delta":
        return False
    return getattr(getattr(event, "delta", None), "type", None) != "continuation"
