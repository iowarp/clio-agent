"""Obtain MCP tool definitions before composing a model request.

A server with no known listing is prepared here -- provisioned, started once, listed
over that connection (:func:`clio_agent.gact.mcp_readiness.mount_namespace_for_session`)
-- once per workspace fleet at a time, and every caller joins the one preparation in
flight. The session warm-up (:mod:`clio_agent.gact.session_warmup`) asks only for
servers with no listing; a listed server starts when a call needs it.

The service also lists the default agent's servers when the agent becomes ready and
when a workspace is created (:mod:`clio_agent.gact.workspace_warmup`). Those listings
persist in the listing cache, so the first turn's fleet already has every definition.

A turn calls :func:`prepare_for_turn` only for missing definitions. A listing cached
after its fleet was built is adopted at once. Otherwise it joins the preparation already
running, including its real tools/list, before building the first request. A
preparation of the same server for another fleet (the launch warm-up) is joined, never
repeated: its persisted listing then serves this fleet too. Valid cached definitions need
no wait and start no server. The existing transport owns launch, connection,
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
#: A preparation future's event: set once its fleet has the namespace's definitions.
_LISTED = "_clio_definitions_listed"


def adopt_cached_listing(tool_executor: Any, namespace: str, spec: Any) -> bool:
    """Merge ``namespace``'s persisted listing into ``tool_executor``; whether it had one.

    A fleet reads the listing cache when it is built. A listing stored later, by the
    launch or workspace warm-up of another fleet, is adopted here without starting
    anything.
    """

    if getattr(spec, "transport", "") != "stdio" or not getattr(spec, "command", ""):
        return False
    merge = getattr(tool_executor, "merge_namespace_tools", None)
    if not callable(merge):
        return False
    from clio_agent.connectors.listing import cached_offered  # noqa: PLC0415

    listed = cached_offered(namespace, spec)
    if not listed:
        return False
    merge(
        namespace,
        {
            f"{namespace}_{tool.name}": tool.model_copy(update={"name": f"{namespace}_{tool.name}"})
            for tool in listed
        },
    )
    logger.info("mcp_listing_adopted namespace=%s tools=%d", namespace, len(listed))
    return True


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
        # The same server preparing for another fleet: wait for it, then use its listing.
        ahead = [
            other
            for (owner, name), other in _inflight.items()
            if name == namespace and owner != key[0] and not other.done()
        ]
        # Visible at once: a status read right after this call sees it preparing.
        mcp_prepare.record(namespace, mcp_prepare.PREPARING, mcp_prepare.PHASE_PROVISION)
        future = concurrent.futures.Future()
        listed = threading.Event()
        setattr(future, _LISTED, listed)
        context = contextvars.copy_context()

        def run() -> None:
            try:
                if ahead:
                    concurrent.futures.wait(ahead)
                    logger.info(
                        "mcp_prepare_joined_other_fleet namespace=%s fleets=%d",
                        namespace,
                        len(ahead),
                    )
                if adopt_cached_listing(tool_executor, namespace, spec):
                    listed.set()  # definitions known: only this fleet's connection remains
                future.set_result(context.run(_prepare_and_settle, tool_executor, namespace, spec))
            except Exception as exc:
                # Already recorded typed (``failed`` + reason); the traceback is for the log.
                logger.exception("mcp_prepare_background_failed namespace=%s", namespace)
                future.set_exception(exc)
            finally:
                listed.set()

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
    A listing persisted since the fleet was built is adopted without waiting. Start
    every other preparation before joining any, so servers prepare concurrently, and
    return as soon as each has its definitions (a server whose listing came from another
    fleet's warm-up still connects in the background). A cold server must answer
    tools/list; a grace period cannot safely substitute for its schemas. The
    connection's existing progress limits bound a stalled server, and failures remain
    visible through the normal readiness boundary.
    """

    if not specs:
        return {}
    from clio_agent.errors import CancellationError  # noqa: PLC0415
    from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415

    missing = {
        ns: spec for ns, spec in specs.items() if not adopt_cached_listing(tool_executor, ns, spec)
    }
    futures = {ns: prepare_in_background(tool_executor, ns, spec) for ns, spec in missing.items()}
    pending = dict(futures)
    while pending:
        if cancellation_requested():
            # The conversation warm-up can be shared with another turn. Stop this
            # waiter, not the shared connection it is still preparing.
            raise CancellationError("Stopped while preparing MCP tools")
        for namespace, future in list(pending.items()):
            listed = getattr(future, _LISTED, None)
            if future.done() or (listed is not None and listed.is_set()):
                del pending[namespace]
        if pending:
            concurrent.futures.wait(
                pending.values(), timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
            )
    failures: dict[str, str] = {}
    for namespace, future in futures.items():
        if not future.done():
            continue  # definitions adopted; its connection is still starting
        # exception() joins the same connection/listing as the conversation warm-up.
        exc = future.exception()
        if exc is not None and not has_definitions(tool_executor, namespace):
            failures[namespace] = failure_reason(namespace, exc)
    return failures


def has_definitions(tool_executor: Any, namespace: str) -> bool:
    """Whether ``tool_executor`` already offers tools of ``namespace``."""

    prefix = f"{namespace}_"
    lister = getattr(tool_executor, "to_dspy_tools", None)
    if not callable(lister):
        return False
    return any(str(getattr(tool, "name", "") or "").startswith(prefix) for tool in lister())


__all__ = [
    "adopt_cached_listing",
    "failure_reason",
    "has_definitions",
    "prepare_for_turn",
    "prepare_in_background",
]
