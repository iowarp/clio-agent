"""Chrome DevTools Protocol (CDP) client for the installed CLIO desktop's
embedded WebView2 shell (#B11, ``scripts/live_verification/desktop_lifecycle_proof.py``).

The desktop app is launched with ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=
--remote-debugging-port=<port>`` (see the launching script), which makes the
embedded WebView2 runtime expose the same debugging surface as headless
Chrome: an HTTP ``/json`` target list and, per page target, a CDP websocket.
This module is a synchronous, dependency-light client over that surface --
``websockets.sync.client`` (no asyncio event loop needed in an otherwise
synchronous script) plus a handful of ``Runtime.evaluate`` JS snippets that
stand in for a real mouse click / key press on the gact-tui shell (the custom
title bar's ``[aria-label="Close"]`` button, the "Keep CLIO running?"
AlertDialog, and its "Keep running" / "Quit CLIO" buttons).

Standalone module: no sibling flat-imports, so it loads cleanly under
``importlib`` with no ``sys.path`` surgery (unlike
``desktop_lifecycle_proof.py``, which flat-imports this module and ``_common``
as siblings, following the package's existing convention -- see
``_common.py``'s own ``sys.path.insert`` + ``import _common as common``).
"""

from __future__ import annotations

import base64
import itertools
import json
import sys
import time
from typing import Any, Protocol

import requests


class CDPError(RuntimeError):
    """A CDP call failed, timed out, or the page raised a JS exception."""


class SupportsEvaluate(Protocol):
    """Structural type for the JS-snippet helpers below (``Runtime.evaluate`` only).

    ``CDPSession`` satisfies this structurally; so does any test double
    exposing a matching ``evaluate`` -- these helpers never need the whole
    session, just this one call, so they accept the narrower shape (and a
    fake session used in a test is never forced to also fake ``send``/
    ``capture_screenshot_png``/... just to satisfy a type check).
    """

    def evaluate(self, expression: str, *, timeout: float = ...) -> Any: ...


