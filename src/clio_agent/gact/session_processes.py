"""The processes a session owns beyond its turn: ``run_code`` kernels and browser pages.

One owner for stopping them, so every exit path stops both (BR23):

- deleting a session stops its kernels and its streams and closes its pages
  (:func:`close_session_processes`);
- a child session's pages close when its task settles (:func:`close_child_pages`); its last
  screenshot stays in its transcript. A background script the child started outlives it
  (CT6) and may still drive those pages, so while the child runs one the close waits, and
  happens when its last script ends (:func:`script_started`, :func:`script_ended`);
- the server's shutdown stops every kernel and browser (:func:`close_all_processes`). A
  managed shutdown ends in ``os._exit``, which skips ``atexit``, so this is called
  explicitly from the server's lifespan.
"""

from __future__ import annotations

import threading
from typing import Any

from clio_agent.gact.browser.settings import BrowserError, errors
from clio_agent.runtime import trace

__all__ = [
    "close_all_processes",
    "close_child_pages",
    "close_session_processes",
    "script_ended",
    "script_started",
]

#: The background scripts each session runs now (session id -> task handles).
_SCRIPTS: dict[str, set[str]] = {}
#: Settled children whose pages close when their last script ends.
_PAGES_AFTER_SCRIPTS: set[str] = set()
_LOCK = threading.Lock()


def script_started(session_id: str, handle: str) -> None:
    """A background script of ``session_id`` started running."""
    with _LOCK:
        _SCRIPTS.setdefault(session_id, set()).add(handle)


def script_ended(session_id: str, handle: str) -> None:
    """A background script of ``session_id`` ended; a settled child's pages close with the
    last one."""
    with _LOCK:
        running = _SCRIPTS.get(session_id, set())
        running.discard(handle)
        if running:
            return
        _SCRIPTS.pop(session_id, None)
        if session_id not in _PAGES_AFTER_SCRIPTS:
            return
        _PAGES_AFTER_SCRIPTS.discard(session_id)
    close_child_pages(session_id)


def close_session_processes(app: Any, session_id: str) -> None:
    """Stop a deleted session's kernels and streams and close its browser pages."""
    from clio_agent.gact.browser import service as browser  # noqa: PLC0415
    from clio_agent.gact.code_mode import kernels  # noqa: PLC0415

    with _LOCK:
        _PAGES_AFTER_SCRIPTS.discard(session_id)
    kernels.close_session(session_id)
    streams = getattr(app.state, "streams", None)
    stopped = streams.stop_session(session_id) if streams is not None else 0
    closed = browser.close_session(session_id)
    trace.event(
        "SESSION",
        "session processes closed session=%s pages=%d streams=%d",
        session_id,
        closed,
        stopped,
    )


def close_child_pages(session_id: str) -> None:
    """Close a child session's browser pages once its task settled, or once its last
    background script ends when it still runs one."""
    from clio_agent.gact.browser import service as browser  # noqa: PLC0415

    with _LOCK:
        if _SCRIPTS.get(session_id):
            _PAGES_AFTER_SCRIPTS.add(session_id)
            waiting = len(_SCRIPTS[session_id])
        else:
            waiting = 0
    if waiting:
        trace.event(
            "BROWSER",
            "child pages kept for its background scripts session=%s scripts=%d",
            session_id,
            waiting,
        )
        return
    try:
        closed = browser.close_session(session_id)
    except errors(TimeoutError, BrowserError) as exc:  # settling never fails here
        trace.event("BROWSER", "child pages close failed session=%s error=%s", session_id, exc)
        return
    if closed:
        trace.event("BROWSER", "child pages closed session=%s pages=%d", session_id, closed)


def close_all_processes() -> None:
    """Stop every kernel and browser (server shutdown)."""
    from clio_agent.gact.browser import service as browser  # noqa: PLC0415
    from clio_agent.gact.code_mode import kernels  # noqa: PLC0415

    browser.close_all()
    kernels.close_all()
