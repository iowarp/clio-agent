"""Three-level tool observations: readable intent, inspectable facts and untouched raw evidence."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.agents.skill_presentation import legacy_skill_presentation, skill_presentation
from clio_agent.gact.tool_result_presentation import ToolPresentation


def test_dictionary_tools_cannot_use_the_string_only_presenter() -> None:
    """A declared presenter must support the callback's actual result type."""
    root = Path(__file__).parents[2] / "src/clio_agent"
    invalid = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id != "native_tool"
            ):
                continue
            presenter = next((kw.value for kw in node.keywords if kw.arg == "presentation"), None)
            if not isinstance(presenter, ast.Constant) or presenter.value != "text":
                continue
            callback = (
                functions.get(node.args[0].id)
                if node.args and isinstance(node.args[0], ast.Name)
                else None
            )
            if (
                callback is None
                or callback.returns is None
                or ast.unparse(callback.returns) != "str"
            ):
                invalid.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not invalid, (
        "String-only presenters cannot describe structured tool results: " + ", ".join(invalid)
    )


@pytest.mark.parametrize(
    ("declaration", "args", "result", "expected"),
    [
        (
            "runtime_inventory",
            {"required_imports": ["pandas", "cv2"]},
            {
                "status": "missing_dependencies",
                "python_version": "3.14",
                "required_imports": {
                    "pandas": {"status": "ready"},
                    "cv2": {"status": "missing", "detail": "Module not found"},
                },
            },
            "cv2",
        ),
        (
            "document_inspection",
            {"path": "report.docx", "action": "render", "pages": "2-4"},
            {
                "status": "complete",
                "manifest": "/work/manifest.json",
                "images": [
                    {"page": 2, "path": "2.png"},
                    {"page": 3, "path": "3.png"},
                    {"page": 4, "path": "4.png"},
                ],
            },
            "2-4",
        ),
        (
            "weather_forecast",
            {"location": "Chicago"},
            {
                "ok": True,
                "weather": {
                    "location": "Chicago, Illinois",
                    "temperatureUnit": "F",
                    "daily": [{"date": "2030-01-02", "condition": "Clear", "high": 40, "low": 20}],
                    "hourly": [],
                },
            },
            "Chicago, Illinois",
        ),
        (
            "surface_inspection",
            {},
            {"surfaces": [{"surface_id": "surface-1", "title": "Seasonal snowfall"}]},
            "Seasonal snowfall",
        ),
        (
            "surface_inspection",
            {"surface_id": "surface-1"},
            {
                "surface_id": "surface-1",
                "viewers": [
                    {
                        "title": "Lake shoreline",
                        "ready": False,
                        "visible": True,
                        "error": "Geometry unavailable",
                    }
                ],
                "controls": [
                    {
                        "component_id": "map",
                        "property": "selection",
                        "path": "/selection",
                        "owner": "data_model",
                        "schema": "catalog.json#/components/Map",
                    }
                ],
            },
            "Geometry unavailable",
        ),
        (
            "surface_capture",
            {},
            {
                "artifact_id": "capture-1",
                "path": "/work/map.png",
                "media_type": "image/png",
                "sha256": "a" * 64,
            },
            "Captured widget image",
        ),
        (
            "dashboard_report",
            {"definition_path": "lake.json"},
            {"artifact_id": "report-1", "title": "Lake comparison"},
            "Lake comparison",
        ),
        (
            "surface_deletion",
            {"surface_id": "surface-1"},
            {"surface_id": "surface-1", "deleted": True},
            "Widget deleted",
        ),
        (
            "goal_started",
            {"condition": "Compare three winters"},
            {"goal_id": "goal-1", "condition": "Compare three winters"},
            "Compare three winters",
        ),
        (
            "loop_started",
            {"prompt": "Check the new measurements"},
            {"loop_id": "loop-1", "interval_s": 60, "next_fire_at": "2030-01-02T10:00:00Z"},
            "Check the new measurements",
        ),
        (
            "goal_wait",
            {},
            {
                "waiting": True,
                "tasks": ["task-1", "task-2"],
                "return_when": "all",
                "message": "Finish this turn",
            },
            "All requested tasks settle",
        ),
    ],
)
def test_inventory_receipts_have_human_facts_without_transport_settings(
    declaration: str, args: dict[str, Any], result: dict[str, Any], expected: str
) -> None:
    args.update(timeout_s=9999, max_output_bytes=9999)
    result["unrelated_transport_debug"] = "RAW_ONLY_SENTINEL"
    original = copy.deepcopy(result)
    view = ToolPresentation.model_validate(native_presentation(declaration, args, result, None))
    text = view.model_dump_json()
    assert expected in text
    assert view.blocks and view.summary
    assert "RAW_ONLY_SENTINEL" not in text and "9999" not in text
    assert result == original


