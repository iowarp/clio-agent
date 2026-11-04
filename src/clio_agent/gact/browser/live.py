"""The shared tab: a page streamed to the web UI, and the person's input played back.

``GET /v1/sessions/{sid}/browser/{page_id}/live`` upgrades to a WebSocket, admitted like the
infrastructure transport: the origin guard and the bearer token (from the ``clio-bearer.``
subprotocol, never echoed; the server accepts ``clio.browser.v1``). The server sends the
page's JPEG frames from ``page.screencast`` as binary messages and its state as JSON
(``url``, ``title``, ``loading``, ``agent_acting``, the agent's last action, ``view_only``,
``sandbox_off``, ``navigation_blocked``, the frame's viewport). The screencast and the
agent-action annotations run only while at least one viewer is connected.

The view sends ``mouse``, ``wheel``, ``key``, ``text``, ``resize``, ``navigate`` and ``theme``
messages; they are played through ``page.mouse``, ``page.keyboard`` and the page's own
navigation, and set ``person_acted`` for the agent's next call. There are no locks: the
person and the agent can both act at any time. The first connected viewer of a page sends
input; others see it with ``view_only`` and their input is ignored (BR12). Frames go
through GACT like everything else, so the view works the same for a local agent, a remote
one and one behind a tunnel.

Also here: ``GET /v1/sessions/{sid}/browser/pages`` (the session's pages, for the Browser
surface and the Work rail) and ``POST .../pages/{page_id}/allow-host`` (the person allows a
host the page was blocked from, as their own grant).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from clio_agent.gact.browser import settings
from clio_agent.runtime import trace

__all__ = ["LIVE_PROTOCOL", "register_browser_routes"]

LIVE_PROTOCOL = "clio.browser.v1"
#: Frames a slow viewer may have queued before older ones are dropped (latest wins).
_FRAME_BACKLOG = 2
_MOUSE_ACTIONS = ("move", "down", "up", "click")
_KEY_ACTIONS = ("down", "up", "press")
_NAVIGATIONS = ("goto", "back", "forward", "reload")


@dataclass
class _Viewer:
    socket: Any
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Any] = field(default_factory=lambda: asyncio.Queue())
    controls: bool = False


@dataclass
class _Hub:
    """The viewers of one page and its running screencast."""

    page_id: str
    viewers: list[_Viewer] = field(default_factory=list)
    screencast: Any = None
    actions: Any = None
    viewport: tuple[int, int] = (0, 0)
    theme: dict[str, str] = field(default_factory=dict)


_HUBS: dict[str, _Hub] = {}


def _deliver(viewer: _Viewer, item: Any) -> None:
    """Queue ``item`` for one viewer from any thread; frames drop oldest when it lags."""

    def put() -> None:
        if isinstance(item, bytes):
            while viewer.queue.qsize() >= _FRAME_BACKLOG:
                viewer.queue.get_nowait()
        viewer.queue.put_nowait(item)

    viewer.loop.call_soon_threadsafe(put)


def _state(service: Any, record: Any, hub: _Hub, viewer: _Viewer) -> dict[str, Any]:
    width, height = hub.viewport
    return {
        "type": "state",
        **service.state(record),
        "view_only": not viewer.controls,
        "viewport": {"width": width, "height": height},
    }


def _broadcast_state(service: Any, record: Any) -> None:
    hub = _HUBS.get(record.id)
    if hub is None:
        return
    for viewer in list(hub.viewers):
        _deliver(viewer, _state(service, record, hub, viewer))


def _style(accent: str) -> dict[str, str]:
    color = accent if accent.startswith("#") and len(accent) in (4, 7) else "#2563eb"
    return {
        "point": f"background: {color}; opacity: 0.85;",
        "highlight": f"outline: 2px solid {color}; outline-offset: 2px;",
        "title": f"font: 600 14px system-ui; background: {color}; color: white; padding: 4px 8px;",
    }


async def _start(service: Any, record: Any, hub: _Hub) -> None:
    """Start the screencast and the agent-action annotations (on the browser loop)."""

    def on_frame(frame: dict[str, Any]) -> None:
        size = (int(frame.get("viewportWidth") or 0), int(frame.get("viewportHeight") or 0))
        if size != hub.viewport:
            hub.viewport = size
            _broadcast_state(service, record)
        for viewer in list(hub.viewers):
            _deliver(viewer, bytes(frame["data"]))

    width = settings.frame_width()
    viewport = record.page.viewport_size or {"width": 1280, "height": 800}
    height = max(1, round(width * viewport["height"] / max(1, viewport["width"])))
    hub.screencast = await record.page.screencast.start(
        on_frame=on_frame, quality=settings.frame_quality(), size={"width": width, "height": height}
    )
    await _annotate(record, hub)


async def _annotate(record: Any, hub: _Hub) -> None:
    if hub.actions is not None:
        await hub.actions.__aexit__(None, None, None)
    hub.actions = await record.page.screencast.show_actions(
        style=_style(hub.theme.get("accent", "")), duration=600
    )


async def _stop(hub: _Hub) -> None:
    for handle in (hub.actions, hub.screencast):
        if handle is not None:
            try:
                await handle.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - the page closed first
                trace.event("BROWSER", "live stop skipped page=%s error=%s", hub.page_id, exc)
    hub.actions = hub.screencast = None


def _number(message: dict[str, Any], key: str) -> float:
    value = message.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    return float(value)


async def _play(service: Any, record: Any, hub: _Hub, message: dict[str, Any]) -> None:
    """Play one input message from the person on the page (on the browser loop)."""
    page = record.page
    kind = message.get("type")
    if kind == "theme":
        hub.theme = {"accent": str(message.get("accent") or "")}
        await _annotate(record, hub)
        return
    record.person_acted = True
    record.last_person_input = time.monotonic()
    if kind == "mouse":
        action = message.get("action")
        if action not in _MOUSE_ACTIONS:
            raise ValueError("mouse action is move, down, up or click")
        x, y = _number(message, "x"), _number(message, "y")
        button = (
            message.get("button")
            if message.get("button") in ("left", "right", "middle")
            else "left"
        )
        if action == "move":
            await page.mouse.move(x, y)
        elif action == "click":
            await page.mouse.click(x, y, button=button, click_count=int(message.get("count") or 1))
        else:
            await page.mouse.move(x, y)
            await (page.mouse.down if action == "down" else page.mouse.up)(button=button)
    elif kind == "wheel":
        await page.mouse.wheel(_number(message, "dx"), _number(message, "dy"))
    elif kind == "key":
        action, key = message.get("action"), str(message.get("key") or "")
        if action not in _KEY_ACTIONS or not key:
            raise ValueError("key needs an action (down, up, press) and a key")
        await getattr(page.keyboard, action)(key)
    elif kind == "text":
        await page.keyboard.insert_text(str(message.get("text") or ""))
    elif kind == "resize":
        width = min(2560, max(320, int(_number(message, "width"))))
        height = min(2560, max(240, int(_number(message, "height"))))
        await page.set_viewport_size({"width": width, "height": height})
    elif kind == "navigate":
        await _navigate(page, message)
    else:
        raise ValueError(f"unknown message type {kind!r}")


async def _navigate(page: Any, message: dict[str, Any]) -> None:
    action = message.get("action") or "goto"
    if action not in _NAVIGATIONS:
        raise ValueError("navigate action is goto, back, forward or reload")
    if action == "goto":
        url = str(message.get("url") or "").strip()
        if not url:
            raise ValueError("goto needs a url")
        if "://" not in url and not url.startswith(("about:", "data:")):
            url = f"https://{url}"
        await page.goto(url, wait_until="commit")
    elif action == "back":
        await page.go_back(wait_until="commit")
    elif action == "forward":
        await page.go_forward(wait_until="commit")
    else:
        await page.reload(wait_until="commit")


def _admitted(websocket: Any, app: Any) -> tuple[int, str] | None:
    """``None`` when the viewer may connect, else ``(close code, reason)``."""
    from clio_agent.gact.auth import supplied_bearer_token  # noqa: PLC0415
    from clio_agent.gact.origin_guard import browser_origin_allowed  # noqa: PLC0415

    origin = websocket.headers.get("origin")
    if origin is not None and not browser_origin_allowed(
        origin, websocket.scope, websocket.headers
    ):
        return 4403, "origin_not_allowed"
    expected = getattr(app.state, "bearer_token", None)
    if expected is not None and not hmac.compare_digest(
        supplied_bearer_token(websocket.scope), expected
    ):
        return 4401, "authentication_required"
    return None


def _in_family(app: Any, page_session: str, session_id: str) -> bool:
    """Whether ``page_session`` is ``session_id`` or one of its children (a subagent's page)."""
    current = page_session
    for _ in range(8):  # spawn depth is bounded well below this
        if current == session_id:
            return True
        session = app.state.sessions.get(current) if current else None
        current = str(getattr(session, "parent_session_id", "") or "")
        if not current:
            return False
    return False


def _offered(websocket: Any) -> bool:
    header = websocket.headers.get("sec-websocket-protocol", "")
    return LIVE_PROTOCOL in {part.strip() for part in header.split(",")}


async def _serve(app: Any, websocket: Any, session_id: str, page_id: str) -> None:
    from clio_agent.gact.browser.service import service  # noqa: PLC0415

    refusal = _admitted(websocket, app)
    svc = service()
    record = svc.pages.get(page_id)
    if refusal is None and (
        record is None or record.closed or not _in_family(app, record.session_id, session_id)
    ):
        refusal = (4404, "page_not_found")
    if refusal is not None:
        await websocket.accept(subprotocol=LIVE_PROTOCOL if _offered(websocket) else None)
        await websocket.close(code=refusal[0], reason=refusal[1])
        return
    await websocket.accept(subprotocol=LIVE_PROTOCOL if _offered(websocket) else None)
    svc.app = app
    hub = _HUBS.setdefault(page_id, _Hub(page_id))
    viewer = _Viewer(websocket, asyncio.get_running_loop(), controls=not hub.viewers)
    hub.viewers.append(viewer)
    try:
        if hub.screencast is None:
            await asyncio.wrap_future(svc.submit(_start(svc, record, hub)))
        await websocket.send_json(_state(svc, record, hub, viewer))
        sender = asyncio.create_task(_send(viewer))
        try:
            await _receive(svc, record, hub, viewer)
        finally:
            sender.cancel()
    finally:
        hub.viewers.remove(viewer)
        if hub.viewers and not any(v.controls for v in hub.viewers):
            hub.viewers[0].controls = True  # the next viewer takes over input
            _broadcast_state(svc, record)
        if not hub.viewers:
            _HUBS.pop(page_id, None)
            if not record.closed:
                await asyncio.wrap_future(svc.submit(_stop(hub)))


async def _send(viewer: _Viewer) -> None:
    while True:
        item = await viewer.queue.get()
        if isinstance(item, bytes):
            await viewer.socket.send_bytes(item)
        else:
            await viewer.socket.send_text(json.dumps(item, default=str))


async def _receive(svc: Any, record: Any, hub: _Hub, viewer: _Viewer) -> None:
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    while True:
        try:
            text = await viewer.socket.receive_text()
        except WebSocketDisconnect:
            return
        try:
            message = json.loads(text)
            if not isinstance(message, dict):
                raise ValueError("a message is a JSON object")
        except ValueError as exc:
            await viewer.socket.send_json(
                {"type": "error", "reason": "live_message_invalid", "detail": str(exc)}
            )
            continue
        if not viewer.controls and message.get("type") != "theme":
            await viewer.socket.send_json({"type": "ignored", "reason": "view_only"})
            continue
        try:
            await asyncio.wrap_future(svc.submit(_play(svc, record, hub, message)))
        except Exception as exc:  # noqa: BLE001 - one bad input never ends the view
            await viewer.socket.send_json(
                {"type": "error", "reason": "live_input_failed", "detail": str(exc)[:300]}
            )


def register_browser_routes(app: Any) -> None:
    """The live view, the session's pages, and allowing a blocked host."""
    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.browser.service import PAGE_UPDATED, service  # noqa: PLC0415

    service().listeners.append(lambda kind, record: _broadcast_state(service(), record))

    @app.websocket("/v1/sessions/{session_id}/browser/{page_id}/live")
    async def browser_live(websocket: WebSocket, session_id: str, page_id: str) -> None:
        await _serve(app, websocket, session_id, page_id)

    @app.get("/v1/sessions/{session_id}/browser/pages")
    async def browser_pages(session_id: str) -> dict[str, Any]:
        """The conversation's pages and its subagents' (labelled by their task)."""
        svc = service()
        records = [r for r in list(svc.pages.values()) if not r.closed]
        return {
            "pages": [svc.state(r) for r in records if _in_family(app, r.session_id, session_id)]
        }

    @app.post("/v1/sessions/{session_id}/browser/pages/{page_id}/allow-host")
    async def browser_allow_host(session_id: str, page_id: str) -> dict[str, Any]:
        from clio_agent.gact.browser.host_gate import allow_blocked  # noqa: PLC0415

        svc = service()
        record = svc.pages.get(page_id)
        if record is None or record.closed or not _in_family(app, record.session_id, session_id):
            raise HTTPException(status_code=404, detail="browser page not found")
        svc.app = app
        host = await asyncio.to_thread(allow_blocked, svc, record)
        if not host:
            raise HTTPException(status_code=409, detail="this page has no blocked host to allow")
        svc.publish(PAGE_UPDATED, record)
        return {"allowed": host, "page": svc.state(record)}
