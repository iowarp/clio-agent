"""Drain a runtime revision while new turns wait, without blocking its children."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any


class TurnRevisionGate:
    """Serialize revision changes with complete turns on the app's event loop.

    A live turn may need to await a child turn. Those descendants must finish
    with their parent before the writer enters; unrelated submissions wait.
    Cancelled queued submissions close their original coroutine explicitly.

    A turn's task is named in its context (``_parent``) so a child turn it starts is
    recognised; every task the turn creates copies that context. The name is a weak
    reference: a strong one let each child keep its parent's finished task -- and the
    parent's context with its ReAct loop, module and call ledger -- alive, so a
    long-lived task that copied one turn's context kept the whole chain of turns (live,
    #1846: 119 finished turns).
    """

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._open.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._writers = asyncio.Lock()
        self._readers: set[asyncio.Task[Any]] = set()
        self._parent: ContextVar[weakref.ref[asyncio.Task[Any]] | None] = ContextVar(
            "turn_revision_parent", default=None
        )

    def _in_running_turn(self) -> bool:
        parent = self._parent.get()
        return parent is not None and parent() in self._readers

    async def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run a whole turn under one revision; wait if a revision is being applied."""
        entered = False
        token = None
        task = asyncio.current_task()
        assert task is not None
        try:
            if not self._in_running_turn():
                await self._open.wait()
            # No await between the gate and registering this reader.
            self._readers.add(task)
            self._idle.clear()
            token = self._parent.set(weakref.ref(task))
            entered = True
            return await coroutine
        finally:
            if not entered:
                coroutine.close()
            if token is not None:
                self._parent.reset(token)
            self._readers.discard(task)
            if not self._readers:
                self._idle.set()

    @asynccontextmanager
    async def change(self) -> AsyncIterator[None]:
        """Wait for live turns and their children, then exclusively apply a revision."""
        if self._in_running_turn():
            raise RuntimeError("A running turn cannot reload its own runtime revision")
        async with self._writers:
            self._open.clear()
            try:
                await self._idle.wait()
                yield
            finally:
                self._open.set()