def test_inventory_and_widget_results_explain_actual_check_shapes() -> None:
    runtime = native_presentation(
        "runtime_inventory",
        {},
        {
            "status": "missing_dependencies",
            "required_imports": {"cv2": {"status": "missing", "detail": "No module named cv2"}},
            "packages": {"numpy": {"version": "2.0", "verification": "module_discovered"}},
        },
        None,
    )
    assert "No module named cv2" in json.dumps(runtime)
    assert "module discovered" in json.dumps(runtime)
    assert runtime["status"] == "degraded"
    surface = native_presentation(
        "surface_inspection",
        {},
        {
            "surface_id": "map",
            "components": [{"title": "Lake boundaries"}],
            "viewers": [{"ready": False, "visible": True}],
            "controls": [
                {
                    "component_id": "map",
                    "property": "selection",
                    "path": "/selection",
                    "owner": "data_model",
                }
            ],
        },
        None,
    )
    assert surface["status"] == "degraded"
    assert surface["blocks"][0]["label"] == "Lake boundaries"
    controls = next(block["text"] for block in surface["blocks"] if block["id"] == "controls")
    assert "map" in controls and "/selection" in controls and "{'" not in controls


@pytest.mark.parametrize(
    "declaration",
    [
        "surface_capture",
        "surface_inspection",
        "surface_deletion",
        "dashboard_report",
        "weather_forecast",
        "document_inspection",
    ],
)
def test_inspection_failures_remain_visible(declaration: str) -> None:
    view = native_presentation(
        declaration,
        {},
        {
            "ok": False,
            "reason": "not_available",
            "detail": "The selected view was not available",
            "hint": "Choose a current view",
        },
        None,
    )
    assert view["status"] == "failed"
    assert "The selected view was not available" in json.dumps(view)
    assert "Choose a current view" in json.dumps(view)


def test_skill_references_have_exact_paths_and_main_procedure_has_a_purpose() -> None:
    metadata = {
        "skill_id": "work-with-ndp",
        "skill_title": "Find, access and analyze scientific data",
        "skill_description": "Discover scientific collections and analyze their observations.",
        "path": "/skills/work-with-ndp/SKILL.md",
    }
    main = skill_presentation(
        {"skill_id": "work-with-ndp"},
        "# Skill: work-with-ndp\nSkill directory (use as SKILL_ROOT): /skills/work-with-ndp\n# Work with NDP\nRead measurements",
        metadata,
    )
    reference = skill_presentation(
        {"skill_id": "work-with-ndp", "file": "references/catalog.md"},
        "# Catalog discovery\nUse the catalog",
        {
            **metadata,
            "file": "references/catalog.md",
            "file_paths": {"references/catalog.md": "/skills/work-with-ndp/references/catalog.md"},
        },
    )
    assert main["blocks"][0]["text"] == "Find, access and analyze scientific data"
    assert reference["action"] == "Read skill reference"
    assert reference["blocks"][0]["text"] == "references/catalog.md"
    assert next(block for block in reference["blocks"] if block["type"] == "link")["uri"].endswith(
        "/references/catalog.md"
    )
    assert "Skill directory (use as SKILL_ROOT)" not in json.dumps(main)
    assert "Read measurements" in json.dumps(main)


def test_skill_batch_counts_distinct_files_and_never_links_an_unknown_reference_to_skill_md() -> (
    None
):
    args = {
        "skill_id": "catalog",
        "file": "catalog.json#/components/Chart",
        "files": ["catalog.json#/components/Chart", "catalog.json#/components/Map"],
    }
    view = skill_presentation(args, "Both component schemas", {"path": "/skills/catalog/SKILL.md"})
    assert view["blocks"][0]["text"] == "2 reference files"
    assert not any(block["type"] == "link" for block in view["blocks"])


def test_historical_skill_reference_presentation_uses_recorded_evidence_only() -> None:
    part = {
        "type": "tool_result",
        "tool_name": "load_skill",
        "presentation": {
            "subject": "skill-name",
            "summary": "",
            "blocks": [
                {"id": "skill-name", "type": "text", "text": "work-with-ndp"},
                {"id": "content", "type": "markdown", "text": "# Saved discovery instructions"},
            ],
        },
        "metadata": {
            "result": {
                "path": "/removed/SKILL.md",
                "skill_id": "work-with-ndp",
                "file": "references/discovery-and-analysis.md",
            }
        },
    }
    before = copy.deepcopy(part)
    view = legacy_skill_presentation(
        part, {"skill_id": "work-with-ndp", "file": "references/discovery-and-analysis.md"}
    )
    assert view and view["action"] == "Read skill reference"
    assert view["blocks"][0]["text"] == "references/discovery-and-analysis.md"
    assert not any(block["type"] == "link" for block in view["blocks"])
    assert part == before


def test_document_calculation_exposes_actual_cell_failures() -> None:
    result = {
        "status": "failed",
        "formula_count": 2,
        "issue_count": 1,
        "issues": [{"part": "xl/worksheets/sheet1.xml", "cell": "B4", "error": "#DIV/0!"}],
        "semantic_review": "pending",
    }
    view = native_presentation(
        "document_inspection", {"action": "recalculate", "path": "analysis.xlsx"}, result, None
    )
    assert view["status"] == "failed"
    assert "B4" in str(view) and "#DIV/0!" in str(view) and "Formulas: 2" in str(view)
