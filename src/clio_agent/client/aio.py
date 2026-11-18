"""``AsyncClioClient``: the same client for ``async with``, coroutines and async iterators.

It holds a :class:`~clio_agent.client.ClioClient` with the same options and runs each
call on a worker thread, so a coroutine never blocks the event loop. Streams
(``stream_flow``, ``RunSession.stream``, ``sessions.events``) are async iterators fed
from a worker thread; leaving one early stops the thread at its next event.

Example:
    >>> async with AsyncClioClient(options=ClioOptions(agent="deep-researcher")) as c:
    ...     async for event in c.stream_flow("deep-research", "What limits ...?"):
    ...         print(type(event).__name__)
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from clio_agent.client.client import ClioClient
from clio_agent.client.options import ClioOptions
from clio_agent.client.run import RunResult
from clio_agent.client.run_api import RunSession

_DONE = object()


async def aiterate(make: Callable[[], Iterator[Any]]) -> AsyncIterator[Any]:
    """Iterate the sync iterator ``make()`` on a worker thread, as an async iterator."""

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    stop = threading.Event()

    def pump() -> None:
        iterator = make()
        try:
            for item in iterator:
                loop.call_soon_threadsafe(queue.put_nowait, (False, item))
                if stop.is_set():
                    break
        except BaseException as exc:  # noqa: BLE001 - forwards: re-raised in the consumer
            loop.call_soon_threadsafe(queue.put_nowait, (True, exc))
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
            loop.call_soon_threadsafe(queue.put_nowait, (False, _DONE))

    worker = loop.run_in_executor(None, pump)
    try:
        while True:
            failed, item = await queue.get()
            if failed:
                raise item
            if item is _DONE:
                break
            yield item
    finally:
        stop.set()
        await worker


class _AsyncNamespace:
    """An API namespace (``sessions``, ``messages``, ...) with coroutine methods."""

    def __init__(self, namespace: Any) -> None:
        self._namespace = namespace

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._namespace, name)
        if not callable(attribute):
            return attribute
        if name == "events":

            def events(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                def make() -> Iterator[Any]:
                    with attribute(*args, **kwargs) as stream:
                        yield from stream

                return aiterate(make)

            return events

        @functools.wraps(attribute)
        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attribute, *args, **kwargs)

        return call


class AsyncRunSession:
    """:class:`RunSession` for async code."""

    def __init__(self, session: RunSession) -> None:
        self.sync = session
        self.id = session.id

    async def run(self, prompt: str, *, files: list[str] | None = None) -> RunResult:
        return await asyncio.to_thread(self.sync.run, prompt, files=files)

    def stream(self, prompt: str, *, files: list[str] | None = None) -> AsyncIterator[Any]:
        return aiterate(lambda: self.sync.stream(prompt, files=files))


class AsyncClioClient:
    """The async twin of :class:`ClioClient`: same options, same methods."""

    def __init__(self, options: ClioOptions | None = None, **kwargs: Any) -> None:
        self.sync = ClioClient(options, **kwargs)
        self.options = self.sync.options
        for name in ("sessions", "messages", "workspaces", "permissions", "documents"):
            setattr(self, name, _AsyncNamespace(getattr(self.sync, name)))

    async def __aenter__(self) -> AsyncClioClient:
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def open(self) -> AsyncClioClient:
        await asyncio.to_thread(self.sync.open)
        return self

    async def close(self) -> None:
        await asyncio.to_thread(self.sync.close)

    async def run(self, prompt: str, **kwargs: Any) -> RunResult:
        """See :meth:`ClioClient.run`."""
        return await asyncio.to_thread(functools.partial(self.sync.run, prompt, **kwargs))

    async def run_flow(self, flow_id: str, input: str, **kwargs: Any) -> RunResult:  # noqa: A002
        """See :meth:`ClioClient.run_flow`."""
        return await asyncio.to_thread(
            functools.partial(self.sync.run_flow, flow_id, input, **kwargs)
        )

    def stream_flow(self, flow_id: str, input: str, **kwargs: Any) -> AsyncIterator[Any]:  # noqa: A002
        """See :meth:`ClioClient.stream_flow`."""
        return aiterate(lambda: self.sync.stream_flow(flow_id, input, **kwargs))

    async def session(self, **kwargs: Any) -> AsyncRunSession:
        """See :meth:`ClioClient.session`."""
        return AsyncRunSession(
            await asyncio.to_thread(functools.partial(self.sync.session, **kwargs))
        )

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.sync, name)
        if not callable(attribute):
            return attribute

        @functools.wraps(attribute)
        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attribute, *args, **kwargs)

        return call


__all__ = ["AsyncClioClient", "AsyncRunSession", "aiterate"]
