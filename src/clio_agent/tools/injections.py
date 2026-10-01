"""What the harness tells the agent about a tool call, collected per call.

When the harness adds something to a tool call's result -- a "did you mean" path
hint, a circuit-breaker warning, an oversize result's file -- it tells the agent in
the result text AND notes it here. The agent loop collects the notes of each call
(:func:`collect`) and records every one as an ``injection``, so the user sees exactly
what the agent was told. A leaf module: no GACT import.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["collect", "note"]

_NOTES: ContextVar[list[tuple[str, str]] | None] = ContextVar("clio_tool_injections", default=None)


def note(source: str, text: str) -> None:
    """Note that the harness told the agent ``text`` (``source``: which fix)."""
    notes = _NOTES.get()
    if notes is not None and text:
        notes.append((source, text))


@contextmanager
def collect() -> Iterator[list[tuple[str, str]]]:
    """Collect the notes of the tool call made inside the block, in order."""
    notes: list[tuple[str, str]] = []
    token = _NOTES.set(notes)
    try:
        yield notes
    finally:
        _NOTES.reset(token)
