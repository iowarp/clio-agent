"""Historical views preserve evidence while exposing the same readable result at every seam."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.agents.recorded_inspection_presentation import legacy_inspection_presentation
from clio_agent.gact.agents.skill_presentation import legacy_skill_presentation
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol.v3.message import transcript_entities
from clio_agent.gact.routes.deps import GactDeps
from clio_agent.gact.routes.messages import register_messages_routes
from clio_agent.gact.session_export_projection import archive_transcript_views
from clio_agent.gact.storage.ndp_presentations import endpoint_presentation
from clio_agent.gact.tool_result_presentation import PAGE_CHARS


@pytest.mark.parametrize("kind", ["reference", "runtime"])
def test_saved_detail_is_identical_in_history_paging_and_native_exports(kind: str) -> None:
    body = "Retained scientific instructions 🌲\n" * 180
    args = (
        {"skill_id": "work-with-ndp", "file": "references/discovery-and-analysis.md"}
        if kind == "reference"
        else {}
    )
    part: dict[str, Any] = {
        "type": "tool_result",
        "tool_name": "load_skill" if kind == "reference" else "prepare_execution_runtime",
        "call_id": "call",
        "presentation": {"summary": "", "blocks": []},
        "metadata": {
            "result": {
                "status": "missing_dependencies",
                "required_imports": {"cv2": {"status": "missing", "detail": body}},
            }
        },
    }
    if kind == "reference":
        part["structured_content"] = {
            "skill_id": "work-with-ndp",
            "file": args["file"],
            "path": "/removed/SKILL.md",
        }
        part["metadata"]["result"] = body
        part["presentation"].update(
            subject="skill-name",
            blocks=[
                {"id": "skill-name", "type": "text", "text": "work-with-ndp"},
                {"id": "content", "type": "markdown", "text": body},
            ],
        )
    else:
        part["presentation"]["blocks"] = [
            {"id": "os", "type": "text", "text": "Os: Windows"},
            {"id": "python_version", "type": "text", "text": "Python version: 3.14"},
        ]
    messages = [
        {
            "id": "message",
            "role": "assistant",
            "session_id": "session",
            "parts": [
                {
                    "type": "tool_call",
                    "call_id": "call",
                    "tool_name": part["tool_name"],
                    "input": args,
                },
                part,
            ],
        }
    ]
    before = deepcopy(messages)
    tool = transcript_entities(messages, "session")["tools"][0]
    block_id = "content" if kind == "reference" else "module-cv2"
    preview = next(b for b in tool["presentation"]["blocks"] if b["id"] == block_id)
    assert len(preview["text"]) == PAGE_CHARS
    assert preview["content_ref"]["session_id"] == "session"
    archive = archive_transcript_views({"session": {"id": "session"}, "messages": messages})
    full = next(
        b for b in archive["session"]["tools"][0]["presentation"]["blocks"] if b["id"] == block_id
    )["text"]
    app = FastAPI()
    app.state.sessions = {"session": object(), "other": object()}
    app.state.messages = {
        "session": [SimpleNamespace(parts=[Part.model_validate(p) for p in messages[0]["parts"]])]
    }
    register_messages_routes(app, cast(GactDeps, SimpleNamespace()))
    with TestClient(app) as client:
        cursor = preview["content_ref"]["cursor"]
        reconstructed = preview["text"]
        while cursor is not None:
            page = client.get(
                f"/v1/sessions/session/tools/call/presentation/{block_id}?cursor={cursor}"
            )
            assert page.status_code == 200
            data = page.json()
            reconstructed += data["text"]
            cursor = data["next_cursor"]
        assert (
            client.get(f"/v1/sessions/other/tools/call/presentation/{block_id}").status_code == 404
        )
    assert reconstructed == full and body in full
    assert messages == before
    if kind == "reference":
        assert tool["presentation"]["action"] == "Read skill reference"
        assert not any(b["type"] == "link" for b in tool["presentation"]["blocks"])
    else:
        assert tool["presentation"]["status"] == "degraded"


def test_recovery_preserves_modern_presentations_errors_and_partial_evidence() -> None:
    part = {
        "type": "tool_result",
        "tool_name": "inspect_a2ui_surface",
        "presentation": {"summary": "", "blocks": []},
        "metadata": {"result": {"truncated": True, "preview": '{"viewers":['}},
    }
    view = legacy_inspection_presentation(part)
    assert view and view["status"] == "degraded" and "partial" in view["summary"]
    part["is_error"] = True
    assert legacy_inspection_presentation(part) is None
    part["is_error"] = False
    part["presentation"]["blocks"] = [
        {"id": "current", "type": "text", "text": "Original observation"}
    ]
    assert legacy_inspection_presentation(part) is None
    assert legacy_skill_presentation(part) is None


def test_connected_folder_and_write_receipts_explain_access_and_destination() -> None:
    listing = native_presentation(
        "source_opened",
        {},
        {
            "source_id": "source",
            "folder": "measurements",
            "access": "read_only",
            "writes_propagate": False,
            "entries": [{"path": "historical.csv", "kind": "file", "size": 9940}],
            "next_offset": 200,
        },
        None,
    )
    assert listing["summary"] == "1 folder entry"
    assert any(b["label"] == "historical.csv" and "9940" in b["text"] for b in listing["blocks"])
    assert "read only" in str(listing) and "next page" in str(listing)
    for propagate, expected in [(False, "Changes staged"), (True, "Source updated")]:
        result = {"path": "analysis.csv", "revision": "new-revision", "writes_propagate": propagate}
        before = deepcopy(result)
        view = native_presentation("source_written", {}, result, None)
        assert view["summary"] == expected
        assert "analysis.csv" in str(view) and result == before


def test_saved_http_error_is_a_failure_with_an_exact_response() -> None:
    result = {
        "status_code": 403,
        "bytes_received": 9,
        "operation_summary": "Read station observations",
        "preview": "Forbidden",
        "content_type": "text/plain",
        "stop_reason": "completed",
    }
    before = deepcopy(result)
    view = endpoint_presentation({}, result, None)
    assert view["status"] == "failed"
    assert view["blocks"][0]["text"] == "Read station observations"
    assert "Forbidden" in str(view) and result == before


def test_native_archive_uses_complete_recorded_observation_before_a_truncated_message_preview() -> (
    None
):
    result = {
        "operations": [
            {
                "id": "GET /observations",
                "summary": "Read observations",
                "method": "GET",
                "path": "/observations",
                "read_only": True,
            }
        ],
        "operation_count": 1,
        "matched_count": 1,
    }
    transcript = {
        "session": {"id": "session"},
        "messages": [
            {
                "id": "message",
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool_result",
                        "tool_name": "connected_data_ndp_endpoint_operations",
                        "call_id": "call",
                        "presentation": {"summary": "1 operation", "blocks": []},
                        "metadata": {"result": {"truncated": True, "preview": '{"operations":['}},
                    }
                ],
            }
        ],
        "tool_records": [
            {"call_id": "call", "tool": "connected_data_ndp_endpoint_operations", "output": result}
        ],
    }
    before = deepcopy(transcript)
    view = archive_transcript_views(transcript)["session"]["tools"][0]["presentation"]
    assert "Read observations" in str(view)
    assert "Only a truncated" not in str(view)
    assert view.get("status") is None
    assert not any(b.get("language") == "json" for b in view["blocks"])
    assert transcript == before
