"""A Claude Code call's stall handling: growing waits and a visible notice at each.

The waits come from :func:`.claude_code_expiry.stall_waits`; nothing here fails the turn.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from dspy.lm15 import TextDelta, ToolCallDelta

from clio_agent.providers.claude_code_expiry import (
    ClaudeCodeIdleTimeout,
    StreamSilence,
    stall_waits,
)

logger = logging.getLogger(__name__)


def _cancel_requested() -> bool:
    from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415

    return cancellation_requested()


class _StallLadder:
    """One call's stall handling: growing waits, a visible notice at each, no deadline.

    Before any of the reply is on screen a stall resends the query; once text is shown
    a resend would repeat it, so the same stream is waited on, longer each time. The
    person sees every stall and can stop the turn; nothing here fails it.
    """

    def __init__(self, first_wait_s: float) -> None:
        self._waits = stall_waits(first_wait_s)
        self._lock = threading.Lock()
        self.wait_s = next(self._waits)
        self.attempt = 1
        self.shown = False  # read on the client's owner-loop thread

    def observe(self, event: Any) -> None:
        delta = getattr(event, "delta", None)
        if isinstance(delta, ToolCallDelta) or (isinstance(delta, TextDelta) and delta.text):
            self.shown = True

    def _advance(self) -> float:
        with self._lock:
            self.wait_s = next(self._waits)
            return self.wait_s

    def on_silence(self, silent_s: float) -> float | None:
        """The relay's stall decision (owner-loop thread): wait longer, or resend."""
        del silent_s
        return self._advance() if self.shown else None

    def still_waiting(self, silence: StreamSilence) -> None:
        _stall_notice(
            "Claude Code has sent nothing for "
            f"{_duration(silence.silent_s)}. Still waiting for the rest of the reply; next "
            f"check in {_duration(silence.next_wait_s)}. Stop the turn to cancel.",
            silent_s=silence.silent_s,
            next_wait_s=silence.next_wait_s,
            action="wait",
        )

    def resend(self, exc: ClaudeCodeIdleTimeout) -> None:
        self.attempt += 1
        wait = self._advance()
        _stall_notice(
            f"Claude Code sent nothing for {_duration(exc.idle_s)}. Retrying the request "
            f"(attempt {self.attempt}) and waiting up to {_duration(wait)}. "
            "Stop the turn to cancel.",
            silent_s=exc.idle_s,
            next_wait_s=wait,
            retry_attempt=self.attempt,
            action="resend",
        )


def _duration(seconds: float) -> str:
    whole = round(seconds)
    if whole < 60:
        return f"{whole} s"
    minutes, rest = divmod(whole, 60)
    return f"{minutes} min" if not rest else f"{minutes} min {rest} s"


def _stall_notice(text: str, **metadata: Any) -> None:
    """Show the stall to the person (and on the record), never to the model."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    logger.warning("claude_code stream stall %s", " ".join(f"{k}={v}" for k, v in metadata.items()))
    stream_audit("lm.stream.stall", reason="model_stream_stalled", **metadata)
    try:
        from clio_agent.gact.model_notices import post_model_notice  # noqa: PLC0415

        post_model_notice("model_stream_stall", text, **metadata)
    except (RuntimeError, TimeoutError):
        # after_live_chunks: the emitter loop is gone, or busy past its wait.
        logger.warning("reason=model_stream_stall_notice_failed", exc_info=True)
