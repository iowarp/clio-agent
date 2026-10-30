"""Readiness boundary for cold MCP namespaces.

The discovery layer intentionally stays process-scoped and single-flight.  This
module adds the per-session boundary around a selected session's cold mount:
bounded increasing-wait retries of a FAILED mount with a typed reason on every retried
attempt, and session-scoped progress events for the UI. The connect itself has no fixed
deadline: it waits while the server's own process tree works
(:mod:`clio_agent.tools.mcp_server_progress`), so a slow start is never cut off and
restarted, and a connect that stopped progressing is a terminal typed failure. It never
installs an undeclared server and never exposes raw subprocess errors.

A terminal failure raises to the existing typed tool-resolution boundary, where
:func:`clio_agent.gact.agents.builders._resolve_requested_tools` records it in
the ``mount_failures`` map the ``_UnsupportedSessionAgent`` reason carries.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: Typed reason for one retried cold mount (queryable in logs/trace).
MCP_MOUNT_RETRY_REASON = "mcp_mount_retry"


def mcp_mount_retry_delays_s() -> tuple[float, ...]:
    """The increasing waits between cold-mount attempts, in seconds.

    Config: ``tools.mcp.mount_retry_delays_s`` /
    ``CLIO_MCP_MOUNT_RETRY_DELAYS_S`` (default ``0.5,1.5``). The ladder's
    LENGTH is the retry budget -- one attempt more than there are delays -- so
    lengthen it for a slow-launching server fleet and shorten it to fail fast.
    """

    from clio_agent import conf  # noqa: PLC0415

    return tuple(
        float(delay)
        for delay in conf.resolve(
            "tools.mcp.mount_retry_delays_s",
            env="CLIO_MCP_MOUNT_RETRY_DELAYS_S",
            # Spelled as strings: ``as_csv`` yields ``list[str]`` for both a YAML
            # list and a comma-separated env value, and the float coercion is the
            # comprehension above -- one conversion point, not two.
            default=["0.5", "1.5"],
            cast=conf.as_csv,
        )
    )


def namespaces_requiring_preparation(
    tool_executor: Any,
    requested_tools: list[str],
    available_tools: Mapping[str, Any],
    declared_specs: Mapping[str, Any],
) -> set[str]:
    """Return declared namespaces whose requested tools are not listed yet.

    The first model call needs the listing (a valid cached one counts). The
    session warm-up starts execution in parallel. Without known schemas, the turn
    joins that connection and its live listing before composing the request.
    """

    del tool_executor
    needed: set[str] = set()
    for name in requested_tools:
        namespace, sep, bare = name.partition("_")
        if not sep or not bare or namespace not in declared_specs:
            continue
        if name not in available_tools:
            needed.add(namespace)
    return needed


def mount_failure_reason(exc: BaseException) -> str:
    """Classify an on-demand mount failure using the discovery vocabulary."""

    from clio_agent.tools.mcp_discovery import _classify_degrade_reason  # noqa: PLC0415

    return _classify_degrade_reason(exc)


def _retryable_mount_error(exc: BaseException) -> bool:
    """Return whether a cold mount can reasonably recover after a short wait.

    A connect that stopped progressing (:class:`NoProgressTimeout`) is not retried: it
    already waited out its no-progress window, so a restart would only repeat it.
    """

    from clio_agent.tools.mcp_prepare import PrepareError  # noqa: PLC0415
    from clio_agent.tools.mcp_server_progress import NoProgressTimeout  # noqa: PLC0415

    return not isinstance(
        exc, (FileNotFoundError, PermissionError, ValueError, NoProgressTimeout, PrepareError)
    )


def _namespace_title(namespace: str) -> str:
    """Return a compact user-facing title for one MCP namespace."""

    words = namespace.replace("-", " ").replace("_", " ").split()
    label = " ".join(word.upper() if word == "ndp" else word.capitalize() for word in words)
    return f"{label or namespace} MCP"


def _publish_dependency_state(
    namespace: str,
    *,
    phase: str,
    state: str,
    attempt: int,
    max_attempts: int,
    reason: str = "",
    retry_in_ms: int | None = None,
    tool_count: int | None = None,
) -> None:
    """Publish one safe, session-scoped MCP preparation projection when available."""

    from clio_agent.gact import context as _ctx  # noqa: PLC0415 - avoid runtime cycle
    from clio_agent.gact.events import Event  # noqa: PLC0415 - lazy at readiness boundary

    app = _ctx.active_app()
    session_id = _ctx.active_session_id() or _ctx.active_tool_session_id()
    bus = getattr(getattr(app, "state", None), "bus", None)
    if not session_id or bus is None:
        return
    dependency_id = f"{session_id}:mcp:{namespace}"
    payload: dict[str, Any] = {
        "id": dependency_id,
        "session_id": session_id,
        "category": "mcp",
        "namespace": namespace,
        "title": _namespace_title(namespace),
        "phase": phase,
        "state": state,
        "attempt": attempt,
        "max_attempts": max_attempts,
    }
    if reason:
        payload["reason"] = reason
    if retry_in_ms is not None:
        payload["retry_in_ms"] = retry_in_ms
    if tool_count is not None:
        payload["tool_count"] = tool_count
    bus.publish(
        Event(
            type="infrastructure.dependency.changed",
            session_id=session_id,
            payload=payload,
        )
    )


def _listed_namespace_tools(tool_executor: Any, namespace: str) -> dict[str, Any]:
    """The tools of ``namespace`` the executor already holds (a cached listing counts)."""

    names = getattr(tool_executor, "get_tool_names", None)
    if not callable(names):
        return {}
    prefix = f"{namespace}_"
    return {name: None for name in names() if name.startswith(prefix)}


def _store_live_listing(namespace: str, spec: Any, bare: list[Any]) -> None:
    """Persist a listing taken over a live connection, so the next start needs none."""

    from clio_agent.tools import listing_cache, mcp_task_routing  # noqa: PLC0415
    from clio_agent.tools.mcp_connection_era import record_task_capability  # noqa: PLC0415

    if getattr(spec, "transport", "") != "stdio" or not getattr(spec, "command", ""):
        return
    # A tool that declares task support is definitive; its absence over a proxied
    # connection is not (the server's own capabilities are not visible through it).
    if any(mcp_task_routing._tool_task_support(tool) in ("optional", "required") for tool in bare):
        record_task_capability(namespace, task_capable=True, source="tool_execution")
    task_capable, source, era = mcp_task_routing.capability_cache_fields(namespace)
    listing_cache.store_listing(
        namespace,
        spec.command,
        tuple(spec.args),
        bare,
        spec.env,
        task_capable=task_capable,
        source=source,
        era=era,
    )


def _prepare_attempt(
    tool_executor: Any, namespace: str, spec: Any, attempt: int, max_attempts: int, phase: list[str]
) -> Mapping[str, Any]:
    """Provision, then start ``namespace`` once: connect, and list over that connection.

    ``phase[0]`` tracks the phase in progress, for the caller's typed failure.
    """

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415
    from clio_agent.tools.mcp_tools_changed import prefixed_namespace_tools  # noqa: PLC0415

    def enter(name: str, **fields: Any) -> None:
        phase[0] = name
        mcp_prepare.record(namespace, mcp_prepare.PREPARING, name, attempt=attempt, **fields)
        _publish_dependency_state(
            namespace, phase=name, state="running", attempt=attempt, max_attempts=max_attempts
        )

    enter(mcp_prepare.PHASE_PROVISION)
    outcome = mcp_prepare.provision(namespace, spec)
    listed = _listed_namespace_tools(tool_executor, namespace)
    if listed:
        enter(mcp_prepare.PHASE_CONNECT, provision=outcome)
        connector = getattr(tool_executor, "prepare_namespace", None)
        if not callable(connector):
            raise RuntimeError("live MCP executor cannot prepare namespace connections")
        connector(namespace)  # progress-based: waits while the server works
        return listed
    enter(mcp_prepare.PHASE_LAUNCH, provision=outcome)
    lister = getattr(tool_executor, "list_namespace_tools", None)
    merger = getattr(tool_executor, "merge_namespace_tools", None)
    if not callable(lister) or not callable(merger):
        raise RuntimeError("live MCP executor cannot list over a namespace connection")
    bare = lister(namespace)
    tools = prefixed_namespace_tools(namespace, bare)
    merger(namespace, tools)
    _store_live_listing(namespace, spec, bare)
    mcp_prepare.notify_listed(namespace, tools)
    return tools


def mount_namespace_for_session(
    tool_executor: Any,
    namespace: str,
    spec: Any,
    *,
    retry_delays_s: tuple[float, ...] | None = None,
    connect: bool = True,
) -> Mapping[str, Any]:
    """Prepare one declared namespace with bounded readiness semantics.

    ``connect=True`` (the session warm-up, a turn's background preparation): provision
    the server's environment (:mod:`clio_agent.tools.mcp_prepare`), then start it once,
    connecting and listing its tools over that one connection. ``connect=False`` only
    lists (joining any in-flight listing). Every phase is recorded in the namespace's
    visible preparation state; a terminal failure is recorded ``failed`` with a typed
    reason and raised to the typed tool-resolution boundary.

    Args:
        tool_executor: The live MCP executor receiving the mounted tools.
        namespace: Declared namespace to mount.
        spec: The declared server spec for ``namespace``.
        retry_delays_s: Increasing waits between attempts; one attempt more
            than there are delays is made. ``None`` resolves the configured
            ladder (:func:`mcp_mount_retry_delays_s`).

    Returns:
        The tools mounted for ``namespace``.

    Raises:
        Exception: The final mount failure, unmodified.
    """

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415
    from clio_agent.tools.mcp_discovery import (  # noqa: PLC0415
        _classify_degrade_reason,
        ensure_namespace,
    )

    if retry_delays_s is None:
        retry_delays_s = mcp_mount_retry_delays_s()
    max_attempts = len(retry_delays_s) + 1
    for attempt in range(1, max_attempts + 1):
        phase = ["launch"]
        try:
            if connect:
                mounted_tools = _prepare_attempt(
                    tool_executor, namespace, spec, attempt, max_attempts, phase
                )
            else:
                _publish_dependency_state(
                    namespace,
                    phase=phase[0],
                    state="running",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                mounted_tools = ensure_namespace(namespace, spec)
                merger = getattr(tool_executor, "merge_namespace_tools", None)
                if not callable(merger):
                    raise RuntimeError("live MCP executor cannot accept mounted tools")
                merger(namespace, mounted_tools)
        except Exception as exc:
            # A preparation names its failures in the preparation vocabulary; a
            # listing-only mount keeps the discovery vocabulary it always used.
            reason = (
                mcp_prepare.classify(exc, phase[0]) if connect else _classify_degrade_reason(exc)
            )
            if attempt < max_attempts and _retryable_mount_error(exc):
                delay = retry_delays_s[attempt - 1]
                retry_in_ms = round(delay * 1_000)
                # Every retried attempt is a typed loud warning -- never silent.
                logger.warning(
                    "reason=%s namespace=%s phase=%s attempt=%d/%d "
                    "degrade_reason=%s retry_in_ms=%d",
                    MCP_MOUNT_RETRY_REASON,
                    namespace,
                    phase[0],
                    attempt,
                    max_attempts,
                    _classify_degrade_reason(exc),
                    retry_in_ms,
                )
                if connect:
                    mcp_prepare.record(
                        namespace,
                        mcp_prepare.PREPARING,
                        phase[0],
                        reason=reason,
                        detail=getattr(exc, "detail", "") or str(exc),
                        attempt=attempt,
                    )
                _publish_dependency_state(
                    namespace,
                    phase="retry",
                    state="retrying",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=reason,
                    retry_in_ms=retry_in_ms,
                )
                time.sleep(delay)
                continue
            if connect:
                mcp_prepare.record(
                    namespace,
                    mcp_prepare.FAILED,
                    phase[0],
                    reason=reason,
                    detail=getattr(exc, "detail", "") or str(exc),
                    attempt=attempt,
                )
            _publish_dependency_state(
                namespace,
                phase=phase[0],
                state="failed",
                attempt=attempt,
                max_attempts=max_attempts,
                reason=reason,
            )
            raise
        break
    else:
        raise AssertionError("MCP readiness attempts exhausted without a result")
    if connect:
        mcp_prepare.record(
            namespace, mcp_prepare.READY, phase[0], attempt=attempt, tool_count=len(mounted_tools)
        )
    _publish_dependency_state(
        namespace,
        phase=phase[0],
        state="ready",
        attempt=attempt,
        max_attempts=max_attempts,
        tool_count=len(mounted_tools),
    )
    return mounted_tools


def mount_namespaces_for_session(
    tool_executor: Any, specs: Mapping[str, Any], *, connect: bool
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    """Mount several declared namespaces at the same time.

    Each namespace goes through :func:`mount_namespace_for_session` on its own worker
    (in a copy of the caller's context), so one slow server never delays another.

    Returns:
        ``(mounted, failures)``: the tools mounted per namespace, and a typed reason
        per namespace that failed (logged; a later call tries it again).
    """

    import contextvars  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from clio_agent.gact import spotter_mount_outcome  # noqa: PLC0415

    if not specs:
        return {}, {}
    mounted: dict[str, Mapping[str, Any]] = {}
    failures: dict[str, str] = {}
    if connect:
        # A preparation joins the one already in flight for the same fleet (a turn's,
        # another warm-up's) instead of starting the server a second time.
        from clio_agent.gact import mcp_background_prepare as background  # noqa: PLC0415

        pending = {
            namespace: background.prepare_in_background(tool_executor, namespace, spec)
            for namespace, spec in specs.items()
        }
        for namespace, prepared in pending.items():
            exc = prepared.exception()  # waits; a failure is a value here, never raised
            if exc is None:
                mounted[namespace] = prepared.result()
                spotter_mount_outcome.record_success(namespace)
                continue
            failures[namespace] = background.failure_reason(namespace, exc)
            spotter_mount_outcome.record_failure(namespace, failures[namespace])
            logger.warning(
                "mcp_prepare_failed namespace=%s reason=%s error=%s",
                namespace,
                failures[namespace],
                exc,
            )
        return mounted, failures
    with ThreadPoolExecutor(max_workers=len(specs), thread_name_prefix="clio-mount") as pool:
        futures = {
            namespace: pool.submit(
                contextvars.copy_context().run,
                mount_namespace_for_session,
                tool_executor,
                namespace,
                spec,
                connect=connect,
            )
            for namespace, spec in specs.items()
        }
        for namespace, future in futures.items():
            try:
                mounted[namespace] = future.result()
                spotter_mount_outcome.record_success(namespace)
            except Exception as exc:  # noqa: BLE001 - typed + named, never cached
                failures[namespace] = mount_failure_reason(exc)
                spotter_mount_outcome.record_failure(namespace, failures[namespace])
                logger.warning(
                    "mcp_mount_failed namespace=%s reason=%s error=%s",
                    namespace,
                    failures[namespace],
                    exc,
                )
    return mounted, failures
