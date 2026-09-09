"""Screenshot-driven regressions for concise native result semantics."""

from datetime import UTC, datetime

from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.agents.spawn_group import wait_completion_offset_ms


def test_resource_search_identifies_the_query_without_repeating_transport_metadata() -> None:
    view = native_presentation(
        "resource",
        {"query": "evidence"},
        {"matches": [{"line": 3, "text": "Actual evidence"}]},
        None,
    )
    assert view["summary"] == "1 match for “evidence”"
    assert view["blocks"][0]["text"] == "3: Actual evidence"


def test_idle_goal_and_empty_schedule_list_have_no_empty_panels() -> None:
    assert (
        native_presentation("goal", {}, {"active": False, "iters_elapsed": 0}, None)["blocks"] == []
    )
    assert native_presentation("schedules", {}, {"schedules": []}, None)["blocks"] == []


def test_resource_inspection_combines_identity_and_conversion_without_redundant_state() -> None:
    result = {
        "resource": {
            "name": "report.pdf",
            "detected_mime": "application/pdf",
            "declared_size": 1234,
            "received_size": 1234,
            "revision": 1,
            "state": "ready",
        },
        "processing": {
            "state": "complete",
            "stage": "complete",
            "message": "Conversion complete",
            "progress": 100,
        },
    }
    view = native_presentation("resource", {}, result, None)
    assert view["blocks"] == [
        {
            "id": "identity",
            "type": "text",
            "text": "application/pdf\n1,234 bytes\nRevision 1\nReady\nConversion complete",
        }
    ]
    assert result["processing"]["progress"] == 100


def test_single_artifact_summary_does_not_repeat_the_clickable_subject() -> None:
    row = {
        "message": "created report.md",
        "artifacts": [
            {
                "accepted": True,
                "created": True,
                "artifact_id": "artifact-1",
                "name": "report.md",
                "version": 2,
            }
        ],
    }
    view = native_presentation("artifact", {}, row, None)
    assert view["action"] == "Created"
    assert view["summary"] == "Version 2"
    assert view["subject"] == "artifact-0"
    assert view["blocks"][0]["label"] == "report.md"


def test_rejected_artifact_is_a_failed_workspace_boundary_error() -> None:
    view = native_presentation(
        "artifact",
        {},
        {
            "artifacts": [
                {
                    "accepted": False,
                    "reason": "escapes_root",
                    "detail": r"D:\outside.md resolves outside the workspace root",
                }
            ]
        },
        None,
    )

    assert view["status"] == "failed"
    assert view["summary"] == ""
    assert view["blocks"] == [
        {
            "id": "rejection-0",
            "type": "text",
            "label": "Artifact rejected",
            "severity": "error",
            "text": (
                "The requested path is outside the active workspace. "
                "Artifacts can only reference files inside this workspace.\n"
                r"D:\outside.md resolves outside the workspace root"
            ),
        }
    ]


def test_wait_completion_offset_is_relative_to_the_current_wait() -> None:
    started = datetime(2026, 9, 9, 2, 0, 0, tzinfo=UTC)

    assert wait_completion_offset_ms(started, "2026-09-09T02:00:07+00:00") == 7000
    assert wait_completion_offset_ms(started, "2026-09-09T01:59:55+00:00") == 0
