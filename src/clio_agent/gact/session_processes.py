"""The processes a session owns beyond its turn: ``run_code`` kernels and browser pages.

One owner for stopping them, so every exit path stops both (BR23):

- deleting a session stops its kernels and closes its pages (:func:`close_session_processes`);
- a child session's pages close when its task settles (:func:`close_child_pages`); its last
  screenshot stays in its transcript;
- the server's shutdown stops every kernel and browser (:func:`close_all_processes`). A
  managed shutdown ends in ``os._exit``, which skips ``atexit``, so this is called
  explicitly from the server's lifespan.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.browser.settings import BrowserError, errors
from clio_agent.runtime import trace

__all__ = ["close_all_processes", "close_child_pages", "close_session_processes"]


def close_session_processes(app: Any, session_id: str) -> None:
    """Stop a deleted session's kernels and close its browser pages."""
    from clio_agent.gact.browser import service as browser  # noqa: PLC0415
    from clio_agent.gact.code_mode import kernels  # noqa: PLC0415

    kernels.close_session(session_id)
    closed = browser.close_session(session_id)
    trace.event("SESSION", "session processes closed session=%s pages=%d", session_id, closed)


def close_child_pages(session_id: str) -> None:
    """Close a child session's browser pages once its task settled."""
    from clio_agent.gact.browser import service as browser  # noqa: PLC0415

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
