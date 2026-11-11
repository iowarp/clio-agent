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
surface and the Work rail), ``POST .../pages/{page_id}/allow-host`` (the person allows a
host the page was blocked from, as their own grant; since BR42 a page is blocked only for
a refused scheme or a gate error, neither of which names a host to allow, so the route
allows nothing today) and ``POST .../trust-certificate``
(the person trusts the certificate a page warns about, for the conversation or workspace).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from fastapi import WebSocket

from clio_agent.gact.browser import picker, settings
from clio_agent.gact.browser.settings import BrowserError, page_errors
from clio_agent.runtime import trace

__all__ = ["LIVE_PROTOCOL", "pause_view", "register_browser_routes", "resume_view"]

LIVE_PROTOCOL = "clio.browser.v1"
#: Replies (errors, ``ignored``) a viewer may have waiting; past it the oldest go.
_REPLY_BACKLOG = 64
#: Close code for a viewer whose page closed.
PAGE_CLOSED_CODE = 4410
_MOUSE_ACTIONS = ("move", "down", "up", "click")
_KEY_ACTIONS = ("down", "up", "press")
_NAVIGATIONS = ("goto", "back", "forward", "reload")


@dataclass
class _Viewer:
    """One viewer. Only its sender writes to its socket (#1918): what it is sent waits here,
    bounded: the latest frame, the latest state (a state is the whole page state, so
    the latest replaces the rest), and a few replies to its own messages."""

    socket: Any
    loop: asyncio.AbstractEventLoop
    controls: bool = False
    frame: bytes | None = None
    state: dict[str, Any] | None = None
    replies: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=_REPLY_BACKLOG))
    #: The page closed: the sender sends what is waiting, then closes the socket.
    closing: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Hub:
    """The viewers of one page and its running screencast."""

    page_id: str
    viewers: list[_Viewer] = field(default_factory=list)
    screencast: Any = None
    actions: Any = None
    viewport: tuple[int, int] = (0, 0)
    theme: dict[str, str] = field(default_factory=dict)
    #: The latest frame, for a viewer who joins a page that is not changing.
    last_frame: bytes = b""
    #: The screencast's start while it runs (on the server's loop): a viewer who joins
    #: then waits for it rather than starting a second screencast.
    starting: Any = None
    #: A start is under way on the browser loop: a second ``_start`` returns at once.
    beginning: bool = False


_HUBS: dict[str, _Hub] = {}


def _put(viewer: _Viewer, item: Any) -> None:
    """Hold ``item`` for the viewer's sender (on the viewer's loop): a frame or a state
    replaces the one waiting, so a burst of frames never pushes a state out."""
    if isinstance(item, bytes):
        viewer.frame = item
    elif isinstance(item, dict) and item.get("type") == "state":
        viewer.state = item
    else:
        viewer.replies.append(item)
    viewer.wake.set()


def _deliver(viewer: _Viewer, item: Any) -> None:
    """Hold ``item`` for one viewer, from any thread."""
    viewer.loop.call_soon_threadsafe(_put, viewer, item)


def _close_viewers(page_id: str) -> None:
    """The page closed: each viewer gets its last state, then its socket is closed."""
    hub = _HUBS.get(page_id)
    for viewer in list(hub.viewers if hub is not None else ()):

        def close(viewer: _Viewer = viewer) -> None:
            viewer.closing = True
            viewer.wake.set()

        viewer.loop.call_soon_threadsafe(close)


def _deliver_controllers(hub: _Hub, item: Any) -> None:
    """Queue ``item`` for the viewers that send input to the page (the picker's results)."""
    for viewer in list(hub.viewers):
        if viewer.controls:
            _deliver(viewer, item)


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

    if hub.screencast is not None or hub.beginning:  # one screencast per page
        return
    hub.beginning = True  # set before the first await: _start runs on the browser loop
    try:
        await _begin(service, record, hub)
    finally:
        hub.beginning = False


async def _begin(service: Any, record: Any, hub: _Hub) -> None:
    def on_frame(frame: dict[str, Any]) -> None:
        size = (int(frame.get("viewportWidth") or 0), int(frame.get("viewportHeight") or 0))
        if size != hub.viewport:
            hub.viewport = size
            _broadcast_state(service, record)
        hub.last_frame = bytes(frame["data"])
        for viewer in list(hub.viewers):
            _deliver(viewer, hub.last_frame)

    viewport = record.page.viewport_size or {"width": 1280, "height": 800}
    width = min(settings.frame_width(), int(viewport["width"]))  # never upscaled
    height = max(1, round(width * viewport["height"] / max(1, viewport["width"])))
    hub.screencast = await record.page.screencast.start(
        on_frame=on_frame, quality=settings.frame_quality(), size={"width": width, "height": height}
    )
    if not hub.last_frame:
        # The screencast sends frames only when the page changes; a still page would
        # otherwise show nothing, so the first frame is a still of the page as it is.
        hub.viewport = (int(viewport["width"]), int(viewport["height"]))
        still = await record.page.screenshot(type="jpeg", quality=settings.frame_quality())
        if not hub.last_frame:
            hub.last_frame = still
        _broadcast_state(service, record)
        for viewer in list(hub.viewers):
            _deliver(viewer, hub.last_frame)


