"""One access decision reads each of its inputs once (owner module).

A file-policy decision (the permission gate's folder check, a filesystem tool's own
check) reads the same inputs many times: the connected-sources ledger and the storage
roots derived from it, each workspace's materialized read roots, the user directories.
Each read stats or resolves files, and one decision made the same read five to sixteen
times (2026-11-05 profile of a script listing a folder: file policy was 62% of a nested
call's own time). Inside :func:`check_scope` the first read is kept for the rest of that
decision; outside one, every read is made afresh, as before. A scope never outlives the
decision that opened it, so a change to the ledger or the folders is seen by the next.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

__all__ = ["check_scope", "checked", "once"]

_P = ParamSpec("_P")
_T = TypeVar("_T")
_MEMO: contextvars.ContextVar[dict[Hashable, Any] | None] = contextvars.ContextVar(
    "clio_check_scope", default=None
)


@contextmanager
def check_scope() -> Iterator[None]:
    """Keep each input read during this decision for the rest of it (nested: the outer)."""
    if _MEMO.get() is not None:
        yield
        return
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def checked(function: Callable[_P, _T]) -> Callable[_P, _T]:
    """Run ``function`` as one decision (:func:`check_scope`).

    Raises:
        TypeError: ``function`` is a coroutine function. Its scope would close when the
            coroutine is created, before any of its reads, so nothing would be kept.
    """
    if inspect.iscoroutinefunction(function):
        raise TypeError(
            f"checked() cannot wrap coroutine function {function.__qualname__}: "
            "open check_scope() inside it instead"
        )

    @functools.wraps(function)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        with check_scope():
            return function(*args, **kwargs)

    return run


def once(key: Hashable, compute: Callable[[], _T]) -> _T:
    """``compute()``, made once per decision when one is open, else every time."""
    memo = _MEMO.get()
    if memo is None:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]
