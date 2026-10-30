"""Start MCP servers early and obtain definitions before composing a model request.

Opening a conversation, creating a session, changing its agent and starting a turn all
ask for the agent's MCP servers here (:mod:`clio_agent.gact.session_warmup`). Each
server is prepared once per workspace fleet at a time -- provisioned, started once,
listed over that connection (:func:`clio_agent.gact.mcp_readiness.
mount_namespace_for_session`) -- and every caller joins the one preparation in flight.

A turn calls :func:`prepare_for_turn` only for missing definitions. It joins the
preparation already running, including its real tools/list, before building the first
request. Valid cached definitions need no wait; the conversation warm-up starts their
execution servers independently. The existing transport owns launch, connection,
authentication and progress limits for every server type.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_inflight: dict[tuple[int, str], concurrent.futures.Future[Mapping[str, Any]]] = {}


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


def prepare_for_turn(tool_executor: Any, specs: Mapping[str, Any]) -> dict[str, str]:
    """Join missing tool listings before the first request; return typed failures.

    ``specs`` contains only namespaces whose definitions are not already known.
    Start every preparation before joining any, so servers prepare concurrently.
    A cold server must answer tools/list; a grace period cannot safely substitute
    for its schemas. The connection's existing progress limits bound a stalled
    server, and failures remain visible through the normal readiness boundary.
    """

    if not specs:
        return {}
    from clio_agent.errors import CancellationError  # noqa: PLC0415
    from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415

    futures = {ns: prepare_in_background(tool_executor, ns, spec) for ns, spec in specs.items()}
    pending = set(futures.values())
    while pending:
        if cancellation_requested():
            # The conversation warm-up can be shared with another turn. Stop this
            # waiter, not the shared connection it is still preparing.
            raise CancellationError("Stopped while preparing MCP tools")
        _done, pending = concurrent.futures.wait(
            pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
        )
    failures: dict[str, str] = {}
    for namespace, future in futures.items():
        # exception() joins the same connection/listing as the conversation warm-up.
        exc = future.exception()
        if exc is not None:
            failures[namespace] = failure_reason(namespace, exc)
    return failures


__all__ = [
    "failure_reason",
    "prepare_for_turn",
    "prepare_in_background",
]
