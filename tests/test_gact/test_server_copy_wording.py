"""Server copy shown in the UI reads as plain text, without a middle dot.

Pins the wording of the presenters rewritten when the "·" separators were removed:
task progress, workbook issues, widget controls and MCP media labels.
"""

from __future__ import annotations

from clio_agent.gact.agents.native_presenters_inspection import (
    document_inspection,
    surface_inspection,
)
from clio_agent.gact.agents.native_presenters_tasks import _task_blocks
from clio_agent.tools.tool_presentation import standard_mcp_presentation


def _status_text(row: dict) -> str:
    return next(b["text"] for b in _task_blocks(row, 1) if b["id"] == "task-1-status")


def test_task_progress_puts_the_count_in_parentheses() -> None:
    row = {
        "status": "running",
        "progress": {"message": "Downloading", "completed": 3, "total": 10, "unit": "files"},
    }
    assert _status_text(row) == "running — Downloading (3 / 10 files)"


def test_task_progress_without_message_shows_the_count() -> None:
    assert _status_text({"status": "running", "progress": {"completed": 3}}) == "running — 3"


def test_workbook_issue_names_sheet_and_cell() -> None:
    view = document_inspection(
        {"action": "recalculate"},
        {"issues": [{"part": "Sheet1", "cell": "B3", "error": "div_zero"}, {"error": "x"}]},
    )
    issues = next(b for b in view["blocks"] if b["id"] == "calculation-issues")
    assert issues["text"] == "Sheet1, cell B3: div zero\nWorkbook: x"


def test_widget_control_names_property_in_parentheses() -> None:
    view = surface_inspection(
        {"surface_id": "s1"},
        {"controls": [{"component_id": "chart", "property": "zoom", "path": "/view/zoom"}]},
    )
    controls = next(b for b in view["blocks"] if b["id"] == "controls")
    assert controls["text"] == "chart (zoom) → /view/zoom"


def test_media_label_puts_the_type_in_parentheses() -> None:
    view = standard_mcp_presentation(
        {
            "content": [
                {"type": "image", "mimeType": "image/png", "data": "AAA"},
                {"type": "audio", "data": "B"},
            ]
        }
    )
    assert [b["label"] for b in view["blocks"]] == ["Image (image/png)", "Audio"]
