"""Session-visible readiness for cold MCP namespaces.

The discovery layer intentionally stays process-scoped and single-flight.  This
module adds the product-facing boundary around a selected session's cold mount:
bounded increasing-wait retries plus sanitized GACT lifecycle events.  It never
installs an undeclared server and never exposes raw subprocess errors on the
public wire.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.events import Event

MCP_MOUNT_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.5)


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


def _session_id() -> str:
    """Return the session that owns the current tool-setup operation."""

    return _ctx.active_session_id() or _ctx.active_tool_session_id()


def _publish_status(
    namespace: str,
    spec: Any,
    *,
    phase: str,
    state: str,
    attempt: int,
    max_attempts: int,
    reason: str = "",
    retry_in_s: float | None = None,
    tool_count: int | None = None,
) -> None:
    """Publish one sanitized dependency lifecycle update for the live session."""

    app = _ctx.active_app()
    session_id = _session_id()
    bus = getattr(getattr(app, "state", None), "bus", None) if app is not None else None
    if not session_id or bus is None:
        return
    payload: dict[str, Any] = {
        "id": f"{session_id}:mcp:{namespace}",
        "session_id": session_id,
        "category": "mcp",
        "namespace": namespace,
        "title": str(getattr(spec, "name", "") or namespace),
        "phase": phase,
        "state": state,
        "attempt": attempt,
        "max_attempts": max_attempts,
    }
    if reason:
        payload["reason"] = reason
    if retry_in_s is not None:
        payload["retry_in_ms"] = round(retry_in_s * 1_000)
    if tool_count is not None:
        payload["tool_count"] = tool_count
    bus.publish(
        Event(
            type="infrastructure.dependency.changed",
            session_id=session_id,
            payload=payload,
        )
    )


def _retryable_mount_error(exc: BaseException) -> bool:
    """Return whether a cold mount can reasonably recover after a short wait."""

    return not isinstance(exc, (FileNotFoundError, PermissionError, ValueError))


def mount_namespace_for_session(
    tool_executor: Any,
    namespace: str,
    spec: Any,
    *,
    retry_delays_s: tuple[float, ...] = MCP_MOUNT_RETRY_DELAYS_S,
) -> Mapping[str, Any]:
    """Mount one declared namespace with visible, bounded readiness semantics.

    The first turn waits for this bounded preparation instead of immediately
    failing while asynchronous discovery is still warming.  A terminal failure
    is still raised to the existing typed tool-resolution boundary.
    """

    from clio_agent.tools.mcp_discovery import (  # noqa: PLC0415
        _classify_degrade_reason,
        ensure_namespace,
    )

    max_attempts = len(retry_delays_s) + 1
    for attempt in range(1, max_attempts + 1):
        phase = "launch"
        _publish_status(
            namespace,
            spec,
            phase="launch",
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
            _publish_status(
                namespace,
                spec,
                phase=phase,
                state="running",
                attempt=attempt,
                max_attempts=max_attempts,
                tool_count=len(mounted_tools),
            )
            connector(namespace)
        except Exception as exc:
            reason = _classify_degrade_reason(exc)
            if attempt < max_attempts and _retryable_mount_error(exc):
                delay = retry_delays_s[attempt - 1]
                _publish_status(
                    namespace,
                    spec,
                    phase=phase,
                    state="retrying",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=reason,
                    retry_in_s=delay,
                )
                time.sleep(delay)
                continue
            _publish_status(
                namespace,
                spec,
                phase=phase,
                state="failed",
                attempt=attempt,
                max_attempts=max_attempts,
                reason=reason,
            )
            raise
        _publish_status(
            namespace,
            spec,
            phase="connect",
            state="ready",
            attempt=attempt,
            max_attempts=max_attempts,
            tool_count=len(mounted_tools),
        )
        return mounted_tools
    raise AssertionError("MCP readiness attempts exhausted without a result")
