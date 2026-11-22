"""Prepare a workspace fleet without allocating a conversation.

The default agent's plugin MCP servers and always-load MCP services use the same
workspace executor that a later session will use. Leaving the composer needs no
session cleanup: the ordinary fleet idle reaper owns the warmed connections.

Three moments start this preparation, all off the request path:

* the agent becoming ready (:func:`start_launch_warmup`): a CLIO-owned root's fleet
  provisions every default server, lists it, persists the listing and is then closed,
  so the first turn of any workspace already has the definitions;
* a workspace being created (``POST /v1/workspaces``) and the default agent changing;
* the composer's explicit ``POST /v1/workspaces/{wid}/warmup``.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Literal, cast

from clio_agent.gact import session_warmup
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.gact.warmup_revision import start_revision_warmup
from clio_agent.runtime import trace
from clio_agent.tools.execution import tool_server_set_context, tool_workspace_context
from clio_agent.tools.workspace_root import canonical_workspace_root

logger = logging.getLogger(__name__)
WarmupStatus = Literal["warming", "disabled", "unavailable"]
_inflight: set[tuple[int, str, str]] = set()
_lock = threading.Lock()
#: The CLIO-owned folder (under the user state dir) the launch warm-up runs its fleet in.
LAUNCH_WARMUP_DIR = "launch-warmup"


def launch_warmup_root() -> str:
    """The CLIO-owned root of the launch warm-up's fleet (created on first use)."""
    from clio_agent.paths import user_state_dir  # noqa: PLC0415

    root = user_state_dir() / LAUNCH_WARMUP_DIR
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def start_launch_warmup(app: Any) -> WarmupStatus:
    """Prepare the default agent's servers once the agent is ready, before any turn.

    Runs in a CLIO-owned root rather than a user workspace: what it keeps is the
    persisted listing every workspace fleet preloads, not a running server. Its fleet
    is closed when the preparation settles.
    """
    try:
        root = launch_warmup_root()
    except OSError:
        logger.exception("launch_warmup_root_unavailable")
        return "unavailable"
    return start_workspace_warmup(app, root, close_after=True)


def start_workspace_warmup(app: Any, root: str, *, close_after: bool = False) -> WarmupStatus:
    """Start or join background preparation for a registered workspace root.

    Uses the same persisted default agent as session creation. Concurrent
    requests coalesce per app, canonical workspace root, and server set. No
    session is created, selected, or used to attribute this preparation.
    ``close_after`` closes the root's fleet once the preparation settles.
    """
    if not session_warmup.session_warmup_enabled():
        return "disabled"
    agent = getattr(app.state, "agent", None)
    if not callable(getattr(agent, "_active_tool_executor", None)):
        return "unavailable"
    root = canonical_workspace_root(root)
    if not root:
        return "unavailable"
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        agent_server_set,
        default_agent_ref,
    )

    server_set = agent_server_set(app, default_agent_ref(app))
    key = (id(app), root, server_set)
    with _lock:
        if key in _inflight:
            return "warming"
        _inflight.add(key)
    extra = {"close_after": True} if close_after else {}
    start_revision_warmup(
        app,
        lambda: _warm_and_release(app, root, server_set, key, **extra),
        name="clio-workspace-warmup",
        aborted=lambda: _release(key),
    )
    return "warming"


def _release(key: tuple[int, str, str]) -> None:
    with _lock:
        _inflight.discard(key)


def _warm_and_release(
    app: Any, root: str, server_set: str, key: tuple[int, str, str], *, close_after: bool = False
) -> None:
    agent = getattr(app.state, "agent", None)
    started = time.monotonic()
    try:
        lease = getattr(agent, "lease_workspace_fleet", None)
        with (
            _gact_app_context(app),
            cast(AbstractContextManager[None], lease(root)) if callable(lease) else nullcontext(),
            tool_workspace_context(root),
            tool_server_set_context(server_set),
        ):
            report = session_warmup.warm_session_servers(agent)
        trace.event(
            "TOOLS",
            "workspace_warmup_finished root=%s server_set=%s seconds=%.1f servers=%s",
            root,
            server_set,
            time.monotonic() - started,
            report,
        )
    except Exception:
        # Best-effort startup never blocks Send; the real turn retries failures.
        logger.exception("workspace_warmup_failed root=%s server_set=%s", root, server_set)
    finally:
        _release(key)
        if close_after:
            _close_fleet(agent, root)


def _close_fleet(agent: Any, root: str) -> None:
    """Close a warm-up's fleet once its listings are persisted (never a busy one)."""
    restart = getattr(agent, "request_fleet_restart", None)
    if not callable(restart):
        return
    try:
        outcome = restart(root)
    except Exception:
        logger.exception("workspace_warmup_close_failed root=%s", root)
        return
    logger.info("workspace_warmup_closed root=%s outcome=%s", root, outcome)
