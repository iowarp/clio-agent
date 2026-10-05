"""Drain a runtime revision while new turns wait, without blocking its children."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any


class TurnRevisionGate:
    """Serialize revision changes with complete turns on the app's event loop.

    A live turn may need to await a child turn. Those descendants must finish
    with their parent before the writer enters; unrelated submissions wait.
    Cancelled queued submissions close their original coroutine explicitly.
    """

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._open.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._writers = asyncio.Lock()
        self._readers: set[asyncio.Task[Any]] = set()
        self._parent: ContextVar[asyncio.Task[Any] | None] = ContextVar(
            "blueprint_revision_parent", default=None
        )

    async def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run a whole turn under one revision; wait if a revision is being applied."""
        entered = False
        token = None
        task = asyncio.current_task()
        assert task is not None
        try:
            if self._parent.get() not in self._readers:
                await self._open.wait()
            # No await between the gate and registering this reader.
            self._readers.add(task)
            self._idle.clear()
            token = self._parent.set(task)
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
        if self._parent.get() in self._readers:
            raise RuntimeError("A running turn cannot reload its own runtime revision")
        async with self._writers:
            self._open.clear()
            try:
                await self._idle.wait()
                yield
            finally:
                self._open.set()
