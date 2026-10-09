"""Publish agent-authored reports without making composition choices for the agent."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.dashboard_reports import publish_dashboard_report


def build_publish_dashboard_report_tool() -> Any:
    """Build the report publishing tool for agents that can author A2UI."""

    def publish_report(definition_path: str, report_id: str = "") -> dict[str, Any]:
        """Publish or revise an agent-authored dashboard as a clickable report artifact.

        Load create-dashboard for the document format and authoring workflow. Publish
        the workspace definition as an artifact in the side panel; pass report_id
        when revising an existing dashboard. Use the active A2UI catalog and shared data model.
        """
        active = _common.active_app_and_session()
        if isinstance(active, dict):
            return active
        app, sid = active
        try:
            return publish_dashboard_report(
                app, sid, definition_path=definition_path, report_id=report_id
            )
        except (OSError, ValueError) as exc:
            return refusal("dashboard_report_failed", detail=str(exc))

    return native_tool(
        publish_report,
        name="publish_dashboard_report",
        title="Save dashboard report",
        domain="surfaces",
        presentation="text",
        read_only=False,
        desc=publish_report.__doc__,
        args={
            "definition_path": {
                "type": "string",
                "description": "Authored dashboard JSON document inside the workspace.",
            },
            "report_id": {
                "type": "string",
                "description": "Existing report to revise; empty for a new report.",
            },
        },
    )
