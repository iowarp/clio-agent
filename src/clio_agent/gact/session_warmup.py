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

import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

READY = "ready"

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


def start_session_warmup(app: Any, sid: str, *, trigger: str) -> threading.Thread | None:
    """Start ``sid``'s servers on a background thread; return it (``None`` when off).

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
    thread = threading.Thread(
        target=_warm_and_release,
        args=(app, sid, trigger),
        name=f"clio-warmup-{sid}",
        daemon=True,
    )
    thread.start()
    return thread


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
    """List and connect the bound session's servers concurrently.

    Runs inside the session's tool context (workspace root, activated blueprint).

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
    declared = agent._discover_pack_servers(
        get_active_tool_blueprint_id(), cwd=get_active_tool_workspace_root() or None
    )
    namespaces = sorted(
        ns
        for ns, spec in specs.items()
        if ns in declared or bool(getattr(spec, "always_load", False))
    )
    if not namespaces:
        return {}

    prepared = getattr(executor, "is_namespace_prepared", None)

    def start(namespace: str) -> str:
        if callable(prepared) and prepared(namespace):
            return READY  # connected already (an earlier warm-up or call)
        try:
            mcp_readiness.mount_namespace_for_session(
                executor, namespace, specs[namespace], connect=True
            )
        except Exception as exc:  # noqa: BLE001 - reported typed; a call retries it
            reason = mcp_readiness.mount_failure_reason(exc)
            logger.warning(
                "session_warmup_server_failed namespace=%s reason=%s error=%s",
                namespace,
                reason,
                exc,
            )
            return reason
        return READY

    with ThreadPoolExecutor(max_workers=len(namespaces), thread_name_prefix="clio-warm") as pool:
        futures = {ns: pool.submit(contextvars.copy_context().run, start, ns) for ns in namespaces}
        return {ns: future.result() for ns, future in futures.items()}
