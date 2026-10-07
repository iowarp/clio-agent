"""Prepare a draft's workspace fleet without allocating a conversation.

The selected default blueprint and always-load MCP services use the same
workspace executor that a later session will use. Leaving the composer needs no
session cleanup: the ordinary fleet idle reaper owns the warmed connections.
"""

from __future__ import annotations

import logging
import threading
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Literal, cast

from clio_agent.gact import session_warmup
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.gact.warmup_revision import start_revision_warmup
from clio_agent.tools.execution import tool_blueprint_context, tool_workspace_context
from clio_agent.tools.workspace_root import canonical_workspace_root

logger = logging.getLogger(__name__)
WarmupStatus = Literal["warming", "disabled", "unavailable"]
_inflight: set[tuple[int, str, str]] = set()
_lock = threading.Lock()


def start_workspace_warmup(app: Any, root: str) -> WarmupStatus:
    """Start or join background preparation for a registered workspace root.

    Uses the same persisted blueprint default as session creation. Concurrent
    requests coalesce per app, canonical workspace root, and blueprint. No
    session is created, selected, or used to attribute this preparation.
    """
    if not session_warmup.session_warmup_enabled():
        return "disabled"
    agent = getattr(app.state, "agent", None)
    if not callable(getattr(agent, "_active_tool_executor", None)):
        return "unavailable"
    root = canonical_workspace_root(root)
    if not root:
        return "unavailable"
    blueprint_id = app.state.session_defaults.get().blueprint_id or ""
    key = (id(app), root, blueprint_id)
    with _lock:
        if key in _inflight:
            return "warming"
        _inflight.add(key)
    start_revision_warmup(
        app,
        lambda: _warm_and_release(app, root, blueprint_id, key),
        name="clio-workspace-warmup",
        finished=lambda: _release(key),
    )
    return "warming"


def _release(key: tuple[int, str, str]) -> None:
    with _lock:
        _inflight.discard(key)


def _warm_and_release(app: Any, root: str, blueprint_id: str, key: tuple[int, str, str]) -> None:
    try:
        agent = app.state.agent
        lease = getattr(agent, "lease_workspace_fleet", None)
        with (
            _gact_app_context(app),
            cast(AbstractContextManager[None], lease(root)) if callable(lease) else nullcontext(),
            tool_workspace_context(root),
            tool_blueprint_context(blueprint_id),
        ):
            report = session_warmup.warm_session_servers(agent)
        logger.info("workspace_warmup root=%s blueprint=%s servers=%s", root, blueprint_id, report)
    except Exception:
        # Best-effort startup never blocks Send; the real turn retries failures.
        logger.exception("workspace_warmup_failed root=%s blueprint=%s", root, blueprint_id)
    finally:
        _release(key)
