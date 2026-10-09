"""Recorded metadata construction for live tool transcript parts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.evidence import _bounded_tool_call_result, _tool_result_preview


def completed_tool_text(result: Any, error: str | None) -> str:
    """Describe recorded failure or retain the existing bounded result preview."""
    return error or (_tool_result_preview(result) if result is not None else "completed")


def completed_tool_metadata(
    result: Any,
    *,
    completed_at: str,
    cancellation: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Retain bounded result evidence, timing and recorded provenance on completion."""
    return {
        "stream_source": "live",
        "completed_at": completed_at,
        "telemetry_source": "live_observer",
        **({"result": _bounded_tool_call_result(result)} if result is not None else {}),
        **cancellation,
        **provenance,
    }
