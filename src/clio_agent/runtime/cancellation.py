"""Cooperative turn cancellation, independent of the agent host implementation."""

import threading
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
    if scope_stopped():
        return True
    checker = _CHECKER.get()
    return bool(checker is not None and checker())


#: A stop that belongs to the calls started in one scope, not to the session's turn: a
#: script's nested calls end when the script is stopped, even when no turn is running.
_SCOPE_STOP: ContextVar[threading.Event | None] = ContextVar("clio_scope_stop", default=None)


def bind_scope_stop(event: threading.Event) -> None:
    """Bind ``event`` as the stop of the calls made in this context (set it in a copy)."""
    _SCOPE_STOP.set(event)


def scope_stopped() -> bool:
    """Whether the scope this call was started in has been stopped."""
    event = _SCOPE_STOP.get()
    return bool(event is not None and event.is_set())


def with_scope_stop(checker: Callable[[], bool] | None) -> Callable[[], bool] | None:
    """``checker``, also true once this context's scope is stopped; unchanged without one.

    The result keeps the checker's ``cause`` (why a call was cancelled).
    """
    if _SCOPE_STOP.get() is None:
        return checker

    def check() -> bool:
        return scope_stopped() or bool(checker is not None and checker())

    cause = getattr(checker, "cause", None)
    if callable(cause):
        check.cause = cause  # type: ignore[attr-defined]
    return check