async def _annotate(record: Any, hub: _Hub) -> None:
    await _hide_actions(hub)
    hub.actions = await record.page.screencast.show_actions(
        style=_style(hub.theme.get("accent", "")), duration=600
    )


async def _hide_actions(hub: _Hub) -> None:
    if hub.actions is not None:
        actions, hub.actions = hub.actions, None
        await actions.__aexit__(None, None, None)


async def _agent_activity(record: Any, active: bool) -> None:
    """Annotate the agent's actions only while its call runs on a watched page.

    Playwright annotates every action made through its API, the person's input played
    back here included, and an annotated pointer action waits for its animation; turned
    on for the person's input, it made typed text land before the click that focused
    the field. So the annotations follow the agent's calls, which is what they show.
    """
    hub = _HUBS.get(record.id)
    if hub is None or hub.screencast is None:
        return
    if active:
        await _annotate(record, hub)
    else:
        await _hide_actions(hub)


async def _stop(hub: _Hub) -> None:
    for handle in (hub.actions, hub.screencast):
        if handle is not None:
            try:
                await handle.__aexit__(None, None, None)
            except page_errors() as exc:  # the page closed first
                trace.event("BROWSER", "live stop skipped page=%s error=%s", hub.page_id, exc)
    hub.actions = hub.screencast = None


async def pause_view(record: Any) -> None:
    """Stop a page's screencast before its browser relaunches (its viewers stay)."""
    hub = _HUBS.get(record.id)
    if hub is not None:
        await _stop(hub)


async def resume_view(service: Any, record: Any) -> None:
    """Start the screencast again on the page's new browser page, for its viewers."""
    hub = _HUBS.get(record.id)
    if hub is None or not hub.viewers:
        return
    hub.last_frame = b""
    await _start(service, record, hub)
    _broadcast_state(service, record)


async def _resize(service: Any, record: Any, hub: _Hub, message: dict[str, Any]) -> None:
    """Lay the page out at the controlling view's size, as a browser window would, so a
    page reflows in a narrow panel instead of shrinking a wide frame into it."""
    width = min(2560, max(320, int(_number(message, "width"))))
    height = min(2560, max(240, int(_number(message, "height"))))
    current = record.page.viewport_size or {}
    if (current.get("width"), current.get("height")) == (width, height):
        return
    await record.page.set_viewport_size({"width": width, "height": height})
    await _stop(hub)  # the screencast keeps the size it started with
    hub.last_frame = b""
    await _start(service, record, hub)
    if record.inflight is not None:
        await _annotate(record, hub)


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
        return
    if kind == "resize":  # the view's size, not something the person did on the page
        await _resize(service, record, hub, message)
        return
    if await picker.handle(record, message, partial(_deliver_controllers, hub)):
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
    assert record is not None  # a missing page was refused above
    await websocket.accept(subprotocol=LIVE_PROTOCOL if _offered(websocket) else None)
    svc.app = app
    hub = _HUBS.setdefault(page_id, _Hub(page_id))
    viewer = _Viewer(websocket, asyncio.get_running_loop(), controls=not hub.viewers)
    hub.viewers.append(viewer)
    record.viewers += 1
    try:
        if hub.screencast is None:
            await _started(svc, record, hub)
        if hub.last_frame:
            _deliver(viewer, hub.last_frame)
        from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

        try:
            await websocket.send_json(_state(svc, record, hub, viewer))
        except WebSocketDisconnect:  # the viewer left before its first state (a remount)
            return
        sender = asyncio.create_task(_send(viewer))
        try:
            await _receive(svc, record, hub, viewer)
        finally:
            sender.cancel()
    finally:
        hub.viewers.remove(viewer)
        record.viewers -= 1
        if viewer.controls:  # a pick it started would keep taking the page's clicks
            await asyncio.wrap_future(svc.submit(picker.release(record)))
        record.last_used = time.monotonic()
        if hub.viewers and not any(v.controls for v in hub.viewers):
            hub.viewers[0].controls = True  # the next viewer takes over input
            _broadcast_state(svc, record)
        if not hub.viewers:
            # The hub stays registered until its screencast has stopped, so a viewer that
            # joins meanwhile (a canvas remount) joins it rather than starting a second
            # screencast on the page; if one did join, the screencast starts again for it.
            if not record.closed:
                await asyncio.wrap_future(svc.submit(_stop(hub)))
            if hub.viewers and not record.closed:
                await _started(svc, record, hub)
            elif not hub.viewers and _HUBS.get(page_id) is hub:
                _HUBS.pop(page_id, None)


async def _started(svc: Any, record: Any, hub: _Hub) -> None:
    """Start the page's screencast once: viewers who connect while it starts wait for the
    same start (a viewer leaving does not cancel it for the others)."""
    if hub.starting is None:
        hub.starting = asyncio.wrap_future(svc.submit(_start(svc, record, hub)))
        hub.starting.add_done_callback(lambda _done: setattr(hub, "starting", None))
    await asyncio.shield(hub.starting)


