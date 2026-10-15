"""Deterministic cleanup for LMs constructed for one forward or goal check.

The context owns only fresh factory instances, never a borrowed session/Fleet LM.
History and usage stay readable after close; DSPy releases its owned HTTP pools.
Scope lists propagate through copied contexts into credential-resolution workers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)


class _ClientCleanupError(RuntimeError):
    """One client failed to close; the owner must still close its remaining clients."""


async def _close_client(lm: Any) -> None:
    """Expose arbitrary client failures as a typed lifecycle failure, preserving the cause."""
    try:
        await lm.aclose()
    except Exception as exc:
        raise _ClientCleanupError("Owned model client cleanup failed") from exc


class _Resources:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lms: list[Any] = []
        self.closed = False

    def record(self, lm: Any) -> None:
        with self.lock:
            late = self.closed
            if not late:
                self.lms.append(lm)
        if late:
            # Cancellation can outlive a resolving worker. A just-constructed
            # LM has not dispatched an async call; close its sync resources now.
            lm.close()

    def take(self) -> list[Any]:
        with self.lock:
            self.closed = True
            lms, self.lms = self.lms, []
            return lms


_CURRENT: ContextVar[_Resources | None] = ContextVar("clio_owned_lm_resources", default=None)


def record_created_lm(lm: Any) -> None:
    """Enroll a fresh factory LM in the current operation's resource lifetime."""
    if (resources := _CURRENT.get()) is not None:
        resources.record(lm)


async def _close(lms: list[Any], primary: BaseException | None) -> None:
    import anyio  # noqa: PLC0415

    errors: list[Exception] = []
    with anyio.CancelScope(shield=True):
        for lm in reversed(lms):
            try:
                await _close_client(lm)
            except _ClientCleanupError as failure:
                exc = failure.__cause__
                if not isinstance(exc, Exception):
                    exc = failure
                errors.append(exc)
                logger.warning(
                    "owned model client cleanup failed error_type=%s", type(exc).__name__
                )
    if errors:
        if primary is not None:
            primary.add_note("Owned model client cleanup also failed; see the runtime log.")
        else:
            raise ExceptionGroup("Owned model client cleanup failed", errors)


@contextmanager
def model_resources() -> Iterator[None]:
    """Close a forward's fresh clients on the persistent LM loop after it unwinds."""
    from clio_agent.lm.engines.lm_loop import run_on_lm_loop  # noqa: PLC0415

    resources = _Resources()
    token = _CURRENT.set(resources)
    primary: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _CURRENT.reset(token)
        if lms := resources.take():
            run_on_lm_loop(lambda: _close(lms, primary))


@asynccontextmanager
async def async_model_resources() -> AsyncIterator[None]:
    """Close a checker's fresh clients on its owner loop, including on cancellation."""
    resources = _Resources()
    token = _CURRENT.set(resources)
    primary: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _CURRENT.reset(token)
        await _close(resources.take(), primary)
