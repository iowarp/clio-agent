"""Read-once memo for one provider catalog check (handshake pass).

A handshake enriches every listed model, and each row consults the same
catalogs: the model overlay, the models.dev indexes and the limits DB. Without a
memo every row re-read and re-indexed them, so a 666-model OpenRouter check
spent ~20 s of CPU re-deriving identical data. Inside :func:`handshake_pass`
each derived value is built once and shared by the pass's rows; outside a pass
every call builds fresh, exactly as before. Nothing outlives the pass, so no
freshness rule changes and no store is added.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")

_PASS: ContextVar[dict[Hashable, Any] | None] = ContextVar("handshake_pass_memo", default=None)


@contextmanager
def handshake_pass() -> Iterator[None]:
    """Share read-once catalog derivations for the duration of one check."""
    token = _PASS.set({})
    try:
        yield
    finally:
        _PASS.reset(token)


def read_once(key: Hashable, build: Callable[[], T]) -> T:
    """``build()`` once per pass for ``key``; outside a pass, every time."""
    memo = _PASS.get()
    if memo is None:
        return build()
    if key not in memo:
        memo[key] = build()
    return memo[key]


def forget(key: Hashable) -> None:
    """Drop ``key`` from the current pass after the pass itself changed its source."""
    memo = _PASS.get()
    if memo is not None:
        memo.pop(key, None)
