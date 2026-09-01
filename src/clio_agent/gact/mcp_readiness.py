"""Readiness boundary for cold MCP namespaces.

The discovery layer intentionally stays process-scoped and single-flight.  This
module adds the per-session boundary around a selected session's cold mount:
bounded increasing-wait retries with a typed reason on every retried attempt.
It never installs an undeclared server and never exposes raw subprocess errors.

A terminal failure raises to the existing typed tool-resolution boundary, where
:func:`clio_agent.gact.agents.builders._resolve_requested_tools` records it in
the ``mount_failures`` map the ``_UnsupportedSessionAgent`` reason carries --
that is the lane with a real consumer, so this module adds no wire vocabulary
of its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: Typed reason for one retried cold mount (queryable in logs/trace).
MCP_MOUNT_RETRY_REASON = "mcp_mount_retry"

MCP_MOUNT_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.5)
MCP_MOUNT_TIMEOUT_MULTIPLIERS: tuple[float, ...] = (1.0, 3.0, 6.0)


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


def mount_namespace_for_session(
    tool_executor: Any,
    namespace: str,
    spec: Any,
    *,
    retry_delays_s: tuple[float, ...] = MCP_MOUNT_RETRY_DELAYS_S,
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
            than there are delays is made.

    Returns:
        The tools mounted for ``namespace``.

    Raises:
        Exception: The final mount failure, unmodified.
    """

    from clio_agent.tools.mcp_discovery import (  # noqa: PLC0415
        _classify_degrade_reason,
        ensure_namespace,
    )

    max_attempts = len(retry_delays_s) + 1
    base_setup_timeout = float(getattr(tool_executor, "_setup_timeout", 10.0))
    for attempt in range(1, max_attempts + 1):
        phase = "launch"
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
            multiplier = MCP_MOUNT_TIMEOUT_MULTIPLIERS[
                min(attempt - 1, len(MCP_MOUNT_TIMEOUT_MULTIPLIERS) - 1)
            ]
            connector(namespace, timeout=base_setup_timeout * multiplier)
        except Exception as exc:
            if attempt < max_attempts and _retryable_mount_error(exc):
                delay = retry_delays_s[attempt - 1]
                # Every retried attempt is a typed loud warning -- never silent.
                logger.warning(
                    "reason=%s namespace=%s phase=%s attempt=%d/%d "
                    "degrade_reason=%s retry_in_ms=%d",
                    MCP_MOUNT_RETRY_REASON,
                    namespace,
                    phase,
                    attempt,
                    max_attempts,
                    _classify_degrade_reason(exc),
                    round(delay * 1_000),
                )
                time.sleep(delay)
                continue
            raise
        return mounted_tools
    raise AssertionError("MCP readiness attempts exhausted without a result")
