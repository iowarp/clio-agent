"""Semantic results are observer views, never replacements for tool observations."""

from __future__ import annotations

import json
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


def test_wait_uses_child_identity_and_declared_display_name() -> None:
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
    assert view["blocks"][0]["uri"] == "child"
    assert view["blocks"][0]["label"] == "Researcher #1 · completed"
    assert view["blocks"][1]["text"] == "Evidence packet"


def test_resource_outline_exposes_the_returned_collections() -> None:
    result = {"resource_id": "paper", "collections": {"pages": 7, "tables": 3, "texts": 28}}
    view = native_presentation("resource", {}, result, None)
    assert view["blocks"][0]["text"] == "Pages: 7\nTables: 3\nTexts: 28"


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
    assert view["summary"] == "https://example.org/paper.pdf"
    blocks = {block["id"]: block for block in view["blocks"]}
    assert "conversion" in blocks["identity"]["text"]
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
