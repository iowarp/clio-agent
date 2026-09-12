"""Semantic results are observer views, never replacements for tool observations."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents import tool_instrumentation
from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.tools.tool_presentation import present_mcp_result


def test_native_presenter_cannot_mutate_model_arguments_or_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def presenter(args: Any, result: Any, structured: Any) -> dict[str, Any]:
        args["nested"].append("mutation")
        result["answer"].clear()
        structured["other"].clear()
        return {"summary": "view", "blocks": []}

    monkeypatch.setitem(tool_instrumentation._RESULT_PRESENTERS, "probe", presenter)
    args, result, structured = {"nested": [1]}, {"answer": [2]}, {"other": [3]}
    assert (
        tool_instrumentation.present_native_result("probe", args, result, structured)["summary"]
        == "view"
    )
    assert args == {"nested": [1]}
    assert result == {"answer": [2]}
    assert structured == {"other": [3]}


def test_todos_show_indexed_content_and_status_not_only_acknowledgement() -> None:
    todos = [
        {"content": "Collect evidence", "status": "in_progress"},
        {"content": "Review evidence", "status": "pending"},
    ]
    view = native_presentation(
        "todos", {}, "Recorded 2 todos", {"message": "Recorded 2 todos", "todos": todos}
    )
    assert view["blocks"] == [
        {
            "id": "todo-0",
            "type": "check",
            "state": "in_progress",
            "text": "Collect evidence",
        },
        {"id": "todo-1", "type": "check", "state": "pending", "text": "Review evidence"},
    ]
    assert view["summary"] == ""


def test_todos_show_the_actual_status_transition() -> None:
    view = native_presentation(
        "todos",
        {},
        "Recorded 1 todo",
        {
            "changes": [
                {
                    "content": "Inspect evidence",
                    "status": "completed",
                    "previous_status": "pending",
                    "change": "status_changed",
                }
            ]
        },
    )
    assert view["summary"] == "1 task changed"
    assert view["blocks"] == [
        {
            "id": "todo-0",
            "type": "check",
            "state": "completed",
            "previous_state": "pending",
            "change": "status_changed",
            "text": "Inspect evidence",
        }
    ]


def test_presenter_failure_is_diagnostic_without_rewriting_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.presentation_observer import completed_presentation

    def broken(args: Any, result: Any, structured: Any) -> dict[str, Any]:
        result["value"].clear()
        raise ValueError("broken view")

    monkeypatch.setitem(tool_instrumentation._RESULT_PRESENTERS, "broken_native", broken)
    raw = {"value": ["untouched"]}
    returned, view = completed_presentation("broken_native", {}, raw, None, "")
    assert returned is raw
    assert raw == {"value": ["untouched"]}
    assert view == {"summary": "", "blocks": [], "diagnostic": "presentation_failed"}


def test_failed_execution_presents_the_authoritative_reason_without_fabricating_a_result() -> None:
    from clio_agent.gact.presentation_observer import completed_presentation

    raw, view = completed_presentation(
        "missing_native", {}, None, None, "", error="Requested file does not exist"
    )
    assert raw is None
    assert view["blocks"] == [
        {
            "id": "execution-error",
            "type": "text",
            "label": "Request failed",
            "severity": "error",
            "text": "Requested file does not exist",
        }
    ]

    _raw, denied = completed_presentation(
        "memory_read_session_summary",
        {},
        None,
        None,
        "",
        error=(
            "403: {'error': {'error': 'memory_policy_denied', "
            "'message': 'raw policy payload', "
            "'details': {'policy_decision': 'deny_other_workspace'}}}"
        ),
    )
    assert denied["blocks"][-1]["text"] == (
        "This session belongs to another workspace and is not accessible "
        "from the current workspace."
    )


def test_observe_uses_child_identity_and_declared_display_name() -> None:
    raw = json.dumps(
        {"results": [{"task_id": "task", "child_session_id": "child", "status": "completed"}]}
    )
    structured = {
        "summary": "One task completed",
        "results": [
            {"name": "Researcher #1", "status": "completed", "answer_excerpt": "Evidence packet"}
        ],
    }
    view = native_presentation("tasks", {}, raw, structured)
    assert view["summary"] == ""
    assert view["blocks"][0] == {
        "id": "task-subject",
        "type": "link",
        "target": "session",
        "uri": "child",
        "label": "Researcher #1",
    }
    assert view["blocks"][1] == {
        "id": "observation",
        "type": "text",
        "label": "Observed",
        "text": "Cursor 1 -> 1; No pattern, returned immediately; Researcher #1: terminal (completed)",
    }


def test_patterned_observe_presents_match_cursor_status_and_curated_evidence() -> None:
    row = {
        "cursor": 7,
        "next_cursor": 10,
        "matched": True,
        "tasks": [
            {
                "task_id": "task",
                "child_session_id": "child",
                "name": "Research methodologist #1",
                "status": "running",
                "new_events": [
                    {
                        "family": "lifecycle",
                        "event_type": "expert.lifecycle.started",
                        "summary": "Context received expert research_methodologist started",
                        "excerpt": "Context received expert research_methodologist started",
                    },
                    {
                        "family": "react.step",
                        "event_type": "react.step.completed",
                        "summary": "Checking method",
                        "excerpt": 'thought: compare methods | tool: search({"query": "evidence"})',
                        "matched": True,
                    },
                ],
            }
        ],
    }

    view = native_presentation("tasks", {"pattern": "station=KOOT"}, row, None)

    assert view == {
        "subject": "task-subject",
        "summary": "",
        "blocks": [
            {
                "id": "task-subject",
                "type": "link",
                "target": "session",
                "uri": "child",
                "label": "Research methodologist #1",
            },
            {
                "id": "observation",
                "type": "text",
                "label": "Observed",
                "text": (
                    'Cursor 7 -> 10; Pattern "station=KOOT" matched; '
                    "Research methodologist #1: running"
                ),
            },
            {
                "id": "evidence-0",
                "type": "text",
                "label": "Evidence · Research methodologist #1",
                "text": 'thought: compare methods | tool: search({"query": "evidence"})',
            },
        ],
    }
    assert "Context received" not in json.dumps(view)


def test_patterned_observe_presents_terminal_release_without_claiming_a_match() -> None:
    row = {
        "cursor": 10,
        "next_cursor": 12,
        "matched": False,
        "tasks": [
            {
                "task_id": "task",
                "child_session_id": "child",
                "name": "Research methodologist #1",
                "status": "completed",
                "new_events": [
                    {
                        "family": "delegation",
                        "event_type": "delegation.completed",
                        "excerpt": "delegation.completed | workflow_state: complete",
                    }
                ],
            }
        ],
    }

    view = native_presentation("tasks", {"pattern": "never-matches"}, row, None)

    assert view["blocks"][1] == {
        "id": "observation",
        "type": "text",
        "label": "Observed",
        "text": (
            'Cursor 10 -> 12; Pattern "never-matches" did not match, '
            "hold released by terminal child; Research methodologist #1: terminal (completed)"
        ),
    }
    assert view["blocks"][2] == {
        "id": "evidence-0",
        "type": "text",
        "label": "Evidence · Research methodologist #1",
        "text": "delegation.completed | workflow_state: complete",
    }


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "unknown_task"])
def test_wait_reports_lifecycle_without_repeating_child_output(status: str) -> None:
    raw = json.dumps(
        {
            "results": [
                {
                    "task_id": "task",
                    "child_session_id": "child",
                    "status": status,
                    "output": "FULL CHILD ANSWER",
                }
            ]
        }
    )
    structured = {
        "summary": "waited 12.0s for 1 task",
        "results": [
            {
                "name": "Researcher #1",
                "status": status,
                "duration_ms": 11004,
                "answer_excerpt": "CHILD EXCERPT",
            }
        ],
    }
    view = native_presentation("wait", {}, raw, structured)
    # #1306: the model-facing "output" is already the digested (bounded) child
    # answer (digested_model_row) -- that IS the meaningful received context, so
    # it wins over the separate structured "answer_excerpt" fallback. The
    # committed wait never shows BOTH; the untaken fallback never leaks.
    assert view["summary"] == ""
    assert view["subject"] == "task-subject"
    assert view["blocks"] == [
        {"id": "task-subject", "type": "text", "text": "Researcher #1"},
        {
            "id": "task-0",
            "type": "item",
            "target": "session",
            "uri": "child",
            "label": "Researcher #1",
            "status": status,
            "result_kind": "completion",
            "duration_ms": None,
            "detail": "FULL CHILD ANSWER",
        },
    ]
    assert "CHILD EXCERPT" not in json.dumps(view)
    assert json.loads(raw)["results"][0]["output"] == "FULL CHILD ANSWER"


def test_declared_running_presenter_survives_wrapping_and_cannot_change_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.agents.tool_instrumentation import (
        instrument_tools,
        native_tool,
        present_native_start,
        rebuilt_tool,
    )

    def execute(task_ids: list[str]) -> str:
        return "unchanged"

    def start(args: Any) -> dict[str, Any]:
        args["task_ids"].clear()
        return {"summary": "Waiting for Researcher #1", "blocks": []}

    tool = native_tool(
        execute,
        name="start_probe",
        desc="probe",
        args={},
        presentation="wait",
        presentation_start=start,
    )
    wrapped = rebuilt_tool(
        tool, lambda task_ids: "unchanged", name="start_probe", desc="probe", args={}
    )
    instrument_tools([wrapped])
    arguments = {"task_ids": ["task"]}
    assert present_native_start("start_probe", arguments)["summary"] == "Waiting for Researcher #1"
    assert arguments == {"task_ids": ["task"]}

    def broken(args: Any) -> dict[str, Any]:
        raise ValueError("observer failed")

    monkeypatch.setitem(tool_instrumentation._START_PRESENTERS, "start_probe", broken)
    assert present_native_start("start_probe", arguments)["diagnostic"] == "presentation_failed"


def test_wait_start_identifies_requested_tasks_without_collecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import context
    from clio_agent.gact.agents.native_presenters import waiting_presentation

    monkeypatch.setattr(context, "active_app", lambda: None)
    assert waiting_presentation({"task_ids": ["x", "y"]}) == {
        "summary": "Waiting for x, y",
        "blocks": [],
    }
    assert waiting_presentation({"task_ids": []})["summary"] == "No tasks requested"


def test_markdown_file_presents_readable_frontmatter_and_body() -> None:
    content = "---\nname: imagegen\ndescription: Create images\n---\n# Image generation\n\nRead the **procedure**."
    result = {
        "structuredContent": {
            "path": r"D:\skills\SKILL.md",
            "size_bytes": len(content),
            "content": content,
        }
    }
    before = json.dumps(result)
    view = present_mcp_result("fs_read_file", {}, result)
    blocks = {block["id"]: block for block in view["blocks"]}
    assert blocks["file"]["type"] == "markdown"
    assert blocks["file"]["text"] == "# Image generation\n\nRead the **procedure**."
    assert blocks["metadata"]["type"] == "text"
    assert blocks["metadata"]["text"] == "Name: imagegen\nDescription: Create images"
    assert json.dumps(result) == before


@pytest.mark.parametrize(
    ("path", "content", "kind"),
    [
        ("readme.MD", "# Heading", "markdown"),
        ("note.txt", "plain text", "text"),
        ("main.py", "print('hello')", "code"),
        ("broken.md", "---\nname: [broken\n---\nBody", "markdown"),
    ],
)
def test_file_format_is_declared_by_read_presenter(path: str, content: str, kind: str) -> None:
    view = present_mcp_result(
        "fs_read_file", {}, {"structuredContent": {"path": path, "content": content}}
    )
    block = next(block for block in view["blocks"] if block["id"] == "file")
    assert block["type"] == kind
    assert block["text"] == content


def test_resource_outline_exposes_the_returned_collections() -> None:
    result = {"resource_id": "paper", "collections": {"pages": 7, "tables": 3, "texts": 28}}
    view = native_presentation("resource", {}, result, None)
    assert view["blocks"][0]["text"] == "Pages: 7\nTables: 3\nTexts: 28"


def test_resource_markdown_derivative_declares_rendered_document_content() -> None:
    result = {
        "resource_id": "paper",
        "representation": "markdown",
        "content": "## Evidence\n\nActual finding",
    }
    view = native_presentation("resource", {}, result, None)
    assert view["blocks"] == [{"id": "content", "type": "markdown", "text": result["content"]}]


def test_resource_search_empty_result_is_explicit() -> None:
    view = native_presentation("resource", {}, {"resource_id": "paper", "matches": []}, None)
    assert view["blocks"][0]["text"] == "No matching passages"


def test_resource_result_links_the_authoritative_name_within_current_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import context

    lookups: list[tuple[str, str]] = []

    def lookup(workspace: str, resource_id: str) -> Any:
        lookups.append((workspace, resource_id))
        return SimpleNamespace(name="Evidence.pdf") if resource_id == "owned" else None

    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions={"s": SimpleNamespace(workspace_id="w")},
            resource_store=SimpleNamespace(get=lookup),
        )
    )
    monkeypatch.setattr(context, "active_app", lambda: app)
    monkeypatch.setattr(context, "active_session_id", lambda: "s")
    view = native_presentation("resource", {}, {"resource_id": "owned", "content": "Body"}, None)
    assert view["summary"] == ""
    assert view["blocks"][0] == {
        "id": "resource",
        "type": "link",
        "target": "resource",
        "uri": "owned",
        "label": "Evidence.pdf",
    }
    foreign = native_presentation(
        "resource", {}, {"resource_id": "foreign", "content": "Body"}, None
    )
    assert all(block["type"] != "link" for block in foreign["blocks"])
    assert lookups == [("w", "owned"), ("w", "foreign")]


def test_resource_truncation_is_not_mistaken_for_a_complete_document() -> None:
    result = {"resource_id": "paper", "content": "First passage", "truncated": True}
    view = native_presentation("resource", {}, result, None)
    assert view["blocks"][-1]["text"] == "Result truncated by the resource tool"
    assert result == {"resource_id": "paper", "content": "First passage", "truncated": True}


def test_observation_keeps_incremental_events_in_technical_result() -> None:
    row = {
        "tasks": [
            {
                "task_id": "task",
                "status": "running",
                "new_events": [
                    {"summary": "Reading evidence", "excerpt": "Official guide fetched"}
                ],
            }
        ]
    }
    before = json.dumps(row)
    view = native_presentation("tasks", {}, row, None)
    assert view["blocks"][-1]["text"] == "Official guide fetched"
    assert json.dumps(row) == before


def test_resource_list_uses_the_custody_name_and_identifier() -> None:
    view = native_presentation(
        "resource", {}, {"resources": [{"id": "r1", "name": "Guide.pdf"}]}, None
    )
    assert view["blocks"][0]["label"] == "Guide.pdf"
    assert view["blocks"][0]["uri"] == "r1"


@pytest.mark.parametrize("event_type", ["react.step.completed", "expert.extract.completed"])
def test_observation_keeps_serialized_action_and_full_extraction_technical(event_type: str) -> None:
    row = {
        "tasks": [
            {
                "task_id": "task",
                "status": "running",
                "new_events": [
                    {
                        "event_type": event_type,
                        "summary": "Researcher completed its analysis",
                        "excerpt": 'tool: submit({"answer": "FULL REPORT"})',
                    }
                ],
            }
        ]
    }
    before = json.dumps(row)
    view = native_presentation("tasks", {}, row, None)
    assert view["blocks"][-1]["text"] == 'tool: submit({"answer": "FULL REPORT"})'
    assert json.dumps(row) == before


def test_observation_drops_child_lifecycle_chatter() -> None:
    row = {
        "tasks": [
            {
                "task_id": "task",
                "new_events": [
                    {
                        "event_type": "expert.lifecycle.started",
                        "summary": "Researcher started",
                        "excerpt": "Researcher started",
                    }
                ],
            }
        ]
    }
    view = native_presentation("tasks", {}, row, None)
    assert all(block["id"] != "evidence-0" for block in view["blocks"])


def test_resource_inspection_uses_the_custody_size_fields() -> None:
    view = native_presentation(
        "resource",
        {},
        {
            "resource": {
                "id": "r1",
                "name": "Guide.pdf",
                "detected_mime": "application/pdf",
                "declared_size": 1200,
                "received_size": 1200,
                "revision": 1,
                "state": "ready",
            }
        },
        None,
    )
    assert view["blocks"][0]["text"] == "application/pdf\n1,200 bytes\nRevision 1\nReady"
    assert view["blocks"][0]["text"].count("1,200") == 1


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    [
        (
            "path_missing",
            "File does not exist",
            "report.md does not exist, so it cannot be registered as an artifact.",
        ),
        (
            "escapes_root",
            "C:\\outside\\report.md resolves outside the workspace root",
            "report.md is outside the active workspace, so it cannot be registered as an artifact.",
        ),
        (
            "would_overwrite",
            "C:\\workspace\\report.md already exists",
            "report.md already exists but is not a registered artifact. "
            "Register the existing file by path or choose another name.",
        ),
    ],
)
def test_rejected_artifact_explains_the_actual_rejection(
    reason: str,
    detail: str,
    expected: str,
) -> None:
    view = native_presentation(
        "artifact",
        {},
        {
            "artifacts": [
                {
                    "accepted": False,
                    "name": "C:\\workspace\\report.md",
                    "reason": reason,
                    "detail": detail,
                }
            ]
        },
        None,
    )
    assert view["blocks"][0]["text"] == expected


def test_web_conversion_exposes_saved_outputs_and_progress() -> None:
    result = {
        "structuredContent": {
            "url": "https://example.org/paper.pdf",
            "content_type": "application/pdf",
            "conversion_id": "conversion",
            "status": 200,
            "local_path": "D:/workspace/paper.md",
            "metadata_path": "D:/workspace/paper.json",
            "events": [{"stage": "docling", "message": "Converting page 3"}],
        }
    }
    before = json.dumps(result)
    view = present_mcp_result("web_fetch", {}, result)
    assert view["summary"] == "HTTP 200"
    assert view["subject"] == "subject"
    assert view["blocks"][0]["uri"] == "https://example.org/paper.pdf"
    blocks = {block["id"]: block for block in view["blocks"]}
    assert blocks["identity"]["text"] == "application/pdf"
    assert "Conversion:" not in json.dumps(blocks)
    assert blocks["events"]["text"] == "docling · Converting page 3"
    assert blocks["local_path"]["uri"] == "D:/workspace/paper.md"
    assert blocks["metadata_path"]["uri"] == "D:/workspace/paper.json"
    assert json.dumps(result) == before


def test_generic_mcp_resource_content_is_readable_and_structured_json_is_technical() -> None:
    view = present_mcp_result(
        "third_party",
        {},
        {
            "structuredContent": {"private_shape": "technical"},
            "content": [
                {
                    "type": "resource",
                    "resource": {"uri": "resource://report", "text": "Actual report"},
                },
                {"type": "text", "text": '{"private_shape":"technical"}'},
            ],
        },
    )
    assert len(view["blocks"]) == 1
    assert view["blocks"][0]["text"] == "Actual report"


def test_web_document_identity_and_degraded_empty_search_are_visible() -> None:
    """Display the actual document title and failed search coverage, not only an acknowledgement."""
    payload = {
        "structuredContent": {
            "ok": True,
            "title": None,
            "url": "https://example.org/guide.pdf",
            "document": {
                "metadata": {"title": "Official guide"},
                "structure_summary": {"pages": 19},
            },
            "conversion_id": "conversion-1",
            "status": 200,
        }
    }
    before = json.dumps(payload)
    view = present_mcp_result("web_fetch", {}, payload)
    assert view["summary"] == "HTTP 200"
    assert view["subject"] == "subject"
    assert view["blocks"][0]["label"] == "Official guide"
    assert view["blocks"][0]["uri"] == "https://example.org/guide.pdf"
    assert "Pages: 19" in view["blocks"][1]["text"]
    assert json.dumps(payload) == before
    search = present_mcp_result(
        "web_search",
        {"query": "research"},
        {
            "structuredContent": {
                "provider": "searxng",
                "results": [],
                "engines_answered": [],
                "unresponsive_engines": [{"engine": "duckduckgo", "reason": "CAPTCHA"}],
            }
        },
    )
    blocks = {block["id"]: block for block in search["blocks"]}
    assert search["summary"] == "0 results"
    assert "result-count" not in blocks
    assert blocks["engine-status"]["text"] == "duckduckgo: CAPTCHA"


def test_file_result_has_one_portable_filename_link() -> None:
    path = r"D:\workspace\evidence.txt"
    view = present_mcp_result(
        "fs_read_file",
        {},
        {
            "structuredContent": {
                "path": path,
                "size_bytes": 12,
                "content": "Evidence body",
            }
        },
    )
    assert view["summary"] == "12 bytes"
    assert view["blocks"][0] == {
        "id": "file-link",
        "type": "link",
        "target": "file",
        "uri": path,
        "label": "evidence.txt",
        "text": "",
        "language": "",
        "command": "",
        "timed_out": False,
        "media_type": "",
        "status": "",
        "detail": "",
        "items": [],
        "action_label": "",
    }
    assert view["blocks"][1]["label"] == ""


def test_failed_handoff_live_and_snapshot_explain_the_same_failure() -> None:
    from clio_agent.gact.events import Event
    from clio_agent.gact.protocol.v3.event import event_to_v3
    from clio_agent.gact.protocol.v3.message import subagent_from_part

    part = {
        "id": "failed-call",
        "type": "expert_handoff",
        "child_agent": "researcher",
        "status": "failed",
        "stage": "delegate.completed",
        "text": "main -> researcher",
        "metadata": {"error": "blueprint_not_found"},
    }
    snapshot = subagent_from_part(part, "session")
    event = Event(
        type="message.part.added",
        session_id="session",
        payload={"message_id": "message", "part": part},
    )
    live = event_to_v3(event, workspace_id="workspace")
    assert live["payload"]["subagent"] == snapshot
    assert snapshot["summary"] == "blueprint_not_found"
    assert snapshot["state"] == "failed"
    assert part["text"] == "main -> researcher"


@pytest.mark.parametrize(
    ("declaration", "row", "expected"),
    [
        (
            "fields:schedule_id,cron,run_at,recurring,next_fire_at,timezone",
            {
                "schedule_id": "schedule",
                "run_at": "2026-09-09T12:00Z",
                "recurring": False,
                "timezone": "UTC",
            },
            [
                "Schedule id: schedule",
                "Run at: 2026-09-09T12:00Z",
                "Recurring: False",
                "Timezone: UTC",
            ],
        ),
        (
            "fields:loop_id,next_fire_at,stopped",
            {"loop_id": "loop", "next_fire_at": "later", "stopped": False},
            ["Loop id: loop", "Next fire at: later", "Stopped: False"],
        ),
        (
            "goal",
            {
                "active": True,
                "condition": "Finish the report",
                "iters_elapsed": 2,
                "budget_spent": {"tokens": 32},
            },
            ["", "Finish the report"],
        ),
        (
            "resource",
            {
                "resource_id": "document",
                "node": {"title": "Findings", "text": "The actual evidence"},
            },
            ["Findings\nThe actual evidence"],
        ),
        (
            "resource",
            {"resource_id": "document", "content": "Actual document body"},
            ["Actual document body"],
        ),
        (
            "resource",
            {"resource_id": "document", "matches": [{"line": 8, "text": "Evidence match"}]},
            ["8: Evidence match"],
        ),
        (
            "schedules",
            {
                "schedules": [
                    {
                        "id": "schedule",
                        "cron": "0 9 * * *",
                        "timezone": "UTC",
                        "prompt": "Review work",
                        "next_fire_at": "tomorrow",
                    }
                ]
            },
            [""],
        ),
        ("text", {}, ["# Real skill\nLoaded procedure"]),
    ],
)
def test_each_native_family_exposes_actual_result_content(
    declaration: str, row: dict[str, Any], expected: list[str]
) -> None:
    raw: Any = "# Real skill\nLoaded procedure" if declaration == "text" else row
    before = json.dumps(raw)
    view = native_presentation(declaration, {}, raw, row)
    assert [block.get("text", "") for block in view["blocks"]] == expected
    assert json.dumps(raw) == before


def test_message_exposes_sent_content_and_keeps_transport_in_raw_details() -> None:
    raw = {"message": "queued", "transport": "inbox", "action": "queue"}
    view = native_presentation(
        "message",
        {"message": "Review this evidence", "task_id": "unknown"},
        {},
        raw,
    )
    assert view["blocks"][0]["text"] == "Review this evidence"
    assert view["summary"] == "Queued"
    assert len(view["blocks"]) == 1
    assert raw == {"message": "queued", "transport": "inbox", "action": "queue"}


def test_one_shot_schedule_has_trigger_and_no_empty_separator() -> None:
    row = {
        "schedules": [
            {"id": "s1", "cron": "", "timezone": "UTC", "prompt": "Review", "next_fire_at": "later"}
        ]
    }
    view = native_presentation("schedules", {}, row, None)
    assert view["summary"] == "1 schedule, 0 recurring, 1 one-shot"
    assert view["blocks"] == [
        {
            "id": "schedule-0",
            "type": "item",
            "target": "work",
            "uri": "s1",
            "label": "Review",
            "items": ["One-shot", "Runs later", "Time zone UTC"],
        }
    ]


def test_created_schedule_shows_prompt_and_one_timestamp_without_repeating_acknowledgment() -> None:
    row = {
        "schedule_id": "s1",
        "recurring": False,
        "next_fire_at": "later",
        "run_at": "later",
        "timezone": "UTC",
        "message": "armed s1 later",
    }
    view = native_presentation("schedule_created", {"prompt": "Review", "delay_s": 600}, row, None)
    assert view == {
        "summary": "Scheduled to run in 10 minutes",
        "blocks": [
            {
                "id": "schedule",
                "type": "item",
                "target": "work",
                "uri": "s1",
                "label": "Review",
                "items": ["One-shot", "Runs later", "Time zone UTC"],
            }
        ],
    }


@pytest.mark.parametrize("deleted", [True, False])
def test_schedule_removal_preserves_actual_outcome_without_duplicate_fields(deleted: bool) -> None:
    row = {
        "schedule_id": "s1",
        "deleted": deleted,
        "message": "cancelled s1" if deleted else "no schedule s1 to cancel",
    }
    view = native_presentation("schedule_deleted", {}, deleted, row)
    assert view == {
        "subject": "schedule-subject",
        **({"status": "error"} if not deleted else {}),
        "summary": "Schedule deleted." if deleted else "No matching schedule was found.",
        "blocks": [
            {
                "id": "schedule-subject",
                "type": "link",
                "target": "work",
                "uri": "s1",
                "label": "s1",
            }
        ],
    }


def test_failed_task_collection_does_not_claim_it_collected_a_result() -> None:
    view = native_presentation(
        "task_output", {}, {"task_id": "missing", "error": "unknown_task"}, None
    )
    assert view["summary"] == ""
    assert view["blocks"][0]["label"] == "Full output unavailable"
    assert view["blocks"][0]["text"] == "unknown task"


def test_model_catalog_exposes_actual_changes_and_failures_without_empty_fields() -> None:
    row = {
        "results": [
            {
                "provider": "codex",
                "source": "codex_sdk",
                "default_model": "model-a",
                "added": [],
                "removed": [],
                "unchanged": ["model-a"],
            },
            {
                "provider": "local",
                "source": "live_handshake",
                "default_model": "",
                "added": [],
                "removed": [],
                "unchanged": [],
                "failed_reason": "Connection refused",
            },
        ]
    }
    before = json.dumps(row)
    view = native_presentation("model_catalog", {}, row, None)
    assert view["summary"] == "2 provider results"
    assert view["blocks"] == [
        {
            "id": "provider-1",
            "type": "item",
            "target": "url",
            "uri": "/settings/providers?provider=codex",
            "label": "codex",
            "status": "succeeded",
            "detail": "Source: codex_sdk\nDefault model: model-a",
            "items": ["model-a"],
            "action_label": "Change",
        },
        {
            "id": "provider-2",
            "type": "item",
            "target": "url",
            "uri": "/settings/providers?provider=local",
            "label": "local",
            "status": "failed",
            "detail": "Source: live_handshake\nConnection refused",
            "items": [],
            "action_label": "Change",
        },
    ]
    assert json.dumps(row) == before


@pytest.mark.parametrize(
    ("row", "summary"),
    [
        (
            {"loop_id": "l1", "stopped": False, "next_fire_at": "later"},
            "Next iteration scheduled",
        ),
        ({"loop_id": "l1", "stopped": True, "next_fire_at": ""}, "Loop stopped"),
        ({"loop_id": "", "stopped": True, "next_fire_at": ""}, "No active loop"),
    ],
)
def test_loop_presentation_distinguishes_scheduled_stopped_and_absent(
    row: dict[str, Any], summary: str
) -> None:
    view = native_presentation(
        "loop", {"prompt": "Qualification marker", "delay_seconds": 600}, row, None
    )
    assert view["summary"] == (
        "Next iteration runs in 10 minutes" if not row["stopped"] else summary
    )
    assert view["blocks"] == (
        [{"id": "next", "type": "text", "text": "Qualification marker\nRuns later"}]
        if not row["stopped"]
        else []
    )


def test_standard_mcp_media_is_declared_and_binary_is_paged_not_dumped() -> None:
    from clio_agent.gact.tool_result_presentation import project_presentation

    data = "AAAA" * 1000
    original = {"content": [{"type": "image", "mimeType": "image/png", "data": data}]}
    view = present_mcp_result("third_party", {}, original)
    assert view["blocks"][0]["type"] == "media"
    assert view["blocks"][0]["media_type"] == "image/png"
    projected = project_presentation(view, "s", "call")
    assert len(projected["blocks"][0]["text"]) == 2048
    assert projected["blocks"][0]["content_ref"]["total_chars"] == 4000
    assert original["content"][0]["data"] == data


def test_elided_mcp_media_does_not_claim_a_preview_exists() -> None:
    view = present_mcp_result(
        "third_party",
        {},
        {"content": [{"type": "audio", "mimeType": "audio/mpeg", "elided": "too_large"}]},
    )
    assert view["blocks"][0]["type"] == "text"
    assert "unavailable" in view["blocks"][0]["text"]
