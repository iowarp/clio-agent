"""Semantic results are observer views, never replacements for tool observations."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.task_types import TaskSnapshot
from clio_agent.gact.agents import tool_instrumentation
from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.tools.tool_presentation import present_mcp_result


def _common_task_row(
    *, output: list[dict[str, Any]] | None = None, **changes: Any
) -> dict[str, Any]:
    """Use the current validated snapshot vocabulary, with public observation chunks."""
    values: dict[str, Any] = {
        "handle": "task",
        "kind": "Subagent",
        "owner": {
            "human_id": "human",
            "session_id": "child",
            "assignment_id": "task",
            "parent_handle": "parent",
        },
        "label": "Researcher #1",
        "description": "Inspect the evidence",
        "description_missing": False,
        "invocation_id": "invocation",
        "tool": "conversation",
        "created_at": "2026-10-17T12:00:00Z",
        "updated_at": "2026-10-17T12:00:00Z",
        "status": "active",
        "raw_status": "active",
        "activity": "busy",
    }
    values.update(changes)
    row = TaskSnapshot.model_validate(values).model_dump(mode="json")
    if output is not None:
        row["output"] = output
    return row


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
    view = tool_instrumentation.present_native_result("probe", args, result, structured)
    assert view is not None and view["summary"] == "view"
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
    assert view["action"] == "Update Todos"
    assert view["summary"] == "1 todo changed"
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
        "session_read_summary",
        {},
        None,
        None,
        "",
        error=(
            "403: {'error': {'error': 'session_recall_policy_denied', "
            "'message': 'raw policy payload', "
            "'details': {'policy_decision': 'deny_other_workspace'}}}"
        ),
    )
    assert denied["blocks"][-1]["text"] == (
        "This session belongs to another workspace and is not accessible "
        "from the current workspace."
    )


def test_observe_uses_child_identity_and_declared_display_name() -> None:
    payload = {"tasks": [_common_task_row(status="completed", activity="")], "cursor": "opaque"}
    raw = json.dumps(payload)
    structured = {"tasks": [_common_task_row(label="Substituted label")]}
    view = native_presentation("tasks", {}, raw, structured)
    links = [block for block in view["blocks"] if block["type"] == "link"]
    assert len(links) == 1 and links[0]["uri"] == "child"
    assert links[0]["label"] == "Researcher #1"
    assert "completed" in json.dumps(view)
    assert "Substituted label" not in json.dumps(view)
    assert json.loads(raw) == payload


def test_patterned_observe_presents_match_cursor_status_and_curated_evidence() -> None:
    chunk = {
        "chunk_id": "matching-chunk",
        "output": "station=KOOT",
        "pattern_match": {"start": 0, "end": 12, "excerpt_truncated": False},
        "fragment": True,
    }
    row = {"cursor": "opaque-continuation", "tasks": [_common_task_row(output=[chunk])]}
    before = json.dumps(row)
    view = native_presentation("tasks", {"tasks": "task", "pattern": "station=KOOT"}, row, None)
    assert any(block.get("uri") == "child" for block in view["blocks"])
    progress = [block for block in view["blocks"] if block.get("label") == "New output"]
    assert len(progress) == 1 and progress[0]["text"] == "station=KOOT"
    assert "matching-chunk" not in json.dumps(view)
    assert "opaque-continuation" not in json.dumps(view)
    assert json.dumps(row) == before


def test_patterned_observe_presents_terminal_release_without_claiming_a_match() -> None:
    row = {
        "cursor": "opaque-final",
        "settled": True,
        "any_settled": True,
        "tasks": [
            _common_task_row(
                status="completed",
                activity="",
                output=[
                    {"chunk_id": "final", "output": {"text": "Research complete", "tools": []}}
                ],
            )
        ],
    }
    before = json.dumps(row)
    view = native_presentation("tasks", {"pattern": "never-matches"}, row, None)
    shown = json.dumps(view)
    assert "completed" in shown and "Research complete" in shown
    assert "matched" not in shown.lower()
    assert json.dumps(row) == before


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "unknown_task"])
def test_wait_reports_lifecycle_without_repeating_child_output(status: str) -> None:
    outcome: dict[str, Any] = (
        {"handle": "task", "error": "task_unavailable"}
        if status == "unknown_task"
        else {
            "handle": "task",
            "status": status,
            "pending": False,
            "result": {"output": "FULL CHILD ANSWER"},
        }
    )
    payload = {"outcomes": [outcome], "pending": [], "timed_out": False}
    before = json.dumps(payload)
    view = native_presentation(
        "wait",
        {"tasks": ["task"]},
        payload,
        {"results": [{"answer_excerpt": "SUBSTITUTED EXCERPT"}]},
    )
    shown = json.dumps(view)
    assert "SUBSTITUTED EXCERPT" not in shown
    if status == "unknown_task":
        assert "0 results collected" in view["summary"]
        assert any(block.get("severity") == "error" for block in view["blocks"])
        assert "FULL CHILD ANSWER" not in shown
    else:
        assert "1 result collected" in view["summary"]
        assert shown.count("FULL CHILD ANSWER") == 1
        assert any(block.get("status") == status for block in view["blocks"])
        if status != "completed":
            assert any(block.get("severity") == "error" for block in view["blocks"])
    assert json.dumps(payload) == before


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
        domain="agents",
    )
    wrapped = rebuilt_tool(
        tool, lambda task_ids: "unchanged", name="start_probe", desc="probe", args={}
    )
    instrument_tools([wrapped])
    arguments = {"task_ids": ["task"]}
    view = present_native_start("start_probe", arguments)
    assert view is not None and view["summary"] == "Waiting for Researcher #1"
    assert arguments == {"task_ids": ["task"]}

    def broken(args: Any) -> dict[str, Any]:
        raise ValueError("observer failed")

    monkeypatch.setitem(tool_instrumentation._START_PRESENTERS, "start_probe", broken)
    failed_view = present_native_start("start_probe", arguments)
    assert failed_view is not None and failed_view["diagnostic"] == "presentation_failed"


def test_wait_start_without_context_is_private_and_distinguishes_status_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import context
    from clio_agent.gact.task_wait_presentation import wait_start_presentation

    monkeypatch.setattr(context, "active_app", lambda: None)
    assert wait_start_presentation({"tasks": ["x", "y"]}) == {
        "summary": "Waiting for tasks",
        "blocks": [],
    }
    assert wait_start_presentation({"tasks": ["x"], "timeout_s": 0}) == {
        "summary": "Checking task status",
        "blocks": [],
    }


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
            _common_task_row(
                output=[
                    {"chunk_id": "read", "output": {"text": "Official guide fetched", "tools": []}}
                ]
            )
        ]
    }
    before = json.dumps(row)
    view = native_presentation("tasks", {}, row, None)
    assert any(block.get("text") == "Official guide fetched" for block in view["blocks"])
    assert "chunk_id" not in json.dumps(view)
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
            _common_task_row(
                output=[
                    {
                        "chunk_id": "analysis",
                        "output": {
                            "text": "Researcher completed its analysis",
                            "tools": [
                                {
                                    "event_type": event_type,
                                    "excerpt": 'tool: submit({"answer": "FULL REPORT"})',
                                }
                            ],
                        },
                    }
                ]
            )
        ]
    }
    before = json.dumps(row)
    view = native_presentation("tasks", {}, row, None)
    shown = json.dumps(view)
    assert "Researcher completed its analysis" in shown
    assert "FULL REPORT" not in shown and "submit(" not in shown
    assert "FULL REPORT" in json.dumps(row)
    assert json.dumps(row) == before


def test_observation_drops_child_lifecycle_chatter() -> None:
    row = {
        "tasks": [
            _common_task_row(
                output=[
                    {
                        "chunk_id": "lifecycle",
                        "output": {
                            "text": "",
                            "tools": [
                                {
                                    "event_type": "expert.lifecycle.started",
                                    "summary": "Context received",
                                }
                            ],
                        },
                    }
                ]
            )
        ]
    }
    before = json.dumps(row)
    view = native_presentation("tasks", {}, row, None)
    assert "Context received" not in json.dumps(view)
    assert all(block.get("label") != "New output" for block in view["blocks"])
    assert json.dumps(row) == before


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
    assert blocks["identity"]["text"] == "Content type: application/pdf"
    assert "Conversion:" not in json.dumps(blocks)
    assert blocks["events"]["text"] == "docling: Converting page 3"
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
        "workspace_id": "",
        "path": "",
        "sha256": "",
        "pages": [],
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
    raw = {
        "accepted": True,
        "session_id": "child",
        "message_id": "mail",
        "transport": "internal-transport",
    }
    before = dict(raw)
    view = native_presentation(
        "message",
        {"message": "Review this evidence", "target": "child"},
        {},
        raw,
    )
    assert view["summary"] == "Message accepted"
    assert sum(block.get("text") == "Review this evidence" for block in view["blocks"]) == 1
    assert any(block.get("uri") == "child" for block in view["blocks"])
    assert "internal-transport" not in json.dumps(view)
    assert raw == before


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
    row = {"handle": "missing", "error": "task_unavailable"}
    before = dict(row)
    view = native_presentation("task_output", {}, row, None)
    assert view["status"] == "failed"
    assert "collected" not in view["summary"].lower()
    assert any(
        block.get("severity") == "error" and block.get("text") == "task_unavailable"
        for block in view["blocks"]
    )
    assert all(block.get("label") != "Result" for block in view["blocks"])
    assert row == before


def test_model_catalog_exposes_actual_changes_and_failures_without_empty_fields() -> None:
    row = {
        "results": [
            {
                "provider": "codex",
                "source": "codex_catalog",
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
            "detail": "Source: codex_catalog\nDefault model: model-a",
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
    assert view is not None
    assert view["blocks"][0]["type"] == "media"
    assert view["blocks"][0]["media_type"] == "image/png"
    projected = project_presentation(view, "s", "call")
    assert projected is not None
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
