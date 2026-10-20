"""Clarify old structured observations without executing tools or changing their ledger."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.agents.native_presenters_inspection import INSPECTION_PRESENTERS

_CONTRACTS = {
    "prepare_execution_runtime": "runtime_inventory",
    "prepare_document_runtime": "runtime_inventory",
    "prepare_document": "document_inspection",
    "get_weather_forecast": "weather_forecast",
    "inspect_a2ui_surface": "surface_inspection",
    "capture_a2ui_surface": "surface_capture",
    "publish_dashboard_report": "dashboard_report",
    "delete_a2ui_surface": "surface_deletion",
    "set_goal": "goal_started",
    "set_loop": "loop_started",
    "wait_for": "goal_wait",
    "connected_data_open": "source_opened",
    "connected_data_write": "source_written",
}


def legacy_inspection_presentation(
    part: Mapping[str, Any], args: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Present retained structured results only where the old declaration lost their meaning."""
    name = str(part.get("tool_name") or "")
    if part.get("type") != "tool_result" or part.get("is_error") or name not in _CONTRACTS:
        return None
    view = part.get("presentation")
    if not isinstance(view, Mapping):
        return None
    blocks = view.get("blocks") or []
    # The old runtime presenter exposed a handful of scalar fields, but discarded
    # nested import checks. Other migrated tools had no observer blocks at all.
    if blocks and not (
        name in {"prepare_execution_runtime", "prepare_document_runtime"}
        and {block.get("id") for block in blocks}
        <= {
            "python",
            "node",
            "output_directory",
            "status",
            "os",
            "architecture",
            "python_version",
            "node_version",
        }
    ):
        return None
    metadata = part.get("metadata") or {}
    row = part.get("structured_content") or (
        metadata.get("result") if isinstance(metadata, Mapping) else None
    )
    if not isinstance(row, Mapping):
        return None
    if row.get("truncated") and "preview" in row:
        return {
            "summary": "Only a partial result was retained",
            "status": "degraded",
            "blocks": [
                {
                    "id": "recorded-limit",
                    "type": "text",
                    "severity": "warning",
                    "text": "This older activity record does not retain the complete structured result. Inspect its recorded preview in technical details.",
                }
            ],
        }
    inputs = args or {}
    if isinstance(inputs.get("kwargs"), Mapping):
        inputs = inputs["kwargs"]
    return INSPECTION_PRESENTERS[_CONTRACTS[name]](inputs, row)
