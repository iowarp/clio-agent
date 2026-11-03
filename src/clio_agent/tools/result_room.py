"""How much of one tool result the model is shown: a share of its context window.

The rule. A result is shown whole when its tokens fit

    min(tools.result_share x window, window x autocompact.pct - last prompt tokens)

counted with the active model's tokenizer (``_estimate_text_tokens``: the model's own
counter where CLIO has it, else about four characters a token).

- The share keeps room to work. A step may read several results and still has to
  think and answer, so no single result may take the whole window. The default 0.125 is
  25,000 tokens on a 200,000-token window, the size Claude Code shows of an MCP result,
  and it scales with the model: 4,000 tokens on 32k, 125,000 on 1M.
- The room left is the real limit: the window up to the point where CLIO compacts it,
  less the prompt the model already carries (the same room a child's report is fitted
  to, :mod:`clio_agent.gact.child_report_fit`).

A result that does not fit is saved to a file and the model gets its head and where the
rest is (:func:`clio_agent.tools.mcp_result_projection.bounded_model_tool_result`).
Tools that size their own result (``fs_read_file``, ``fs_search_text``, the shell) fill
the same room and say how to continue. :attr:`ResultRoom.chars` is the room in
characters at four a token, the size those tools fill before :meth:`ResultRoom.fits`
counts the tokens.

When the window is unknown (no handshake or catalog entry for the model, or a call
outside an agent loop) there is no window to share, but the result must still fit a
model: one delivered whole into an unknown window can overflow it, and the overflow
handling drops it entirely, while a result saved to a file loses nothing. The room is
then ``tools.unknown_window_result_tokens`` (default 25,000, the same Claude Code size),
traced as :data:`TOOL_RESULT_WINDOW_UNKNOWN`.

A tool that runs away from the calling agent's context (a native task) carries the room
measured at its call (:func:`room_limits`, :func:`room_from_limits`, :func:`pinned_room`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from clio_agent import conf
from clio_agent.runtime import trace

__all__ = [
    "CHARS_PER_TOKEN",
    "RESULT_ROOM_LEFT",
    "RESULT_WINDOW_SHARE",
    "TOOL_RESULT_WINDOW_UNKNOWN",
    "ResultRoom",
    "pinned_room",
    "result_room",
    "result_share",
    "room_from_limits",
    "room_left",
    "room_limits",
    "unknown_window_result_tokens",
    "window_share_tokens",
]

#: Characters a token, the inverse of the estimator's fallback (``_heuristic_text_tokens``).
CHARS_PER_TOKEN = 4
#: Typed reason: the window is unknown, so the room is ``tools.unknown_window_result_tokens``.
TOOL_RESULT_WINDOW_UNKNOWN = "tool_result_window_unknown"
#: Why a room has its size: the window's share, or the room left before compaction.
RESULT_WINDOW_SHARE = "window_share"
RESULT_ROOM_LEFT = "room_left"
#: No tokenizer averages more characters a token than this on text, so a longer text
#: cannot fit and is not counted (counting megabytes with a tokenizer takes seconds).
_MAX_CHARS_PER_TOKEN = 8


@dataclass(frozen=True)
class ResultRoom:
    """The model-facing size of one result, in tokens, and the same in characters."""

    tokens: int
    chars: int
    reason: str
    window: int = 0

    def measure(self, text: str) -> int:
        """Tokens of ``text`` for the active model."""
        from clio_agent.gact.runtime.context_tokens import _estimate_text_tokens  # noqa: PLC0415

        return _estimate_text_tokens(text)

    def fits(self, text: str) -> bool:
        """Whether ``text`` is shown whole."""
        if not text:
            return True
        if len(text) > max(self.tokens, 1) * _MAX_CHARS_PER_TOKEN:
            return False
        return self.measure(text) <= self.tokens


_PINNED: ContextVar[ResultRoom | None] = ContextVar("clio_result_room", default=None)


def _from_tokens(tokens: int, reason: str, window: int) -> ResultRoom:
    tokens = max(0, tokens)
    return ResultRoom(tokens=tokens, chars=tokens * CHARS_PER_TOKEN, reason=reason, window=window)


def result_share() -> float:
    """Share of the context window one result may use (``tools.result_share``).

    Raises:
        ValueError: The share is not in (0, 1].
    """
    value = conf.resolve(
        "tools.result_share", env="CLIO_TOOL_RESULT_SHARE", default=0.125, cast=conf.as_float
    )
    if not 0 < value <= 1:
        raise ValueError(f"tools.result_share must be in (0, 1], not {value}")
    return value


def unknown_window_result_tokens() -> int:
    """Tokens of one result shown when the window is unknown.

    Raises:
        ValueError: The configured size is below 1.
    """
    value = conf.resolve(
        "tools.unknown_window_result_tokens",
        env="CLIO_UNKNOWN_WINDOW_RESULT_TOKENS",
        default=25_000,
        cast=conf.as_int,
    )
    if value < 1:
        raise ValueError(f"tools.unknown_window_result_tokens must be at least 1, not {value}")
    return value


def _window() -> int:
    from clio_agent.gact.context import active_react_context_window  # noqa: PLC0415

    return max(0, active_react_context_window())


def room_left() -> int | None:
    """Tokens left before compaction: window x ``autocompact.pct`` less the last prompt.

    ``None`` when the window is unknown.
    """
    from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
        _autocompact_threshold,
        _last_prompt_tokens,
    )

    window = _window()
    if window <= 0:
        return None
    return int(window * _autocompact_threshold()) - _last_prompt_tokens()


def window_share_tokens() -> int | None:
    """The window's share for one result, without the room left (``None``: unknown window)."""
    window = _window()
    return int(window * result_share()) if window > 0 else None


def _measure() -> ResultRoom:
    window = _window()
    if window <= 0:
        tokens = unknown_window_result_tokens()
        trace.event(
            "TOOLS",
            "%s: one result is shown up to %d tokens (tools.unknown_window_result_tokens)",
            TOOL_RESULT_WINDOW_UNKNOWN,
            tokens,
        )
        return _from_tokens(tokens, TOOL_RESULT_WINDOW_UNKNOWN, 0)
    share = int(window * result_share())
    left = room_left()
    if left is not None and left < share:
        return _from_tokens(left, RESULT_ROOM_LEFT, window)
    return _from_tokens(share, RESULT_WINDOW_SHARE, window)


def result_room() -> ResultRoom:
    """The room one result has now: pinned by the caller, or measured here."""
    return _PINNED.get() or _measure()


@contextmanager
def pinned_room(room: ResultRoom | None) -> Iterator[None]:
    """Use ``room`` for results made inside (the room measured where the call was made)."""
    token = _PINNED.set(room)
    try:
        yield
    finally:
        _PINNED.reset(token)


def room_limits(room: ResultRoom) -> dict[str, int | str]:
    """``room`` as recorded execution limits of a task that runs apart from its caller."""
    return {
        "result_tokens": room.tokens,
        "result_chars": room.chars,
        "result_reason": room.reason,
        "result_window": room.window,
    }


def room_from_limits(limits: Mapping[str, object]) -> ResultRoom | None:
    """The room recorded by :func:`room_limits`, or ``None`` when none was recorded."""
    tokens, chars = limits.get("result_tokens"), limits.get("result_chars")
    if not isinstance(tokens, int) or not isinstance(chars, int):
        return None
    window = limits.get("result_window")
    return ResultRoom(
        tokens=tokens,
        chars=chars,
        reason=str(limits.get("result_reason") or ""),
        window=window if isinstance(window, int) else 0,
    )
