"""NDP output must be inspectable through CLIO's ordinary semantic result blocks."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.parts import Part
from clio_agent.gact.protocol.v3.message import transcript_entities
from clio_agent.gact.routes.deps import GactDeps
from clio_agent.gact.routes.messages import register_messages_routes
from clio_agent.gact.session_export_projection import archive_transcript_views
from clio_agent.gact.storage.ndp_presentations import endpoint_presentation, ndp_presentation
from clio_agent.gact.storage.ndp_recorded_presentation import legacy_ndp_presentation
from clio_agent.gact.tool_result_presentation import (
    PAGE_CHARS,
    ToolPresentation,
    project_presentation,
)


def capture(**changes: Any) -> dict[str, Any]:
    """Provide a scientific response and its normal retained receipt for contract tests."""
    return {
        "id": "capture",
        "endpoint_id": "ndpep_lab",
        "endpoint_name": "Station service",
        "endpoint_url": "http://127.0.0.1:8002",
        "ssh_profile": "ares",
        "workspace_id": "workspace",
        "operation_id": "GET /stations",
        "status_code": 200,
        "content_type": "application/json",
        "bytes_received": 9180,
        "stop_reason": "completed",
        "parameters": {"station": "II.BFO"},
        "preview": '{"stations": ["II.BFO"]}',
        "resource_id": "resource",
        "workspace_path": r"D:\workspace\stations.json",
        "sha256": "a" * 64,
        **changes,
    }


def body(view: dict[str, Any] | None) -> str:
    assert view is not None
    return json.dumps(ToolPresentation.model_validate(view).model_dump(), ensure_ascii=False)


def test_capture_shows_operation_input_response_saved_file_and_verification() -> None:
    row = capture()
    before = deepcopy(row)
    view = endpoint_presentation({}, row, None)
    text = body(view)
    for expected in [
        "Station service",
        "ares",
        "GET /stations",
        "II.BFO",
        "HTTP 200",
        "9180 bytes saved",
        "stations.json",
        "Captured response",
        "a" * 64,
    ]:
        assert expected in text
    assert next(b for b in view["blocks"] if b["id"] == "saved-response") == {
        "id": "saved-response",
        "type": "link",
        "target": "resource",
        "uri": "resource",
        "label": "stations.json",
    }
    assert row == before


def test_response_preview_masks_credentials_without_changing_observations() -> None:
    preview = json.dumps(
        {
            "access_token": "private-token",
            "password": "private-password",
            "east": 0.5,
            "cursor": "keep-exact-cursor",
            "url": "https://user:private-pass@example.org/a?X-Amz-Signature=private-sig",
        }
    )
    row = capture(preview=preview)
    text = body(endpoint_presentation({}, row, None))
    for secret in ["private-token", "private-password", "private-pass", "private-sig"]:
        assert secret not in text
    assert "keep-exact-cursor" in text and "0.5" in text
    assert row["preview"] == preview


def test_sse_control_events_are_not_presented_as_scientific_record_count() -> None:
    row = capture(
        content_type="text/event-stream",
        events=5,
        stop_reason="event_limit",
        preview='event: record\ndata: {"time": 123}\n\nevent: done\ndata: {"resume": "exact-cursor"}',
    )
    text = body(endpoint_presentation({}, row, None))
    assert "SSE events: 5 (includes control events)" in text
    assert "exact-cursor" in text and "remote producer may still be running" in text
    assert "5 records" not in text


def test_catalog_page_shows_datasets_resources_filters_and_next_page() -> None:
    row = {
        "datasets": [
            {
                "id": "dataset",
                "title": "Snowfall observations",
                "description": "Measured snow",
                "organization": "snow-lab",
                "endpoint_name": "NDP catalog",
                "resources": [{"id": "csv", "name": "Snow.csv", "format": "CSV", "size": 100}],
            }
        ],
        "total": 80,
        "next_offset": 50,
        "warnings": ["One Endpoint was unavailable"],
    }
    text = body(ndp_presentation({"query": "snow", "offset": 0}, row, None))
    for expected in [
        "Snowfall observations",
        "Measured snow",
        "snow-lab",
        "Snow.csv",
        "Resource ID: csv",
        "Showing 1 of 80",
        "Next offset: 50",
        "One Endpoint was unavailable",
    ]:
        assert expected in text


def test_organizations_are_real_names_and_their_catalog_identities() -> None:
    view = ndp_presentation(
        {},
        {
            "organizations": [
                {
                    "id": "earthscope_consortium",
                    "endpoint_name": "NDP catalog",
                    "endpoint_id": "ndp",
                }
            ]
        },
        None,
    )
    assert "earthscope_consortium" in body(view)
    assert "NDP catalog" in body(view)


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"datasets": [], "total": 0}, "No datasets matched"),
        ({"organizations": []}, "No organizations matched"),
        ({}, "No structured NDP discovery result"),
    ],
)
def test_discovery_empty_and_unavailable_results_are_not_blank(
    row: dict[str, Any], expected: str
) -> None:
    assert expected in body(ndp_presentation({}, row, None))


@pytest.mark.parametrize(
    "row,expected",
    [
        (
            {
                "name": "Ares",
                "api_title": "NDP API",
                "operations": [
                    {
                        "id": "GET /stations",
                        "summary": "Query station epochs",
                        "parameters": [{"name": "station"}],
                    }
                ],
            },
            "Query station epochs",
        ),
        (
            {
                "name": "Ares",
                "operations": [],
                "operation_count": 40,
                "search_note": "Use fewer words",
                "query": "absent",
            },
            "40 in this API",
        ),
        (
            {
                "status": "choose_api_url",
                "candidates": ["http://127.0.0.1:8002"],
                "message": "Choose an API URL",
            },
            "8002",
        ),
        ({"state": "sign_in_required"}, "Sign in to your NDP account"),
        (
            {
                "artifact_id": "artifact",
                "state": "verified",
                "size": 100,
                "bucket": "science",
                "key": "results/table.csv",
                "sha256": "abc",
                "reused": True,
            },
            "results/table.csv",
        ),
        ({"artifact_id": "artifact", "state": "unverified", "size": 100}, "unverified"),
        ({}, "No structured NDP result"),
        (capture(bytes_received=0, preview=""), "No response body"),
    ],
)
def test_all_endpoint_outcomes_have_defined_output(row: dict[str, Any], expected: str) -> None:
    assert expected in body(endpoint_presentation({}, row, None))


def recorded(preview: str = '{"station":"II.BFO"}') -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_name": "connected_data_ndp_endpoint_request",
        "call_id": "call",
        "presentation": {"summary": "9180 bytes captured, completed", "blocks": []},
        "metadata": {"result": capture(preview=preview)},
    }


def test_historical_recovery_is_display_only_and_preserves_existing_or_failed_output() -> None:
    part = recorded()
    before = deepcopy(part)
    assert "II.BFO" in body(legacy_ndp_presentation(part))
    assert part == before
    part["is_error"] = True
    assert legacy_ndp_presentation(part) is None
    part["is_error"] = False
    part["presentation"]["blocks"] = [{"id": "already", "type": "text", "text": "Original view"}]
    assert legacy_ndp_presentation(part) is None


def test_old_truncated_metadata_stays_explicitly_truncated() -> None:
    part = recorded()
    part["metadata"]["result"] = {
        "preview": '{"operations":["GET /sta',
        "truncated": True,
        "original_chars": 20000,
    }
    text = body(legacy_ndp_presentation(part))
    assert "Only a truncated result was retained" in text
    assert "0 Endpoint operations" not in text


def test_truncated_historical_credentials_are_not_exposed() -> None:
    part = recorded()
    part["metadata"]["result"] = {
        "preview": '{"access_token":"private-truncated-token',
        "truncated": True,
        "original_chars": 20000,
    }
    text = body(legacy_ndp_presentation(part))
    assert "private-truncated-token" not in text
    assert "[redacted]" in text


def test_historical_response_pages_remain_scoped_and_reconstruct_the_recorded_body() -> None:
    preview = "actual observation\n" * 500
    part = Part.model_validate(recorded(preview))
    app = FastAPI()
    app.state.sessions = {"session": object(), "other": object()}
    app.state.messages = {"session": [SimpleNamespace(parts=[part])]}
    register_messages_routes(app, cast(GactDeps, SimpleNamespace()))
    full = legacy_ndp_presentation(part.model_dump())
    projected = project_presentation(full, "session", "call")
    assert projected is not None
    block = next(b for b in projected["blocks"] if b["id"] == "response-preview")
    assert len(block["text"]) == PAGE_CHARS
    reconstructed = block["text"]
    with TestClient(app) as client:
        cursor = block["content_ref"]["cursor"]
        while cursor is not None:
            response = client.get(
                f"/v1/sessions/session/tools/call/presentation/response-preview?cursor={cursor}"
            )
            assert response.status_code == 200
            page = response.json()
            reconstructed += page["text"]
            cursor = page["next_cursor"]
        assert (
            client.get("/v1/sessions/other/tools/call/presentation/response-preview").status_code
            == 404
        )
    assert reconstructed == preview
    assert part.presentation is not None
    assert part.presentation["blocks"] == []


def test_history_and_native_archive_recover_the_same_output_without_changing_ledger() -> None:
    preview = "retained station observation\n" * 300
    messages = [
        {
            "id": "message",
            "role": "assistant",
            "session_id": "session",
            "parts": [
                {
                    "type": "tool_call",
                    "tool_name": "connected_data_ndp_endpoint_request",
                    "call_id": "call",
                    "input": {"endpoint_id": "ndpep_lab", "operation_id": "GET /stations"},
                },
                recorded(preview),
            ],
        }
    ]
    before = deepcopy(messages)
    tool = transcript_entities(messages, "session")["tools"][0]
    assert any(b.get("content_ref") for b in tool["presentation"]["blocks"])
    archive = archive_transcript_views({"session": {"id": "session"}, "messages": messages})
    blocks = archive["session"]["tools"][0]["presentation"]["blocks"]
    assert next(b for b in blocks if b["id"] == "response-preview")["text"] == preview
    assert messages == before
