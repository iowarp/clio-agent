"""The browser tools: ten CLIO built-ins a ``run_code`` script reaches through ``tools.*``.

The ``browser`` plugin claims them with ``callers="code"`` (BR7), so their schemas never
enter the prompt; the kernel's ``browser`` binding wraps them in a Playwright-shaped
``page`` object. Each call runs through the agent's instrumented tool path like any other:
the permission gate, the observer, nesting under ``run_code``, cancellation and provenance
all apply. Every result carries the page (``id``, ``url``, ``title``, ``host``),
``person_acted`` (the person navigated or sent input to the page since the agent's
previous call on it), ``navigation_blocked`` when the page was refused a host meanwhile,
and the downloads the call produced.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from clio_agent.gact.browser import files
from clio_agent.gact.browser.service import PageRecord, service
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.gact.browser.targets import describe, locator_for

__all__ = ["BROWSER_TOOLS", "BUILDERS"]

#: Every tool name the ``browser`` plugin may claim.
BROWSER_TOOLS: tuple[str, ...] = (
    "browser_open",
    "browser_pages",
    "browser_close",
    "browser_navigate",
    "browser_snapshot",
    "browser_read",
    "browser_act",
    "browser_wait",
    "browser_screenshot",
    "browser_evaluate",
)
_ACTIONS = (
    "click",
    "dblclick",
    "hover",
    "fill",
    "type",
    "press",
    "select",
    "check",
    "uncheck",
    "upload",
    "drag_to",
    "scroll",
    "mouse_click",
    "key",
)
_READS = ("text", "inner_html", "value", "attribute", "count", "box", "visible", "checked")
_WAITS = ("load", "url", "target", "gone", "idle")
_NAVIGATIONS = ("goto", "back", "forward", "reload")
_LOAD_STATES = ("load", "domcontentloaded", "networkidle", "commit")
#: Playwright action options a script may pass through ``options``.
_ACT_OPTIONS = frozenset({"button", "click_count", "delay", "modifiers", "force", "position"})
_PASSWORD_CHECK = "e => !!e && (e.type === 'password' || /password/i.test(e.autocomplete || ''))"


def _session() -> tuple[Any, str]:
    from clio_agent.gact import context  # noqa: PLC0415

    app, session_id = context.active_app(), context.active_session_id() or ""
    if not session_id:
        raise BrowserError("browser_no_session", "browser tools run inside a conversation")
    svc = service()
    if app is not None:
        svc.app = app
    return app, session_id


def _record(page: str) -> tuple[Any, PageRecord]:
    app, session_id = _session()
    return app, service().owned(session_id, str(page))


def page_info(record: PageRecord) -> dict[str, Any]:
    return {"id": record.id, "url": record.url(), "title": record.title, "host": record.host()}


def _result(app: Any, record: PageRecord, **extra: Any) -> dict[str, Any]:
    notes = service().take_notes(record)
    for download in notes.get("downloads", ()):
        if download.get("path"):
            from pathlib import Path  # noqa: PLC0415

            download.update(
                files.mint(
                    app,
                    record.session_id,
                    Path(download["path"]),
                    image=False,
                    page_url=record.url(),
                )
            )
    return {"page": page_info(record), **notes, **extra}


def _choice(value: Any, allowed: tuple[str, ...], name: str) -> str:
    text = str(value or "")
    if text not in allowed:
        raise BrowserError("browser_argument_invalid", f"{name} is one of {', '.join(allowed)}")
    return text


# ---- the tools ---------------------------------------------------------------------------
def browser_open(url: str | None = None, label: str = "") -> dict[str, Any]:
    """Open a page for this conversation, loading ``url`` when given; returns its id."""
    app, session_id = _session()
    session = app.state.sessions.get(session_id) if app is not None else None
    name = label or str(getattr(session, "title", "") or "")
    svc = service()
    same = [page_info(r) for r in svc.session_pages(session_id) if url and _same_url(r, url)]
    record = svc.wait(svc.open(session_id, name))
    if url:
        svc.run(
            record, f"open {url}", lambda r: r.page.goto(url, wait_until="load"), navigates=True
        )
    extra = {"already_open": same} if same else {}
    return _result(app, record, opened=True, label=record.label, **extra)


def _same_url(record: PageRecord, url: str) -> bool:
    """Whether an open page already shows ``url`` (fragment aside): the new page is a fresh
    load, while that one keeps what the person and earlier scripts did on it."""
    current = record.url()
    return current.split("#", 1)[0] == url.split("#", 1)[0]


def browser_pages() -> dict[str, Any]:
    """The conversation's open pages."""
    _app, session_id = _session()
    svc = service()
    return {"pages": [svc.state(r) for r in svc.session_pages(session_id)]}


