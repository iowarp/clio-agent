"""Screenshot-driven regressions for concise native result semantics."""

import json

from pytest import MonkeyPatch

from clio_agent.gact.agents import native_presenters
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


def test_resource_read_uses_the_linked_filename_for_language_aware_code() -> None:
    monkeypatch = MonkeyPatch()
    monkeypatch.setattr(
        native_presenters,
        "_resource_link",
        lambda _resource_id: {
            "id": "resource",
            "type": "link",
            "target": "resource",
            "uri": "resource-1",
            "label": "qualification.html",
        },
    )
    try:
        view = native_presentation(
            "resource",
            {},
            {
                "resource_id": "resource-1",
                "representation": "original",
                "text": "<main>Qualification</main>",
            },
            None,
        )
    finally:
        monkeypatch.undo()

    assert view["blocks"][-1] == {
        "id": "content",
        "type": "code",
        "language": "html",
        "text": "<main>Qualification</main>",
    }


def test_idle_goal_and_empty_schedule_list_have_no_empty_panels() -> None:
    assert (
        native_presentation("goal", {}, {"active": False, "iters_elapsed": 0}, None)["blocks"] == []
    )
    assert native_presentation("schedules", {}, {"schedules": []}, None)["blocks"] == []


def test_loaded_skill_header_owns_identity_without_repeating_it_as_a_large_heading() -> None:
    view = native_presentation(
        "text",
        {"skill_id": "inspect-qualification-brief"},
        "# Skill: inspect-qualification-brief\n\nRead the supplied values, then verify the total.",
        None,
    )

    assert view["subject"] == "skill-name"
    assert view["blocks"] == [
        {
            "id": "skill-name",
            "type": "text",
            "text": "inspect-qualification-brief",
        },
        {
            "id": "content",
            "type": "markdown",
            "text": "Read the supplied values, then verify the total.",
        },
    ]


def test_wait_shows_the_meaningful_structured_context_the_parent_received() -> None:
    raw = json.dumps(
        {
            "results": [
                {
                    "task_id": "task_1",
                    "child_session_id": "child_1",
                    "status": "completed",
                    "output": "complete",
                    "workflow_state": {
                        "verification": {
                            "status": "complete",
                            "alpha": 4,
                            "beta": 7,
                            "sum": 11,
                        }
                    },
                }
            ]
        }
    )
    structured = {
        "results": [
            {
                "name": "Verification #1",
                "status": "completed",
                "waited_ms": 8200,
                "answer_excerpt": "complete",
            }
        ]
    }

    view = native_presentation("wait", {}, raw, structured)

    assert view["blocks"][-1]["detail"] == (
        "Verification status: complete\n"
        "Verification alpha: 4\n"
        "Verification beta: 7\n"
        "Verification sum: 11"
    )


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
    assert view["action"] == "Create Artifact"
    assert view["summary"] == "Created version 2"
    assert view["subject"] == "artifact-0"
    assert view["blocks"][0]["label"] == "report.md"


def test_existing_artifact_names_the_deduplicated_registration_state() -> None:
    row = {
        "message": "deduplicated against existing report.md v1",
        "artifacts": [
            {
                "accepted": True,
                "created": False,
                "reason": "already_registered",
                "artifact_id": "artifact-1",
                "name": "report.md",
                "version": 1,
            }
        ],
    }

    view = native_presentation("artifact", {}, row, None)

    assert view["action"] == "Create Artifact"
    assert view["summary"] == "Already registered as version 1"
    assert view["subject"] == "artifact-0"


def test_memory_results_are_linked_bounded_and_hide_opaque_context_ids(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_presenters,
        "_session_link",
        lambda session_id: {
            "id": "session",
            "type": "link",
            "target": "session",
            "uri": session_id,
            "label": "Qualification session",
        },
    )

    summary = native_presentation(
        "memory",
        {},
        {
            "tool": "memory_read_session_summary",
            "summary": {
                "session_id": "session-1",
                "title": "Qualification session",
                "message_count": 8,
                "status": "idle",
                "recent_excerpts": [{"role": "assistant", "excerpt": "Bounded result"}],
            },
        },
        None,
    )
    assert summary["summary"] == "8 messages\nStatus: Idle"
    assert summary["blocks"][0]["target"] == "session"
    assert summary["blocks"][1] == {
        "id": "excerpt-0",
        "type": "text",
        "label": "Assistant",
        "text": "Bounded result",
    }

    context = native_presentation(
        "memory",
        {},
        {
            "tool": "memory_read_context_frame",
            "frame": {
                "session_id": "session-1",
                "items": [
                    {
                        "kind": "message",
                        "role": "user",
                        "source_id": "opaque-message-id",
                        "included": True,
                        "reason": "visible_transcript",
                    }
                ],
            },
        },
        None,
    )
    assert context["summary"] == "1 retained item"
    assert context["blocks"][-1] == {
        "id": "item-0",
        "type": "text",
        "label": "User message",
        "text": "Included from visible transcript",
    }
    assert "opaque-message-id" not in json.dumps(context)
