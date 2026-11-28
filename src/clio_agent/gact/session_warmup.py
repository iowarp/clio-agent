"""Have a session's MCP tool listings and environments ready before its first message.

When a session is created or opened, CLIO prepares the servers the session will use --
the session agent's plugin servers and any always-load service -- concurrently, off the
request path. A server process starts only when something needs it: a server whose
tools are already listed only has its environment built, and starts at its first call;
a server with no listing is started once to list its tools, which the first model
request needs. Nothing waits on the warm-up. A turn that arrives first builds its model
request from the listings (joining an in-flight listing).

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
from contextlib import ExitStack
from typing import Any

from clio_agent.gact.warmup_revision import start_revision_warmup
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

READY = "ready"
#: A server's tools are known and its environment is built; it is not started.
LISTED = "listed"
# The namespaces a warm-up covered on an executor (a fleet): while all their tools are
# known, a later warm-up has nothing to do and skips the server re-read.
_WARMED_ATTR = "_clio_warmed_namespaces"

# Sessions whose warm-up is running: one at a time per session.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()
# Warm-ups asked for before the app's agent was ready: ``{id(app): {sid: trigger}}``.
_deferred: dict[int, dict[str, str]] = {}

#: A warm-up waits for the agent, which is still being built.
WARMUP_DEFERRED = "session_warmup_deferred_agent_starting"


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
        trigger: What asked for it (``session_created`` / ``conversation_opened`` /
            ``agent_changed`` / ``page_prestart``), for the log.
    """

    if not session_warmup_enabled():
        return None
    agent = getattr(getattr(app, "state", None), "agent", None)
    if not callable(getattr(agent, "_active_tool_executor", None)):
        # The agent is still being built (just after start): the request is kept and
        # runs when it is ready (:func:`start_deferred_warmups`), never dropped.
        with _inflight_lock:
            _deferred.setdefault(id(app), {})[sid] = trigger
        trace.event("TOOLS", "%s session=%s trigger=%s", WARMUP_DEFERRED, sid, trigger)
        return None
    with _inflight_lock:
        if sid in _inflight:
            return None  # already warming; this caller joins it through the executor
        _inflight.add(sid)
    return start_revision_warmup(
        app,
        lambda: _warm_and_release(app, sid, trigger),
        name=f"clio-warmup-{sid}",
        aborted=lambda: _release(sid),
    )


def start_deferred_warmups(app: Any) -> None:
    """Start the warm-ups asked for while ``app``'s agent was being built."""

    with _inflight_lock:
        pending = _deferred.pop(id(app), {})
    for sid, trigger in pending.items():
        start_session_warmup(app, sid, trigger=trigger)


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
            # Opening a conversation does not restart a server that failed: that is
            # what reconnect (or the conversation's next turn) is for.
            report = warm_session_servers(
                app.state.agent, retry_failed=trigger != "conversation_opened"
            )
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


def warm_session_servers(agent: Any, *, retry_failed: bool = True) -> dict[str, str]:
    """Know the bound workspace's plugin server tools, and build their environments.

    Runs inside a session or draft tool context (workspace root, server set). A server
    whose tools are already known (listed by this fleet, or persisted by an earlier
    listing) is not started: only its environment is provisioned, and the server starts
    when a call needs it. A server with no listing is started once to list its tools --
    the same preparation a turn would otherwise join
    (:func:`clio_agent.gact.mcp_background_prepare.prepare_in_background`).
    ``retry_failed=False`` leaves a server whose last preparation failed as it is.

    Returns:
        ``{namespace: "listed" | "ready" | <typed failure reason>}``: ``listed`` is
        known and provisioned but not started, ``ready`` was started to list it.
    """

    import contextvars  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from clio_agent.gact import mcp_background_prepare as background  # noqa: PLC0415
    from clio_agent.gact import mcp_readiness  # noqa: PLC0415
    from clio_agent.tools import mcp_prepare  # noqa: PLC0415
    from clio_agent.tools.execution import (  # noqa: PLC0415
        get_active_tool_server_set,
    )

    executor = agent._active_tool_executor()
    specs = getattr(executor, "_clio_namespace_specs", None) or {}
    warmed = getattr(executor, _WARMED_ATTR, None)
    server_set = get_active_tool_server_set()
    if (
        warmed is not None
        and getattr(executor, "_clio_warmed_server_set", "") == server_set
        and all(background.has_definitions(executor, ns) for ns in warmed)
    ):
        return {}  # every server's tools are known: nothing to do
    declared = {
        name for servers in agent._discover_plugin_servers(server_set).values() for name in servers
    }
    namespaces = sorted(
        ns
        for ns, spec in specs.items()
        if ns in declared or bool(getattr(spec, "always_load", False))
    )
    if not namespaces:
        return {}

    def _failed(ns: str) -> str:
        row = mcp_prepare.preparation_state(ns) or {}
        return str(row.get("reason") or "") if row.get("state") == mcp_prepare.FAILED else ""

    known = [
        ns
        for ns in namespaces
        if background.has_definitions(executor, ns)
        or background.adopt_cached_listing(executor, ns, specs[ns])
    ]
    report = {ns: _failed(ns) for ns in namespaces if ns not in known and not retry_failed}
    report = {ns: reason for ns, reason in report.items() if reason}
    unlisted = {ns: specs[ns] for ns in namespaces if ns not in known and ns not in report}
    with ThreadPoolExecutor(
        max_workers=max(1, len(known)), thread_name_prefix="clio-provision-ahead"
    ) as pool:
        provisions = {
            ns: pool.submit(contextvars.copy_context().run, _provision_ahead, ns, specs[ns])
            for ns in known
        }
        mounted, failures = mcp_readiness.mount_namespaces_for_session(
            executor, unlisted, connect=True
        )
        report.update({ns: future.result() for ns, future in provisions.items()})
    report.update(dict.fromkeys(mounted, READY))
    report.update(failures)
    setattr(executor, _WARMED_ATTR, frozenset(namespaces))
    executor._clio_warmed_server_set = server_set
    return {ns: report[ns] for ns in namespaces}


def _provision_ahead(namespace: str, spec: Any) -> str:
    """Build ``namespace``'s environment without starting its server."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    try:
        mcp_prepare.provision(namespace, spec)
    except Exception as exc:  # noqa: BLE001 - the server's first start builds it again
        reason = mcp_prepare.classify(exc, mcp_prepare.PHASE_PROVISION)
        logger.warning(
            "session_warmup_provision_failed namespace=%s reason=%s error=%s",
            namespace,
            reason,
            exc,
        )
        return reason
    return LISTED


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


def hold_session_fleet(app: Any, sid: str, *, seconds: float | None = None) -> None:
    """Keep ``sid``'s workspace fleet while the session waits on its user.

    ``seconds`` overrides :func:`hold_while_waiting_s` (a prestarted page's hold).
    """

    state = getattr(app, "state", None)
    agent = getattr(state, "agent", None)
    lease = getattr(agent, "lease_workspace_fleet", None)
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
        from clio_agent.tools.reaper import residency_lease  # noqa: PLC0415

        try:
            stack.enter_context(residency_lease(agent, root))
        except RuntimeError as error:  # connected-data access is being reconfigured
            logger.warning(
                "session_fleet_hold_refused session=%s root=%s error=%s", sid, root, error
            )
            return
        timer = threading.Timer(
            hold_while_waiting_s() if seconds is None else seconds,
            release_session_fleet,
            args=(sid,),
        )
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