def browser_close(page: str) -> dict[str, Any]:
    """Close one page."""
    _app, record = _record(page)
    info = page_info(record)
    service().close_page(record)
    return {"page": info, "closed": True}


def browser_navigate(
    page: str, url: str | None = None, action: str = "goto", wait_until: str = "load"
) -> dict[str, Any]:
    """Go to ``url``, or go back, forward or reload."""
    app, record = _record(page)
    action = _choice(action, _NAVIGATIONS, "action")
    wait_until = _choice(wait_until, _LOAD_STATES, "wait_until")
    if action == "goto" and not url:
        raise BrowserError("browser_argument_invalid", "goto needs a url")

    async def go(r: PageRecord) -> int | None:
        if action == "goto":
            response = await r.page.goto(str(url), wait_until=wait_until)
        elif action == "back":
            response = await r.page.go_back(wait_until=wait_until)
        elif action == "forward":
            response = await r.page.go_forward(wait_until=wait_until)
        else:
            response = await r.page.reload(wait_until=wait_until)
        return response.status if response is not None else None

    status = service().run(record, f"{action} {url or ''}".strip(), go, navigates=True)
    return _result(app, record, status=status)


def browser_snapshot(page: str, target: Any = None, boxes: bool = False) -> dict[str, Any]:
    """The page's accessibility tree with refs (``ref=f9e14``), as text."""
    app, record = _record(page)

    async def snap(r: PageRecord) -> str:
        root = locator_for(r.page, target) if target else r.page.locator("body")
        return await root.aria_snapshot(mode="ai", boxes=bool(boxes))

    text = service().run(record, "snapshot", snap)
    return _result(app, record, snapshot=text, lines=len(text.splitlines()))


def browser_read(page: str, target: Any, what: str, name: str | None = None) -> dict[str, Any]:
    """Read from the element ``target`` names: its text, value, an attribute, count..."""
    app, record = _record(page)
    what = _choice(what, _READS, "what")
    if what == "attribute" and not name:
        raise BrowserError("browser_argument_invalid", "reading an attribute needs its name")

    async def read(r: PageRecord) -> Any:
        locator = locator_for(r.page, target)
        if what == "text":
            return await locator.inner_text()
        if what == "inner_html":
            return await locator.inner_html()
        if what == "value":
            return await locator.input_value()
        if what == "attribute":
            return await locator.get_attribute(str(name))
        if what == "count":
            return await locator.count()
        if what == "box":
            return await locator.bounding_box()
        if what == "visible":
            return await locator.is_visible()
        return await locator.is_checked()

    value = service().run(record, f"read {what} {describe(target)}".strip(), read)
    return _result(app, record, what=what, value=value)


async def _refuse_password(locator: Any) -> None:
    if await locator.evaluate(_PASSWORD_CHECK):
        raise BrowserError(
            "browser_password_field",
            "the agent never types into a password field; ask the person to sign in "
            "in the shared browser tab, then continue on the signed-in page",
        )


async def _refuse_focused_password(page: Any) -> None:
    if await page.evaluate(f"({_PASSWORD_CHECK})(document.activeElement)"):
        await _refuse_password(page.locator(":focus"))


def _uploads(value: Any) -> list[str]:
    from clio_agent.tools.file_policy import validate_read_path  # noqa: PLC0415

    paths = value if isinstance(value, list) else [value]
    if not paths or not all(isinstance(p, str) and p for p in paths):
        raise BrowserError("browser_argument_invalid", "upload takes a path or a list of paths")
    checked = []
    for path in paths:
        try:
            checked.append(str(validate_read_path(path, field="value", loads_whole_file=True)))
        except Exception as exc:  # noqa: BLE001 - the file policy's own refusal, typed
            raise BrowserError("browser_upload_refused", str(exc)) from exc
    return checked


