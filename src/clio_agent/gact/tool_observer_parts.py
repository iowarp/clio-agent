"""Recorded metadata construction for live tool transcript parts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.evidence import _bounded_tool_call_result, _tool_result_preview


def completed_tool_text(result: Any, error: str | None) -> str:
    """Describe recorded failure or retain the existing bounded result preview."""
    return error or (_tool_result_preview(result) if result is not None else "completed")


#: Unsuccessful completions that are not failures of the tool: it was refused before it
#: ran, or the person stopped it. Recorded as ``execution_outcome`` on the result part.
NON_FAILURE_OUTCOMES = frozenset({"cancelled", "denied"})


def completed_tool_outcome(error: str | None, result: Any, *, refused: bool) -> str | None:
    """``denied`` for a refused call, ``cancelled`` for one stopped by cancellation;
    ``None`` when the call succeeded or failed on its own."""
    if error is None:
        return None
    if refused:
        return "denied"
    trace = result.get("error") if isinstance(result, Mapping) else None
    if isinstance(trace, Mapping) and trace.get("error") == "cancelled":
        return "cancelled"  # a CancellationError's typed trace (tools/execution.py)
    if error in {"CancelledError", "cancelled"} or "cancelled by client" in error:
        return "cancelled"
    return None


def completed_tool_metadata(
    result: Any,
    *,
    completed_at: str,
    cancellation: Mapping[str, Any],
    provenance: Mapping[str, Any],
    outcome: str | None = None,
) -> dict[str, Any]:
    """Retain bounded result evidence, timing and recorded provenance on completion."""
    return {
        "stream_source": "live",
        "completed_at": completed_at,
        "telemetry_source": "live_observer",
        **({"execution_outcome": outcome} if outcome else {}),
        **({"result": _bounded_tool_call_result(result)} if result is not None else {}),
        # A plugin view's event from the result's ``_meta`` (tools/mcp_results.py).
        **(
            {"ui_event": result["uiEvent"]}
            if isinstance(result, Mapping) and "uiEvent" in result
            else {}
        ),
        **cancellation,
        **provenance,
    }
