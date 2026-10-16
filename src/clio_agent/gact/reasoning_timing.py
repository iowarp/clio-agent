"""Observed reasoning-phase time, kept separate from tool execution."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from clio_agent.gact.types import Part


class ReasoningClock:
    """Time one open reasoning passage with a monotonic clock.

    This measures CLIO's observed phase from its first delta to closure, not
    provider compute time or scheduling before the first reasoning delta.
    """

    def __init__(self) -> None:
        self._open: tuple[str, float] | None = None

    def start(self, part: Part) -> None:
        """Record the actual first-delta boundary for a reasoning part only."""
        if part.type == "thinking":
            self._open = (part.id, time.perf_counter())
            part.metadata["started_at"] = datetime.now(timezone.utc).isoformat()

    def finish(self, part: Part) -> None:
        """Close the observed phase once; no timing is invented for batch text."""
        if self._open is None or self._open[0] != part.id:
            return
        part.duration_ms = max(0.0, (time.perf_counter() - self._open[1]) * 1000)
        part.metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
        self._open = None


def reasoning_timing(part: dict[str, object]) -> dict[str, object]:
    """Project optional recorded timing without borrowing another part's time."""
    metadata = part.get("metadata")
    result: dict[str, object] = {}
    if isinstance(metadata, dict):
        for key in ("started_at", "completed_at"):
            if isinstance(metadata.get(key), str):
                result[key] = metadata[key]
    duration = part.get("duration_ms")
    if (
        result.get("started_at")
        and result.get("completed_at")
        and (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and 0 <= duration < float("inf")
        )
    ):
        result["duration_ms"] = float(duration)
    return result
