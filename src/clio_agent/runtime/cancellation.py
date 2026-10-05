"""Cooperative turn cancellation, independent of the agent host implementation."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CHECKER: ContextVar[Callable[[], bool] | None] = ContextVar(
    "clio_cancellation_checker", default=None
)


@contextmanager
def cancellation_checker(checker: Callable[[], bool] | None) -> Iterator[None]:
    """Scope a cooperative cancellation checker to the current agent turn."""
    token = _CHECKER.set(checker)
    try:
        yield
    finally:
        _CHECKER.reset(token)


def cancellation_requested() -> bool:
    """Return whether the active cooperative cancellation checker is set."""
    checker = _CHECKER.get()
    return bool(checker is not None and checker())
