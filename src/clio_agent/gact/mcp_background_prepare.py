"""Prepare MCP servers in the background, so no request waits for a server it may not use.

Opening a conversation, creating a session, changing its agent and starting a turn all
ask for the agent's MCP servers here (:mod:`clio_agent.gact.session_warmup`). Each
server is prepared once per workspace fleet at a time -- provisioned, started once,
listed over that connection (:func:`clio_agent.gact.mcp_readiness.
mount_namespace_for_session`) -- and every caller joins the one preparation in flight.

A turn (:func:`prepare_for_turn`) waits at most ``tools.mcp.turn_prepare_grace_s`` for
the servers its agent declares. One still preparing after that is handed to the agent
as *preparing*: its tools join the agent's tool list at the next step once they are
listed (``agents.live_tool_refresh``), and the agent can wait for it explicitly with
``wait_for_tools`` (:mod:`clio_agent.gact.agents.wait_for_tools`).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_GRACE_S = 2.0

#: A turn started while some of its agent's servers were still preparing.
MCP_PREPARE_TURN_NOT_WAITING = "mcp_prepare_turn_not_waiting"

_lock = threading.Lock()
_inflight: dict[tuple[int, str], concurrent.futures.Future[Mapping[str, Any]]] = {}


def turn_prepare_grace_s() -> float:
    """``tools.mcp.turn_prepare_grace_s`` (2 s): how long a turn waits for a cold server."""

    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "tools.mcp.turn_prepare_grace_s",
        env="CLIO_MCP_TURN_PREPARE_GRACE_S",
        default=_DEFAULT_GRACE_S,
        cast=conf.as_float,
    )
    return max(0.0, value)


def prepare_in_background(
    tool_executor: Any, namespace: str, spec: Any
) -> concurrent.futures.Future[Mapping[str, Any]]:
    """Start (or join) the preparation of ``namespace`` on ``tool_executor``.

    The worker runs in a copy of the caller's context, so its progress events reach
    the session that asked. The returned future resolves to the namespace's tools, or
    raises the preparation's typed failure.
    """

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    key = (id(tool_executor), namespace)
    with _lock:
        future = _inflight.get(key)
        if future is not None and not future.done():
            return future
        # Visible at once: a status read right after this call sees it preparing.
        mcp_prepare.record(namespace, mcp_prepare.PREPARING, mcp_prepare.PHASE_PROVISION)
        future = concurrent.futures.Future()
        context = contextvars.copy_context()

        def run() -> None:
            try:
                future.set_result(context.run(_prepare_and_settle, tool_executor, namespace, spec))
            except Exception as exc:
                # Already recorded typed (``failed`` + reason); the traceback is for the log.
                logger.exception("mcp_prepare_background_failed namespace=%s", namespace)
                future.set_exception(exc)

        # A daemon thread: a preparation never holds the process open at exit.
        threading.Thread(target=run, name=f"clio-prepare-{namespace}", daemon=True).start()
        _inflight[key] = future

    def _done(finished: concurrent.futures.Future[Mapping[str, Any]]) -> None:
        with _lock:
            if _inflight.get(key) is finished:
                del _inflight[key]

    future.add_done_callback(_done)
    return future


def _prepare_and_settle(tool_executor: Any, namespace: str, spec: Any) -> Mapping[str, Any]:
    """Run one preparation; its outcome is recorded before any joiner sees it."""

    from clio_agent.gact.mcp_readiness import mount_namespace_for_session  # noqa: PLC0415
    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    try:
        tools = mount_namespace_for_session(tool_executor, namespace, spec)
    except Exception as exc:
        _settle(namespace, mcp_prepare.FAILED, exc=exc)
        raise
    _settle(namespace, mcp_prepare.READY, tool_count=len(tools))
    return tools


def _settle(namespace: str, state: str, *, exc: BaseException | None = None, **fields: Any) -> None:
    """Make sure a finished preparation is not left showing ``preparing``."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    row = mcp_prepare.preparation_state(namespace) or {}
    if row.get("state") != mcp_prepare.PREPARING:
        return  # the preparation recorded its own outcome
    phase = str(row.get("phase") or mcp_prepare.PHASE_PROVISION)
    if exc is not None:
        fields.update(reason=mcp_prepare.classify(exc, phase), detail=str(exc))
    mcp_prepare.record(namespace, state, phase, **fields)


def failure_reason(namespace: str, exc: BaseException) -> str:
    """The typed reason a preparation of ``namespace`` failed with."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    row = mcp_prepare.preparation_state(namespace) or {}
    return str(row.get("reason") or mcp_prepare.classify(exc, str(row.get("phase") or "")))


def prepare_for_turn(
    tool_executor: Any, specs: Mapping[str, Any], *, grace_s: float | None = None
) -> tuple[set[str], dict[str, str]]:
    """Prepare ``specs`` without making the turn wait past the grace period.

    Returns:
        ``(preparing, failures)``: the namespaces still preparing when the grace ran
        out, and a typed reason for each that failed within it.
    """

    if not specs:
        return set(), {}
    wait_s = turn_prepare_grace_s() if grace_s is None else grace_s
    futures = {ns: prepare_in_background(tool_executor, ns, spec) for ns, spec in specs.items()}
    concurrent.futures.wait(list(futures.values()), timeout=wait_s)
    preparing: set[str] = set()
    failures: dict[str, str] = {}
    for namespace, future in futures.items():
        if not future.done():
            preparing.add(namespace)
            continue
        exc = future.exception()
        if exc is not None:
            failures[namespace] = failure_reason(namespace, exc)
    if preparing:
        from clio_agent.runtime import trace  # noqa: PLC0415

        trace.event(
            "TOOLS",
            "%s namespaces=%s grace_s=%.1f",
            MCP_PREPARE_TURN_NOT_WAITING,
            ",".join(sorted(preparing)),
            wait_s,
        )
    return preparing, failures


__all__ = [
    "MCP_PREPARE_TURN_NOT_WAITING",
    "failure_reason",
    "prepare_for_turn",
    "prepare_in_background",
    "turn_prepare_grace_s",
]
