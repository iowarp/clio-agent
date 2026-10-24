"""``asyncio.wait`` lets go of its caller once it returns.

Python 3.14's ``asyncio.wait`` records the waiting task on every awaited future
(``future_add_to_awaited_by``, for ``asyncio.graph``) and removes it only from the
futures that complete. A future still pending when the wait returns -- the
``FIRST_COMPLETED`` case -- keeps a reference to the waiting task for as long as that
future lives, and the task keeps its context.

FastMCP waits on each call together with the client's long-lived session task
(``Client._await_with_session_monitoring``), so every MCP tool call left its task, and
the turn context it ran in -- the ReAct loop, module, tools and call ledger -- on the
pooled session for good (live, #1846: 128 finished tool-call tasks on 11 sessions).

:func:`install` wraps ``asyncio.tasks._wait`` so the waiting task is removed from every
awaited future on the way out. Removing it twice is harmless, so a Python that fixes
this upstream is unaffected.
"""

from __future__ import annotations

from typing import Any

__all__ = ["install"]


def install() -> None:
    """Make ``asyncio.wait`` drop its caller from the futures it awaited (idempotent)."""

    from asyncio import futures, tasks  # noqa: PLC0415

    discard = getattr(futures, "future_discard_from_awaited_by", None)
    original = tasks._wait  # type: ignore[attr-defined]
    if discard is None or getattr(original, "_clio_releases_waiter", False):
        return

    async def _wait(fs: Any, timeout: Any, return_when: Any, loop: Any) -> Any:
        waiter = tasks.current_task()
        try:
            return await original(fs, timeout, return_when, loop)
        finally:
            if waiter is not None:
                for future in fs:
                    discard(future, waiter)

    _wait._clio_releases_waiter = True  # type: ignore[attr-defined]
    tasks._wait = _wait  # type: ignore[attr-defined]
