"""Two viewers of one page connecting at once share one screencast (browser plan).

Fakes for the socket, the page record and the browser service; the real ``_serve``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.browser import live
from clio_agent.gact.browser import service as service_module


class _Socket:
    headers: dict[str, str] = {}
    scope: dict[str, Any] = {}

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.leave = asyncio.Event()

    async def accept(self, subprotocol: str | None = None) -> None:
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None

    async def send_json(self, value: Any) -> None:
        self.sent.append(value)

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        from starlette.websockets import WebSocketDisconnect

        await self.leave.wait()
        raise WebSocketDisconnect()


class _Service:
    """Runs a submitted coroutine on the test's loop, as the browser loop would."""

    def __init__(self, record: Any) -> None:
        self.pages = {record.id: record}
        self.app: Any = None

    def submit(self, coroutine: Any) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coroutine, asyncio.get_running_loop())

    def state(self, record: Any) -> dict[str, Any]:
        return {"url": "https://a/"}


@pytest.mark.asyncio
async def test_two_viewers_connecting_at_once_start_one_screencast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: start the screencast whenever ``hub.screencast`` is unset (the old
    ``_serve``) -> the second viewer starts a second one while the first starts -> red."""
    record = SimpleNamespace(id="page_1", session_id="sess_1", closed=False, viewers=0)
    svc = _Service(record)
    starts: list[str] = []

    async def start(_svc: Any, _record: Any, hub: Any) -> None:
        starts.append(hub.page_id)
        await asyncio.sleep(0.05)  # the screencast takes a moment to start
        hub.screencast, hub.last_frame = object(), b"still"

    async def stop(hub: Any) -> None:
        hub.screencast = None

    monkeypatch.setattr(service_module, "service", lambda: svc)
    monkeypatch.setattr(live, "_start", start)
    monkeypatch.setattr(live, "_stop", stop)
    app = SimpleNamespace(state=SimpleNamespace(bearer_token=None, sessions={}))
    sockets = [_Socket(), _Socket()]
    serving = [
        asyncio.create_task(live._serve(app, socket, "sess_1", "page_1")) for socket in sockets
    ]
    for _ in range(50):
        await asyncio.sleep(0.01)
        if all(any(isinstance(m, dict) for m in s.sent) for s in sockets):
            break
    assert starts == ["page_1"]  # one screencast for both viewers
    await asyncio.sleep(0.02)
    assert all(b"still" in s.sent for s in sockets)  # each was sent the still frame
    for socket in sockets:
        socket.leave.set()
    await asyncio.wait_for(asyncio.gather(*serving), timeout=2)
    assert "page_1" not in live._HUBS
