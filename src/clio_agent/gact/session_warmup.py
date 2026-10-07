"""Start a session's MCP servers in the background, before its first message.

Owner, 2026-09-30: the first message must not pay for starting the session's
servers. When a session is created or an Agent Blueprint is activated, CLIO starts
the servers the session will use -- the activated blueprint's declared servers and
any always-load service -- concurrently, off the request path: each is listed and
connected. Nothing waits on the warm-up. A turn that arrives first builds its model
request from the listings (joining an in-flight listing), and a tool call waits only
for its own server: the executor's per-namespace connect joins the warm-up's.

A server that fails to start is reported typed (logged, and as the session's
``infrastructure.dependency.changed`` state) and is tried again when a call needs
it -- a failure here is never remembered.

Config: ``tools.mcp.session_warmup`` / ``CLIO_MCP_SESSION_WARMUP`` (default on).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import AbstractContextManager, ExitStack
from typing import Any, cast

from clio_agent.gact.warmup_revision import start_revision_warmup

logger = logging.getLogger(__name__)

READY = "ready"
# The namespaces a warm-up started on an executor (a fleet): while all are still up,
# a later warm-up has nothing to do and skips the blueprint re-read.
_WARMED_ATTR = "_clio_warmed_namespaces"

# Sessions whose warm-up is running: one at a time per session.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def session_warmup_enabled() -> bool:
    """Whether sessions start their servers in the background (default on)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "tools.mcp.session_warmup",
        env="CLIO_MCP_SESSION_WARMUP",
        default=True,
        cast=conf.as_bool,
    )


def start_session_warmup(
    app: Any, sid: str, *, trigger: str
) -> threading.Thread | asyncio.Task[None] | None:
    """Start ``sid``'s servers under its runtime revision (``None`` when off).

    Args:
        app: The GACT app (its ``state.agent`` owns the workspace fleets).
        sid: The session whose servers to start.
        trigger: What asked for it (``session_created`` / ``blueprint_activated``),
            for the log.
    """

    agent = getattr(getattr(app, "state", None), "agent", None)
    if not session_warmup_enabled() or not callable(getattr(agent, "_active_tool_executor", None)):
        return None
    with _inflight_lock:
        if sid in _inflight:
            return None  # already warming; this caller joins it through the executor
        _inflight.add(sid)
    return start_revision_warmup(
        app,
        lambda: _warm_and_release(app, sid, trigger),
        name=f"clio-warmup-{sid}",
        finished=lambda: _release(sid),
    )


def _release(sid: str) -> None:
    with _inflight_lock:
        _inflight.discard(sid)


def _warm_and_release(app: Any, sid: str, trigger: str) -> None:
    try:
        _warm(app, sid, trigger)
    finally:
        _release(sid)


def _warm(app: Any, sid: str, trigger: str) -> None:
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _gact_app_context,
        _tool_session_context,
    )

    started = time.monotonic()
    try:
        with _gact_app_context(app), _tool_session_context(sid):
            report = warm_session_servers(app.state.agent)
    except Exception:  # noqa: BLE001 - a warm-up never fails a session; the turn retries
        logger.exception("session_warmup_failed session=%s trigger=%s", sid, trigger)
        return
    logger.info(
        "session_warmup session=%s trigger=%s elapsed_s=%.1f servers=%s",
        sid,
        trigger,
        time.monotonic() - started,
        report,
    )


def warm_session_servers(agent: Any) -> dict[str, str]:
    """List and connect the bound workspace/blueprint's servers concurrently.

    Runs inside a session or draft tool context (workspace root, blueprint).

    Returns:
        ``{namespace: "ready" | <typed failure reason>}``.
    """

    from clio_agent.gact import mcp_readiness  # noqa: PLC0415
    from clio_agent.tools.execution import (  # noqa: PLC0415
        get_active_tool_blueprint_id,
        get_active_tool_workspace_root,
    )

    executor = agent._active_tool_executor()
    specs = getattr(executor, "_clio_namespace_specs", None) or {}
    prepared = getattr(executor, "is_namespace_prepared", None)
    warmed = getattr(executor, _WARMED_ATTR, None)
    blueprint_id = get_active_tool_blueprint_id()
    if (
        warmed is not None
        and getattr(executor, "_clio_warmed_blueprint_id", "") == blueprint_id
        and callable(prepared)
        and all(prepared(ns) for ns in warmed)
    ):
        return {}  # its servers are all up: no blueprint re-read beside the turn
    declared = agent._discover_pack_servers(
        blueprint_id, cwd=get_active_tool_workspace_root() or None
    )
    namespaces = sorted(
        ns
        for ns, spec in specs.items()
        if ns in declared or bool(getattr(spec, "always_load", False))
    )
    if not namespaces:
        return {}

    cold = {ns: specs[ns] for ns in namespaces if not (callable(prepared) and prepared(ns))}
    _mounted, failures = mcp_readiness.mount_namespaces_for_session(executor, cold, connect=True)
    setattr(executor, _WARMED_ATTR, frozenset(namespaces))
    executor._clio_warmed_blueprint_id = blueprint_id
    return {ns: failures.get(ns, READY) for ns in namespaces}


# --------------------------------------------------------------------------- #
# Keep a waiting session's servers                                            #
# --------------------------------------------------------------------------- #
# A turn that stops to ask its user leaves the fleet idle; the idle reaper would
# close the servers while the user answers, and the resume would start them all
# again. The session's workspace fleet is held (a lease, as a running turn takes)
# until its next turn starts, or for ``tools.mcp.hold_while_waiting_s``.
_holds: dict[str, tuple[ExitStack, threading.Timer]] = {}
_holds_lock = threading.Lock()


def hold_while_waiting_s() -> float:
    """How long a session waiting on its user keeps its servers (default 30 min)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "tools.mcp.hold_while_waiting_s",
        env="CLIO_MCP_HOLD_WHILE_WAITING_S",
        default=1800.0,
        cast=conf.as_float,
    )


def hold_session_fleet(app: Any, sid: str) -> None:
    """Keep ``sid``'s workspace fleet while the session waits on its user."""

    state = getattr(app, "state", None)
    lease = getattr(getattr(state, "agent", None), "lease_workspace_fleet", None)
    sessions = getattr(state, "sessions", None)
    workspaces = getattr(state, "workspaces", None)
    sess = sessions.get(sid) if sessions is not None else None
    workspace_id = str(getattr(sess, "workspace_id", "") or "")
    ws = workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
    root = str(getattr(ws, "root_path", "") or "")
    if not root or not callable(lease):
        return
    with _holds_lock:
        if sid in _holds:
            return
        stack = ExitStack()
        stack.enter_context(cast(AbstractContextManager[None], lease(root)))
        timer = threading.Timer(hold_while_waiting_s(), release_session_fleet, args=(sid,))
        timer.daemon = True
        _holds[sid] = (stack, timer)
    timer.start()
    logger.info("session_fleet_held session=%s root=%s", sid, root)


def release_session_fleet(sid: str) -> None:
    """Drop ``sid``'s hold (its next turn started, or the wait ran out)."""

    with _holds_lock:
        held = _holds.pop(sid, None)
    if held is None:
        return
    stack, timer = held
    timer.cancel()
    stack.close()
