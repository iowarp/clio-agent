"""Screenshot-driven regressions for concise native result semantics."""

from clio_agent.gact.agents.native_presenters import native_presentation


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
            "text": "application/pdf · 1,234 bytes · Revision 1 · Ready\nConversion complete",
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
    assert view["summary"] == "Created · v2"
    assert view["subject"] == "artifact-0"
    assert view["blocks"][0]["label"] == "report.md"