def _act_work(
    target: Any, action: str, value: Any, x: Any, y: Any, options: Mapping[str, Any]
) -> Callable[[PageRecord], Any]:
    unknown = sorted(set(options) - _ACT_OPTIONS)
    if unknown:
        raise BrowserError("browser_argument_invalid", f"unknown options {unknown}")
    if action in {"mouse_click", "scroll"} and target is None and (x is None or y is None):
        raise BrowserError("browser_argument_invalid", f"{action} needs a target or x and y")
    files_to_upload = _uploads(value) if action == "upload" else []

    async def act(r: PageRecord) -> None:
        page = r.page
        if action == "mouse_click":
            await page.mouse.click(float(x), float(y), **options)
            return
        if action == "key":
            await _refuse_focused_password(page)
            await page.keyboard.press(str(value))
            return
        if action == "type" and target is None:  # into the focused element
            await _refuse_focused_password(page)
            await page.keyboard.type(str(value or ""))
            return
        if action == "scroll" and target is None:
            await page.mouse.wheel(float(x), float(y))
            return
        if target is None:
            raise BrowserError("browser_argument_invalid", f"{action} needs a target")
        locator = locator_for(page, target)
        if action in {"fill", "type", "press"}:
            await _refuse_password(locator)
        if action == "fill":
            await locator.fill(str(value if value is not None else ""))
        elif action == "type":
            await locator.press_sequentially(str(value or ""), **options)
        elif action == "press":
            await locator.press(str(value))
        elif action == "select":
            await locator.select_option(value)
        elif action == "check":
            await locator.check(**options)
        elif action == "uncheck":
            await locator.uncheck(**options)
        elif action == "upload":
            await locator.set_input_files(files_to_upload)
        elif action == "drag_to":
            await locator.drag_to(locator_for(page, value))
        elif action == "scroll":
            await locator.scroll_into_view_if_needed()
        else:
            await getattr(locator, action)(**options)

    return act