def list_targets(
    debug_port: int, *, host: str = "127.0.0.1", timeout: float = 5.0
) -> list[dict[str, Any]]:
    """``GET http://host:debug_port/json`` -> the raw WebView2/Chrome target list."""

    resp = requests.get(f"http://{host}:{debug_port}/json", timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def find_page_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first ``type == "page"`` target carrying a websocket debugger URL.

    Pure filtering over an already-fetched target list -- unit-tested
    directly; :func:`list_targets` is the only network-touching half.
    """

    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target
    return None


class CDPSession:
    """One synchronous CDP session over a page target's websocket.

    Correlates replies to requests by an incrementing ``id`` (the CDP
    JSON-RPC-shaped protocol); unrelated frames (Page/Runtime/Target events)
    are drained into :attr:`events` rather than discarded, in case a caller
    wants them as extra evidence.
    """

    def __init__(self, ws_url: str, *, open_timeout: float = 10.0) -> None:
        import websockets.sync.client as ws_client  # noqa: PLC0415 - optional, CDP-only dep

        self._ws = ws_client.connect(ws_url, open_timeout=open_timeout, max_size=None)
        self._next_id = itertools.count(1)
        self.events: list[dict[str, Any]] = []

    def send(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        """Send one CDP command and block for its correlated reply."""

        msg_id = next(self._next_id)
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPError(f"{method} timed out after {timeout:g}s")
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError as exc:
                raise CDPError(f"{method} timed out after {timeout:g}s") from exc
            frame = json.loads(raw)
            if frame.get("id") == msg_id:
                if "error" in frame:
                    raise CDPError(f"{method} -> {frame['error']}")
                return dict(frame.get("result") or {})
            self.events.append(frame)

    def evaluate(self, expression: str, *, timeout: float = 15.0) -> Any:
        """``Runtime.evaluate`` ``expression`` and return its JS value."""

        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        exception = result.get("exceptionDetails")
        if exception:
            raise CDPError(f"Runtime.evaluate raised: {exception}")
        return result.get("result", {}).get("value")

    def capture_screenshot_png(self, *, timeout: float = 15.0) -> bytes:
        """``Page.captureScreenshot`` -> raw PNG bytes."""

        result = self.send("Page.captureScreenshot", {"format": "png"}, timeout=timeout)
        return base64.b64decode(result["data"])

    def press_key(
        self, key: str, *, code: str, windows_virtual_key_code: int, timeout: float = 10.0
    ) -> None:
        """``Input.dispatchKeyEvent`` a keyDown+keyUp pair for one key (e.g. Escape)."""

        for event_type in ("keyDown", "keyUp"):
            self.send(
                "Input.dispatchKeyEvent",
                {
                    "type": event_type,
                    "key": key,
                    "code": code,
                    "windowsVirtualKeyCode": windows_virtual_key_code,
                    "nativeVirtualKeyCode": windows_virtual_key_code,
                },
                timeout=timeout,
            )

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - best-effort teardown must never raise
            pass

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


#: Escape's Windows virtual-key code (VK_ESCAPE), for :meth:`CDPSession.press_key`.
VK_ESCAPE = 0x1B


def press_escape(session: CDPSession) -> None:
    session.press_key("Escape", code="Escape", windows_virtual_key_code=VK_ESCAPE)


def click_selector(session: SupportsEvaluate, selector: str) -> bool:
    """``.click()`` the first element matching ``selector``. False if none found."""

    script = (
        "(() => { const el = document.querySelector("
        f"{json.dumps(selector)}); if (!el) return false; el.click(); return true; }})()"
    )
    return bool(session.evaluate(script))


def click_button_with_text(session: SupportsEvaluate, text: str) -> bool:
    """``.click()`` the first ``<button>`` whose trimmed text exactly matches ``text``."""

    script = (
        "(() => { const btn = Array.from(document.querySelectorAll('button'))"
        f".find(b => (b.innerText || '').trim() === {json.dumps(text)});"
        " if (!btn) return false; btn.click(); return true; })()"
    )
    return bool(session.evaluate(script))


def selector_present(session: SupportsEvaluate, selector: str) -> bool:
    return bool(session.evaluate(f"document.querySelector({json.dumps(selector)}) !== null"))


def text_present(session: SupportsEvaluate, text: str) -> bool:
    script = (
        f"(() => Boolean(document.body) && document.body.innerText.includes({json.dumps(text)}))()"
    )
    return bool(session.evaluate(script))


def document_visibility_state(session: SupportsEvaluate) -> str:
    return str(session.evaluate("document.visibilityState"))


# --------------------------------------------------------------------------- #
# Win32 top-level-window visibility -- a second, OS-level signal alongside
# ``document.visibilityState`` (the CDP target can persist even once the
# window itself is hidden to the tray; see the "Keep running" step).
# --------------------------------------------------------------------------- #
def window_visibilities_for_pid(pid: int) -> list[bool]:
    """``IsWindowVisible()`` for every top-level window owned by ``pid``.

    Windows-only (returns ``[]`` elsewhere, never raises): this script only
    ever runs against the Windows-installed desktop build.
    """

    if not sys.platform.startswith("win"):
        return []
    import ctypes  # noqa: PLC0415 - Windows-only
    from ctypes import wintypes  # noqa: PLC0415 - Windows-only

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    visibilities: list[bool] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd: int, _lparam: int) -> bool:
        owner_pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == pid:
            visibilities.append(bool(user32.IsWindowVisible(hwnd)))
        return True

    user32.EnumWindows(_enum_proc, 0)
    return visibilities


def any_window_visible_for_pid(pid: int) -> bool | None:
    """``True``/``False`` once ``pid`` owns at least one top-level window, else ``None``."""

    visibilities = window_visibilities_for_pid(pid)
    return any(visibilities) if visibilities else None
