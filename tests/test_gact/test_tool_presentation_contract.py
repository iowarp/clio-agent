"""Regression gates for observer-only semantics, inventory, paging and streams."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.parts import Part
from clio_agent.gact.routes.messages import register_messages_routes
from clio_agent.gact.tool_progress import ToolProgressRegistry
from clio_agent.gact.tool_result_presentation import (
    PAGE_CHARS,
    ToolPresentation,
    presentation_page,
    project_presentation,
)
from clio_agent.tools.tool_presentation import (
    MCP_PRESENTATION_ADAPTERS,
    PresentationAdapter,
    present_mcp_result,
)


def test_every_native_registration_declares_presentation() -> None:
    root = Path(__file__).parents[2] / "src" / "clio_agent"
    missing = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "native_tool"
            ):
                if not any(keyword.arg == "presentation" for keyword in node.keywords):
                    missing.append(f"{path}:{node.lineno}")
    assert missing == []
    assert {
        "fs_read_file",
        "fs_propose_edit",
        "fs_apply_edit_write",
        "shell_bash",
    } <= MCP_PRESENTATION_ADAPTERS.keys()
    for namespace in ("fs", "shell"):
        path = root / "tools" / "servers" / f"{namespace}_server.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"
                    for decorator in node.decorator_list
                ):
                    assert f"{namespace}_{node.name}" in MCP_PRESENTATION_ADAPTERS


def test_historical_header_projection_preserves_original_blocks() -> None:
    original = {
        "summary": "",
        "blocks": [
            {
                "id": "file-link",
                "type": "link",
                "target": "file",
                "uri": "/workspace/f.txt",
                "label": "f.txt",
            },
            {"id": "diff", "type": "diff", "text": "@@ -1 +1 @@\n-before\n+after"},
        ],
    }
    before = json.dumps(original)
    view = project_presentation(original, "session", "call", tool_name="fs_apply_edit_write")
    assert view is not None and view["action"] == "Write"
    assert view["subject"] == "file-link"
    assert view["blocks"] == original["blocks"]
    assert json.dumps(original) == before
    unknown = project_presentation(original, "session", "call", tool_name="third_party")
    assert unknown == original


def test_persisted_block_and_pages_reconstruct_exact_unicode_content() -> None:
    text = "aé🌻\n" * 5000
    full = ToolPresentation(
        summary="Loaded report", blocks=[{"id": "body", "type": "markdown", "text": text}]
    ).model_dump(exclude_none=True)
    part = Part(type="tool_result", call_id="call", presentation=full)
    restored = Part.model_validate_json(part.model_dump_json())
    projection = project_presentation(restored.presentation, "session", "call")
    block = projection["blocks"][0]
    assert len(block["text"]) == PAGE_CHARS
    assert block["content_ref"]["session_id"] == "session"
    assembled = block["text"]
    cursor = block["content_ref"]["cursor"]
    while cursor is not None:
        page = presentation_page(restored.presentation["blocks"][0], cursor)
        assembled += page["text"]
        cursor = page["next_cursor"]
    assert assembled == text
    with pytest.raises(ValueError):
        presentation_page(full["blocks"][0], len(text) + 1)


def test_paged_content_is_scoped_to_session_call_and_block() -> None:
    app = FastAPI()
    part = Part(
        type="tool_result",
        call_id="call",
        presentation={
            "summary": "",
            "blocks": [{"id": "body", "type": "text", "text": "private result"}],
        },
    )
    app.state.sessions = {"owner": object(), "other": object()}
    app.state.messages = {"owner": [SimpleNamespace(parts=[part])]}
    register_messages_routes(app, SimpleNamespace())
    with TestClient(app) as client:
        path = "/v1/sessions/owner/tools/call/presentation/body"
        assert client.get(path).json()["text"] == "private result"
        assert client.get(path + "?cursor=-1").status_code == 400
        assert client.get(path.replace("owner", "other")).status_code == 404
        assert client.get(path.replace("call/", "wrong/")).status_code == 404
        assert client.get(path.replace("body", "wrong")).status_code == 404


def test_adapter_failure_keeps_original_result_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    original = {"structuredContent": {"answer": [1, 2, 3]}}
    before = json.dumps(original)

    def broken(args: Any, result: Any, capture: Any) -> dict[str, Any]:
        raise RuntimeError("presenter failed")

    monkeypatch.setitem(MCP_PRESENTATION_ADAPTERS, "broken", PresentationAdapter(broken))
    assert present_mcp_result("broken", {}, original)["diagnostic"] == "presentation_failed"
    assert json.dumps(original) == before


def test_child_output_is_a_paged_text_block_not_serialized_metadata() -> None:
    output = "Research evidence\n" * 1000
    raw = json.dumps({"task_id": "child", "status": "completed", "output": output})
    result = native_presentation("task_output", {}, raw, None)
    projected = project_presentation(result, "parent", "collect")
    assert projected["blocks"][0]["text"] == output[:PAGE_CHARS]
    assert projected["blocks"][0]["content_ref"]["total_chars"] == len(output)
    assert "error_reason" not in json.dumps(projected)
    assert json.loads(raw)["output"] == output


def test_terminal_append_offsets_and_final_order() -> None:
    registry = ToolProgressRegistry()
    handle = registry.started("call", "session")
    seen = []
    for stream, text in [("stdout", "first\n"), ("stderr", "warning\n"), ("stdout", "last\n")]:
        projected = registry.project(
            "shell_bash",
            {
                "observer_handle": handle,
                "message": json.dumps(
                    {"type": "clio.terminal.chunk", "stream": stream, "text": text}
                ),
            },
        )
        seen.append(projected[1]["presentation_delta"])
    assembled = ""
    for delta in seen:
        assert delta["offset"] == len(assembled)
        assembled += delta["text"]
    assert registry.completed("call") == assembled
    assert assembled.index("first") < assembled.index("warning") < assembled.index("last")
