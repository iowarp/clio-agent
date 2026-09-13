"""Readiness boundary for cold MCP namespaces.

The discovery layer intentionally stays process-scoped and single-flight.  This
module adds the per-session boundary around a selected session's cold mount:
bounded increasing-wait retries with a typed reason on every retried attempt and
session-scoped progress events for the UI. It never installs an undeclared server
and never exposes raw subprocess errors.

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

MCP_MOUNT_TIMEOUT_MULTIPLIERS: tuple[float, ...] = (1.0, 3.0, 6.0)


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


def mcp_mount_setup_timeout_s() -> float:
    """The base per-namespace connect timeout when the executor exposes none.

    Resolved from the SAME key the executor's own default comes from
    (``tools.mcp.setup_timeout_s`` / ``CLIO_MCP_SETUP_TIMEOUT_S``), so this
    boundary can never diverge from the timeout the live executor was built
    with (there is one source for the semantic, not two).
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "tools.mcp.setup_timeout_s",
        env="CLIO_MCP_SETUP_TIMEOUT_S",
        default=10.0,
        cast=conf.as_float,
    )


def namespaces_requiring_preparation(
    tool_executor: Any,
    requested_tools: list[str],
    available_tools: Mapping[str, Any],
    declared_specs: Mapping[str, Any],
) -> set[str]:
    """Return declared namespaces whose requested tools are absent or disconnected."""

    namespace_prepared = getattr(tool_executor, "is_namespace_prepared", None)
    needed: set[str] = set()
    for name in requested_tools:
        namespace, sep, bare = name.partition("_")
        if not sep or not bare or namespace not in declared_specs:
            continue
        prepared = callable(namespace_prepared) and namespace_prepared(namespace)
        if name not in available_tools or not prepared:
            needed.add(namespace)
    return needed


def mount_failure_reason(exc: BaseException) -> str:
    """Classify an on-demand mount failure using the discovery vocabulary."""

    from clio_agent.tools.mcp_discovery import _classify_degrade_reason  # noqa: PLC0415

    return _classify_degrade_reason(exc)


def _retryable_mount_error(exc: BaseException) -> bool:
    """Return whether a cold mount can reasonably recover after a short wait."""

    return not isinstance(exc, (FileNotFoundError, PermissionError, ValueError))


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


def mount_namespace_for_session(
    tool_executor: Any,
    namespace: str,
    spec: Any,
    *,
    retry_delays_s: tuple[float, ...] | None = None,
) -> Mapping[str, Any]:
    """Mount one declared namespace with bounded readiness semantics.

    The first turn waits for this bounded preparation instead of immediately
    failing while asynchronous discovery is still warming.  A terminal failure
    is still raised to the existing typed tool-resolution boundary.

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

    from clio_agent.tools.mcp_discovery import (  # noqa: PLC0415
        _classify_degrade_reason,
        ensure_namespace,
    )

    if retry_delays_s is None:
        retry_delays_s = mcp_mount_retry_delays_s()
    max_attempts = len(retry_delays_s) + 1
    configured_setup_timeout = getattr(tool_executor, "_setup_timeout", None)
    base_setup_timeout = (
        float(configured_setup_timeout)
        if configured_setup_timeout is not None
        else mcp_mount_setup_timeout_s()
    )
    for attempt in range(1, max_attempts + 1):
        phase = "launch"
        _publish_dependency_state(
            namespace,
            phase=phase,
            state="running",
            attempt=attempt,
            max_attempts=max_attempts,
        )
        try:
            mounted_tools = ensure_namespace(namespace, spec)
            merger = getattr(tool_executor, "merge_namespace_tools", None)
            if not callable(merger):
                raise RuntimeError("live MCP executor cannot accept mounted tools")
            merger(namespace, mounted_tools)
            connector = getattr(tool_executor, "prepare_namespace", None)
            if not callable(connector):
                raise RuntimeError("live MCP executor cannot prepare namespace connections")
            phase = "connect"
            _publish_dependency_state(
                namespace,
                phase=phase,
                state="running",
                attempt=attempt,
                max_attempts=max_attempts,
            )
            multiplier = MCP_MOUNT_TIMEOUT_MULTIPLIERS[
                min(attempt - 1, len(MCP_MOUNT_TIMEOUT_MULTIPLIERS) - 1)
            ]
            connector(namespace, timeout=base_setup_timeout * multiplier)
        except Exception as exc:
            if attempt < max_attempts and _retryable_mount_error(exc):
                delay = retry_delays_s[attempt - 1]
                reason = _classify_degrade_reason(exc)
                retry_in_ms = round(delay * 1_000)
                # Every retried attempt is a typed loud warning -- never silent.
                logger.warning(
                    "reason=%s namespace=%s phase=%s attempt=%d/%d "
                    "degrade_reason=%s retry_in_ms=%d",
                    MCP_MOUNT_RETRY_REASON,
                    namespace,
                    phase,
                    attempt,
                    max_attempts,
                    reason,
                    retry_in_ms,
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
            _publish_dependency_state(
                namespace,
                phase=phase,
                state="failed",
                attempt=attempt,
                max_attempts=max_attempts,
                reason=_classify_degrade_reason(exc),
            )
            raise
        _publish_dependency_state(
            namespace,
            phase="connect",
            state="ready",
            attempt=attempt,
            max_attempts=max_attempts,
            tool_count=len(mounted_tools),
        )
        return mounted_tools
    raise AssertionError("MCP readiness attempts exhausted without a result")
