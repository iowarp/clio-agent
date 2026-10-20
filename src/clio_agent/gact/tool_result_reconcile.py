"""Reconcile final tool text without mixing separate invocations or retries."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.evidence import _tool_result_preview
from clio_agent.gact.types import Part


def reconcile_tool_result_text(parts: list[Part], rows: list[dict[str, Any]]) -> None:
    """Use invocation identity, with only unambiguous legacy argument matching."""
    calls = {part.call_id: part for part in parts if part.type == "tool_call" and part.call_id}
    identified = {str(row["call_id"]): row for row in rows if row.get("call_id")}
    legacy = [row for row in rows if not row.get("call_id")]
    for part in parts:
        if part.type != "tool_result" or not part.call_id or part.is_error:
            continue
        call = calls.get(part.call_id)
        if call is None:
            continue
        row = identified.get(part.call_id)
        if row is None:
            candidates = [
                item
                for item in legacy
                if str(item.get("name") or "") == call.tool_name and item.get("args") == call.input
            ]
            # Repeated calls with identical arguments are separate observations.
            same_calls = [
                item
                for item in calls.values()
                if item.tool_name == call.tool_name and item.input == call.input
            ]
            if len(candidates) != 1 or len(same_calls) != 1:
                continue
            row = candidates[0]
        if "result" not in row or row.get("ok") is False:
            continue
        part.content = [
            Part(
                id=f"{part.id}_final_text",
                type="text",
                text=_tool_result_preview(row["result"]),
            )
        ]
