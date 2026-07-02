"""Fixtures for the SDK test suite.

The SDK is exercised against the REAL in-process gact app
(``build_app()``) over a genuine httpx transport — no live server, no
mocks of the wire. Neither ``httpx.ASGITransport`` (async, buffers the
whole body) nor starlette's ``TestClient`` (per-request portal,
buffers streaming responses) can consume gact's infinite SSE feed
incrementally, so :class:`StreamingASGITransport` below drives the
ASGI app on one persistent background event loop and hands response
chunks over a thread-safe queue as the app produces them. Background
turn tasks scheduled during a POST keep running on that same loop,
exactly like under uvicorn.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.gact.app import build_app
from clio_agent.sdk import ClioClient

_DONE = object()


class StreamingASGITransport(httpx.BaseTransport):
    """Sync httpx transport running an ASGI app on a background loop.

    The response is returned as soon as ``http.response.start``
    arrives; body chunks stream through a queue, so an SSE response
    that never ends can still be consumed (and closed) incrementally.
    """

    def __init__(self, app: Any, *, start_timeout_s: float = 30.0) -> None:
        self._app = app
        self._start_timeout_s = start_timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="sdk-test-asgi-loop", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            # ASGI raw_path excludes the query string; httpx's includes it.
            "raw_path": request.url.raw_path.split(b"?", 1)[0],
            "query_string": request.url.query,
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "server": (request.url.host, request.url.port or 80),
            "client": ("testclient", 50000),
            "root_path": "",
        }

        started = threading.Event()
        meta: dict[str, Any] = {}
        chunks: queue.Queue[Any] = queue.Queue()
        disconnected = asyncio.Event()

        async def receive() -> dict[str, Any]:
            if not meta.get("body_sent"):
                meta["body_sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                meta["status"] = message["status"]
                meta["headers"] = message.get("headers", [])
                started.set()
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    chunks.put(chunk)
                if not message.get("more_body", False):
                    chunks.put(_DONE)

        future = asyncio.run_coroutine_threadsafe(self._app(scope, receive, send), self._loop)

        def _on_done(f: Any) -> None:
            if not f.cancelled() and f.exception() is not None:
                chunks.put(f.exception())
            chunks.put(_DONE)
            started.set()

        future.add_done_callback(_on_done)

        if not started.wait(self._start_timeout_s):
            future.cancel()
            raise TimeoutError(f"ASGI app produced no response within {self._start_timeout_s}s")
        if "status" not in meta:
            # The app crashed before http.response.start.
            exc = None if future.cancelled() else future.exception()
            raise exc if exc is not None else RuntimeError("ASGI app sent no response")

        stream = _QueueByteStream(chunks, future, self._loop, disconnected)
        return httpx.Response(
            status_code=meta["status"],
            headers=meta["headers"],
            stream=stream,
            request=request,
        )


class _QueueByteStream(httpx.SyncByteStream):
    def __init__(
        self,
        chunks: queue.Queue[Any],
        future: Any,
        loop: asyncio.AbstractEventLoop,
        disconnected: asyncio.Event,
    ) -> None:
        self._chunks = chunks
        self._future = future
        self._loop = loop
        self._disconnected = disconnected

    def __iter__(self) -> Iterator[bytes]:
        while True:
            item = self._chunks.get(timeout=60)
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._disconnected.set)
        self._future.cancel()


# --------------------------------------------------------------------------- #
# stub agent — same contract the gact suite drives turns with
# --------------------------------------------------------------------------- #


@dataclass
class StubPrediction:
    answer: str = "stub answer"
    selected_expert: str = "data_expert"
    routing_rationale: str = "stubbed routing"
    permissions_requested: list[dict[str, Any]] = field(default_factory=list)


class StubAgent:
    """Minimal ClioAgent stand-in: records calls, returns a canned turn."""

    def __init__(
        self,
        answer: str = "stub answer",
        permissions_requested: list[dict[str, Any]] | None = None,
    ) -> None:
        self.answer = answer
        self.permissions_requested = list(permissions_requested or [])
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str) -> StubPrediction:
        self.calls.append((question, session_id))
        return StubPrediction(
            answer=self.answer,
            permissions_requested=self.permissions_requested,
        )


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _fresh_arc(tmp_path: Path) -> Any:
    """A real in-memory ARC, mirroring tests/test_gact/conftest.py."""

    from clio_agent.arc.live import _MemoryStore
    from clio_agent.arc.memory import ARCMemory

    return ARCMemory(data_dir=str(tmp_path / "arc"), store=_MemoryStore())


@pytest.fixture()
def stub_agent() -> StubAgent:
    return StubAgent()


@pytest.fixture()
def app(tmp_path: Path, stub_agent: StubAgent) -> Any:
    return build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=stub_agent,
        arc=_fresh_arc(tmp_path),
    )


@pytest.fixture()
def transport(app: Any) -> Iterator[StreamingASGITransport]:
    t = StreamingASGITransport(app)
    yield t
    t.close()


@pytest.fixture()
def client(transport: StreamingASGITransport) -> Iterator[ClioClient]:
    with ClioClient("http://testserver", transport=transport) as c:
        yield c
