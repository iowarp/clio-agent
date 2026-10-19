"""Agent approvals and trusted HTTP routes share the same live Endpoint owner."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.storage import ndp_endpoint_tools
from tests.test_gact.test_ndp_endpoints import peer as peer_fixture
from tests.test_gact.test_source_connect_runtime import bound as bound_fixture

peer = peer_fixture
bound = bound_fixture


def test_agent_denial_and_actual_response_custody(bound: tuple[Any, Any, Path], peer: str) -> None:
    service, app, _ = bound
    app.state.pending_permission_gate = lambda *args: "deny"
    with pytest.raises(PermissionError, match="not approved"):
        ndp_endpoint_tools.connected_data_ndp_endpoint_connect("lab", peer)
    assert service.ndp_endpoints.connections("ws_default") == []
    decisions = []

    def approve(*args: Any) -> str:
        decisions.append(args)
        return "allow"

    app.state.pending_permission_gate = approve
    connection = ndp_endpoint_tools.connected_data_ndp_endpoint_connect("lab", peer)
    assert decisions[-1][1]["url"] == peer
    operations = ndp_endpoint_tools.connected_data_ndp_endpoint_operations(
        connection["id"], "stations"
    )
    assert len(operations["operations"]) == 1
    app.state.pending_permission_gate = lambda *args: "deny"
    with pytest.raises(PermissionError, match="not approved"):
        ndp_endpoint_tools.connected_data_ndp_endpoint_request(
            connection["id"], "POST /jobs", body={"station": "ANMO"}
        )
    assert service.ndp_endpoints.captures("ws_default", connection["id"]) == []
    app.state.pending_permission_gate = approve
    capture = ndp_endpoint_tools.connected_data_ndp_endpoint_request(
        connection["id"], "GET /stations", parameters={"limit": 2}
    )
    assert capture["preview"] == '{"stations":["ANMO"]}'
    assert capture["materialization"]["state"] == "ready"
    assert Path(capture["workspace_path"]).read_bytes() == capture["preview"].encode()
    assert capture["source_artifact_id"]
    assert decisions[-1][1]["method"] == "GET"
    assert decisions[-1][1]["parameters"] == {"limit": 2}
    receipt = service.ndp_endpoints.captures("ws_default", connection["id"])[0]
    assert receipt.workspace_path == capture["workspace_path"]

    # The observer must expose the response from this actual HTTP request using
    # the tool's declared adapter, while preserving the model's original result.
    from clio_agent.gact.agents.tool_instrumentation import instrument_tools
    from clio_agent.gact.presentation_observer import completed_presentation

    instrument_tools(ndp_endpoint_tools.build_ndp_endpoint_tools())
    observed, view = completed_presentation(
        "connected_data_ndp_endpoint_request", {}, capture, None, ""
    )
    assert observed is capture
    assert any(
        block["id"] == "response-preview" and "ANMO" in block["text"] for block in view["blocks"]
    )
    assert any(
        block["id"] == "saved-response" and block["uri"] == capture["resource_id"]
        for block in view["blocks"]
    )


def test_native_permission_names_the_operation_and_destination(
    bound: tuple[Any, Any, Path], peer: str
) -> None:
    """The actual gate retains a readable summary without weakening its approval decision."""
    from clio_agent.gact.permission_gate import resolve_permission

    service, app, _ = bound
    connection = service.ndp_endpoints.connect(
        "ws_default", ndp_endpoint_tools.EndpointConnectionInput(name="Lab", url=peer)
    )
    app.state.pending_permission_gate = app.state.make_permission_gate()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            ndp_endpoint_tools.connected_data_ndp_endpoint_request, connection.id, "GET /stations"
        )
        deadline = time.monotonic() + 5
        while not app.state.permissions and time.monotonic() < deadline:
            time.sleep(0.01)
        pending = next(row for row in app.state.permissions.values() if row["status"] == "pending")
        assert pending["summary"] == "Run GET /stations on Lab"
        assert not future.done()
        resolve_permission(app, pending["id"], "allow")
        assert future.result(timeout=5)["status_code"] == 200


def test_operation_search_matches_words_and_retains_full_inventory(
    bound: tuple[Any, Any, Path], peer: str
) -> None:
    """A natural multiword query must not hide a usable observed station route."""
    service, _, _ = bound
    connection = service.ndp_endpoints.connect(
        "ws_default", ndp_endpoint_tools.EndpointConnectionInput(name="Lab", url=peer)
    )
    result = ndp_endpoint_tools.connected_data_ndp_endpoint_operations(
        connection.id, "FDSN stations epochs"
    )
    assert [row["id"] for row in result["operations"]] == ["GET /stations"]
    assert result["matched_count"] == 1
    assert result["operation_count"] > 1
    absent = ndp_endpoint_tools.connected_data_ndp_endpoint_operations(connection.id, "absentxyz")
    assert absent["operations"] == []
    assert absent["operation_count"] == result["operation_count"]
    assert absent["matched_count"] == 0
    assert absent["search_note"]
    all_rows = ndp_endpoint_tools.connected_data_ndp_endpoint_operations(connection.id)
    assert all_rows["matched_count"] == all_rows["operation_count"]


def test_operation_search_reads_descriptions_and_ranks_specific_matches(
    bound: tuple[Any, Any, Path], peer: str
) -> None:
    """The callable inventory preserves actual schemas and favors more relevant routes."""
    service, _, _ = bound
    connection = service.ndp_endpoints.connect(
        "ws_default", ndp_endpoint_tools.EndpointConnectionInput(name="Lab", url=peer)
    )
    connection.schema_document["paths"]["/stations"]["get"]["description"] = (
        "Station epochs and coordinates"
    )
    connection.schema_document["paths"]["/organization"]["get"]["description"] = (
        "Station publishers"
    )
    service.ndp_endpoints.store.put("ndp_endpoint", connection.id, connection)
    result = ndp_endpoint_tools.connected_data_ndp_endpoint_operations(
        connection.id, "STATION epochs"
    )
    assert [row["id"] for row in result["operations"]][:2] == ["GET /stations", "GET /organization"]
    assert result["operations"][0]["parameters"][0]["name"] == "limit"