def browser_act(
    page: str,
    action: str,
    target: Any = None,
    value: Any = None,
    x: float | None = None,
    y: float | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Act on the page: click, fill, type, press, select, check, upload, scroll, key..."""
    app, record = _record(page)
    action = _choice(action, _ACTIONS, "action")
    work = _act_work(target, action, value, x, y, dict(options or {}))
    service().run(record, f"{action} {describe(target)}".strip(), work)
    return _result(app, record, action=action)


def browser_wait(
    page: str,
    until: str,
    value: str | None = None,
    target: Any = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Wait for the page to load, reach a URL, or for an element to appear or go."""
    app, record = _record(page)
    until = _choice(until, _WAITS, "until")
    timeout_ms = float(timeout_s) * 1000 if timeout_s else 0

    async def wait(r: PageRecord) -> None:
        if until == "load":
            await r.page.wait_for_load_state(
                _choice(value or "load", _LOAD_STATES, "value"), timeout=timeout_ms
            )
        elif until == "idle":
            await r.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        elif until == "url":
            if not value:
                raise BrowserError("browser_argument_invalid", "waiting for a url needs value")
            await r.page.wait_for_url(value, timeout=timeout_ms)
        else:
            if target is None:
                raise BrowserError("browser_argument_invalid", f"waiting {until} needs a target")
            state = "visible" if until == "target" else "hidden"
            await locator_for(r.page, target).wait_for(state=state, timeout=timeout_ms)

    service().run(record, f"wait {until}", wait, limit_s=timeout_s)
    return _result(app, record, until=until)


def browser_screenshot(page: str, target: Any = None, full_page: bool = False) -> dict[str, Any]:
    """Write a PNG of the page (or one element) to scratch and return its path."""
    app, record = _record(page)
    path = files.screenshot_path(record.session_id, record.id, record.host())

    async def shoot(r: PageRecord) -> None:
        if target is not None:
            await locator_for(r.page, target).screenshot(path=str(path))
        else:
            await r.page.screenshot(path=str(path), full_page=bool(full_page))

    service().run(record, "screenshot", shoot)
    import hashlib  # noqa: PLC0415

    data = path.read_bytes()
    shot = {
        "path": str(path),
        "media_type": "image/png",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **files.mint(app, record.session_id, path, image=True, page_url=record.url()),
    }
    return _result(app, record, screenshot=shot)


def browser_evaluate(page: str, expression: str, arg: Any = None) -> dict[str, Any]:
    """Run JavaScript in the page; returns its JSON value."""
    app, record = _record(page)
    value = service().run(record, "evaluate", lambda r: r.page.evaluate(str(expression), arg))
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise BrowserError("browser_value_not_json", f"the value is not JSON: {exc}") from exc
    return _result(app, record, value=value)


# ---- declarations ------------------------------------------------------------------------
_TARGET = {
    "anyOf": [{"type": "object"}, {"type": "null"}],
    "default": None,
    "description": "A locator spec: {steps: [{by: role|text|label|ref|css|..., ...}], frame?}.",
}
_PAGE = {"type": "string", "description": "The page id, such as p2."}


def _native(func: Callable[..., Any], *, read_only: bool, title: str, args: dict[str, Any]) -> Any:
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415
    from clio_agent.gact.browser.presentation import presenter_for, starting_for  # noqa: PLC0415
    from clio_agent.gact.code_mode.dispatch import CONCURRENCY_GROUP_ATTR  # noqa: PLC0415

    # One concurrency group for every browser tool; calls on one page are serialized by the
    # service, calls on different pages run at once (plan, "Concurrency").
    setattr(func, CONCURRENCY_GROUP_ATTR, "browser")
    return native_tool(
        func,
        name=func.__name__,
        desc=(func.__doc__ or "").strip(),
        args=args,
        presentation=presenter_for(func.__name__),
        presentation_start=starting_for(func.__name__),
        domain="browser",
        title=title,
        read_only=read_only,
        callers="code",
    )


def _builder(
    func: Callable[..., Any], title: str, read_only: bool, **args: Any
) -> Callable[[], Any]:
    return lambda: _native(func, read_only=read_only, title=title, args=args)


BUILDERS: dict[str, Callable[[], Any]] = {
    "browser_open": _builder(
        browser_open,
        "Open page",
        False,
        url={"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        label={"type": "string", "default": ""},
    ),
    "browser_pages": _builder(browser_pages, "List pages", True),
    "browser_close": _builder(browser_close, "Close page", False, page=_PAGE),
    "browser_navigate": _builder(
        browser_navigate,
        "Navigate",
        False,
        page=_PAGE,
        url={"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        action={"type": "string", "enum": list(_NAVIGATIONS), "default": "goto"},
        wait_until={"type": "string", "enum": list(_LOAD_STATES), "default": "load"},
    ),
    "browser_snapshot": _builder(
        browser_snapshot,
        "Read page structure",
        True,
        page=_PAGE,
        target=_TARGET,
        boxes={"type": "boolean", "default": False},
    ),
    "browser_read": _builder(
        browser_read,
        "Read from page",
        True,
        page=_PAGE,
        target={"type": "object"},
        what={"type": "string", "enum": list(_READS)},
        name={"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
    ),
    "browser_act": _builder(
        browser_act,
        "Act on page",
        False,
        page=_PAGE,
        action={"type": "string", "enum": list(_ACTIONS)},
        target=_TARGET,
        value={"default": None},
        x={"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
        y={"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
        options={"anyOf": [{"type": "object"}, {"type": "null"}], "default": None},
    ),
    "browser_wait": _builder(
        browser_wait,
        "Wait for page",
        True,
        page=_PAGE,
        until={"type": "string", "enum": list(_WAITS)},
        value={"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        target=_TARGET,
        timeout_s={"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
    ),
    "browser_screenshot": _builder(
        browser_screenshot,
        "Screenshot page",
        True,
        page=_PAGE,
        target=_TARGET,
        full_page={"type": "boolean", "default": False},
    ),
    "browser_evaluate": _builder(
        browser_evaluate,
        "Run page script",
        False,
        page=_PAGE,
        expression={"type": "string"},
        arg={"default": None},
    ),
}
