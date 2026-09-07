"""Failure-isolated tool-observer notification and progress correlation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Optional

logger = logging.getLogger(__name__)

ToolObserver = Callable[
    [str, Mapping[str, Any], Optional[str], Optional[str], Any | None],
    Any | None,
]
LegacyToolObserver = Callable[[str, Mapping[str, Any], Optional[str], Optional[str]], None]


def notify_tool_observer(
    observer: Optional[ToolObserver | LegacyToolObserver],
    name: str,
    args: Mapping[str, Any],
    phase: str,
    error: str | None = None,
    result: Any | None = None,
) -> Any | None:
    """Notify an observer without allowing telemetry failure to break execution."""

    if observer is None:
        return None
    try:
        if result is None:
            return observer(name, dict(args), phase, error)  # type: ignore[misc, call-arg, no-any-return]
        try:
            return observer(  # type: ignore[misc, call-arg, no-any-return]
                name, dict(args), phase, error, result
            )
        except TypeError:
            return observer(name, dict(args), phase, error)  # type: ignore[misc, call-arg, no-any-return]
    except Exception as exc:  # noqa: BLE001 - observers must never break tool execution
        logger.warning(
            "tool observer raised; its view of this call is lost "
            "reason=tool_observer_failed tool=%s phase=%s error=%s",
            name,
            phase,
            exc,
        )
        return None


def observer_progress_handler(
    observer: Optional[ToolObserver | LegacyToolObserver],
    name: str,
    args: Mapping[str, Any],
    observer_handle: object,
) -> Callable[[float, float | None, str | None], Awaitable[None]]:
    """Return an async MCP progress callback carrying the observer's start handle."""

    async def forward(progress: float, total: float | None, message: str | None) -> None:
        notify_tool_observer(
            observer,
            name,
            args,
            "progress",
            result={
                "observer_handle": observer_handle,
                "progress": progress,
                "total": total,
                "message": message,
            },
        )

    return forward


__all__ = [
    "LegacyToolObserver",
    "ToolObserver",
    "notify_tool_observer",
    "observer_progress_handler",
]