async def _send(viewer: _Viewer) -> None:
    """The viewer's only writer: replies, then the latest state, then the latest frame."""
    while True:
        await viewer.wake.wait()
        viewer.wake.clear()
        while viewer.replies:
            await viewer.socket.send_text(json.dumps(viewer.replies.popleft(), default=str))
        state, viewer.state = viewer.state, None
        if state is not None:
            await viewer.socket.send_text(json.dumps(state, default=str))
        frame, viewer.frame = viewer.frame, None
        if frame is not None and not viewer.closing:
            await viewer.socket.send_bytes(frame)
        if viewer.closing:
            await viewer.socket.close(code=PAGE_CLOSED_CODE, reason="page_closed")
            return


async def _receive(svc: Any, record: Any, hub: _Hub, viewer: _Viewer) -> None:
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    while True:
        try:
            text = await viewer.socket.receive_text()
        except (WebSocketDisconnect, RuntimeError):  # closed; Starlette raises either
            return
        try:
            message = json.loads(text)
            if not isinstance(message, dict):
                raise ValueError("a message is a JSON object")
        except ValueError as exc:
            _put(viewer, {"type": "error", "reason": "live_message_invalid", "detail": str(exc)})
            continue
        if not viewer.controls and message.get("type") != "theme":
            _put(viewer, {"type": "ignored", "reason": "view_only"})
            continue
        try:
            await asyncio.wrap_future(svc.submit(_play(svc, record, hub, message)))
        except Exception as exc:  # noqa: BLE001 - forwards: to the viewer as live_input_failed
            _put(viewer, {"type": "error", "reason": "live_input_failed", "detail": str(exc)[:300]})


def _on_page_change(kind: str, record: Any) -> None:
    from clio_agent.gact.browser.service import service  # noqa: PLC0415

    _broadcast_state(service(), record)
    if getattr(record, "closed", False):
        _close_viewers(record.id)  # their last state says closed; nothing more will come


def _open_for_person(svc: Any, session_id: str, url: str) -> Any:
    """The conversation's page already on ``url``, or a new one the person navigates there
    (their navigation: its host is granted for the conversation)."""
    for record in svc.session_pages(session_id):
        if record.url().split("#", 1)[0] == url.split("#", 1)[0]:
            if url.lower().startswith("file:"):  # opened again: show the file as it is now
                record.last_person_input = time.monotonic()
                svc.submit(record.page.reload())
            return record
    record = svc.wait(svc.open(session_id, ""))
    record.last_person_input = time.monotonic()
    svc.submit(record.page.goto(url))
    return record


def register_browser_routes(app: Any) -> None:
    """The live view, the session's pages, and allowing a blocked host."""
    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.browser.service import PAGE_UPDATED, service  # noqa: PLC0415

    svc = service()
    if _on_page_change not in svc.listeners:
        svc.listeners.append(_on_page_change)
    if _agent_activity not in svc.agent_hooks:
        svc.agent_hooks.append(_agent_activity)

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

    @app.post("/v1/sessions/{session_id}/browser/pages")
    async def browser_open_for_person(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Open a link the person clicked in the conversation, as their own navigation."""
        url = str(body.get("url") or "")
        if not url and (body.get("path") or body.get("artifact_id")):
            from clio_agent.gact.browser.file_pages import url_for  # noqa: PLC0415

            try:  # a workspace file or an artifact's file, opened as a local page
                url = await asyncio.to_thread(
                    lambda: url_for(
                        app,
                        session_id,
                        path=str(body.get("path") or ""),
                        artifact_id=str(body.get("artifact_id") or ""),
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https", "file"):
            raise HTTPException(status_code=422, detail="only web pages and local files open")
        if app.state.sessions.get(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        if scheme == "file":
            from clio_agent.gact.browser.file_pages import refusal  # noqa: PLC0415

            reason = await asyncio.to_thread(refusal, app, session_id, url)
            if reason:
                raise HTTPException(
                    status_code=422,
                    detail=f"{reason}: the file is outside what this conversation may read",
                )
        svc = service()
        svc.app = app
        try:
            record = await asyncio.to_thread(_open_for_person, svc, session_id, url)
        except BrowserError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"page": svc.state(record)}

    @app.post("/v1/sessions/{session_id}/browser/pages/{page_id}/trust-certificate")
    async def browser_trust_certificate(
        session_id: str, page_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """The person trusts the certificate the page warns about (browser addendum B)."""
        from clio_agent.gact.browser.certificates import trust  # noqa: PLC0415

        scope = str(body.get("scope") or "session")
        if scope not in ("session", "workspace"):
            raise HTTPException(status_code=422, detail="scope is session or workspace")
        svc = service()
        record = svc.pages.get(page_id)
        if record is None or record.closed or not _in_family(app, record.session_id, session_id):
            raise HTTPException(status_code=404, detail="browser page not found")
        svc.app = app
        try:
            return await asyncio.to_thread(trust, svc, record, scope)
        except BrowserError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc

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
