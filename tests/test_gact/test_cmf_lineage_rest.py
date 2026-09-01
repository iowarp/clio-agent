"""The server-mode lineage reader maps CMF REST rows onto CLIO's graph shape.

Driven by a canned REST surface: the reader is asserted to produce the SAME node
and edge vocabulary the local MLMD worker produces, so
``GET /v1/artifacts/{id}/lineage`` answers one shape whichever lane wrote the
metadata.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from clio_agent.gact.artifacts.provenance.cmf_lineage_rest import (
    CMFRestLineageReader,
    cmf_property,
    lineage_display_id,
)

_PIPELINE = "clio-agent"
_STAGE = "clio-agent/artifacts"


def _artifact_row(
    artifact_id: str,
    name: str,
    *,
    version: int = 1,
    call_id: str = "",
    prior_version: int = 0,
    mechanism: str = "tool_schema",
    cmf_id: int = 1,
) -> dict[str, Any]:
    return {
        "artifact_id": cmf_id,
        "name": f"{name}:v{version}",
        "artifact_type": "Dataset",
        "custom_properties_clio_artifact_id": artifact_id,
        "custom_properties_clio_name": name,
        "custom_properties_clio_version": version,
        "custom_properties_clio_kind": "dataset",
        "custom_properties_clio_sha256": "a" * 64,
        "custom_properties_clio_workspace_id": "ws_1",
        "custom_properties_clio_mechanism": mechanism,
        "custom_properties_clio_prior_version": prior_version,
        "custom_properties_clio_producer_json": json.dumps({"call_id": call_id}),
    }


def _execution_row(call_id: str, *, tool: str = "fs_apply_edit_write") -> dict[str, Any]:
    return {
        "execution_id": 7,
        "name": f"clio:{call_id}",
        "execution_properties": {
            "clio_call_id": call_id,
            "clio_tool": tool,
            "clio_status": "success",
            "clio_kind": "ordinary",
            "clio_session_id": "sess_1",
            "clio_turn_id": "msg_1",
            "clio_environment_json": json.dumps({"tier": "container"}),
        },
    }


class _Surface:
    """A canned cmf-server REST surface."""

    def __init__(self) -> None:
        self.artifacts: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.layers: list[list[dict[str, Any]]] = []
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.startswith("/api/pipeline-stages/"):
            return httpx.Response(200, json={"stages": [_STAGE], "total_stages": 1})
        if path.startswith("/api/artifact-types-by-stage/"):
            return httpx.Response(200, json=["Dataset"])
        if path.startswith("/api/artifacts-by-stage/"):
            return httpx.Response(200, json={"items": self.artifacts})
        if path.startswith("/api/executions-by-stage/"):
            return httpx.Response(200, json={"items": self.executions})
        if path.startswith("/api/artifact-lineage/tangled-tree/"):
            return httpx.Response(200, json=self.layers)
        return httpx.Response(404, json={"detail": "no route"})


def _reader(surface: _Surface) -> CMFRestLineageReader:
    client = httpx.Client(
        transport=httpx.MockTransport(surface.handler), base_url="http://cmf.test"
    )
    return CMFRestLineageReader("http://cmf.test", _PIPELINE, client=client)


def test_rows_map_onto_clio_artifact_and_activity_nodes() -> None:
    surface = _Surface()
    surface.artifacts = [_artifact_row("artifact_1", "a.csv", call_id="call_1")]
    surface.executions = [_execution_row("call_1")]
    graph = _reader(surface).lineage("artifact_1", direction="both", depth=3)

    assert graph is not None
    assert graph["provider"] == "cmf"
    assert graph["root"] == "artifact_1"
    assert graph["direction"] == "both"
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert set(nodes) == {"artifact_1", "activity:call_1"}
    artifact = nodes["artifact_1"]
    assert artifact["type"] == "artifact"
    assert artifact["name"] == "a.csv"
    assert artifact["version"] == 1
    assert artifact["kind"] == "dataset"
    assert artifact["sha256"] == "a" * 64
    assert artifact["workspace_id"] == "ws_1"
    assert artifact["producer_call_id"] == "call_1"
    activity = nodes["activity:call_1"]
    assert activity["type"] == "activity"
    assert activity["tool"] == "fs_apply_edit_write"
    assert activity["status"] == "success"
    assert activity["session_id"] == "sess_1"
    assert activity["environment_tier"] == "container"


def test_producer_yields_the_generated_edge() -> None:
    surface = _Surface()
    surface.artifacts = [_artifact_row("artifact_1", "a.csv", call_id="call_1")]
    surface.executions = [_execution_row("call_1")]
    graph = _reader(surface).lineage("artifact_1", direction="both", depth=3)
    assert graph is not None
    assert graph["edges"] == [
        {
            "from": "activity:call_1",
            "to": "artifact_1",
            "type": "generated",
            "evidence": "cmf-producer",
        }
    ]


def test_prior_version_yields_the_revision_edge() -> None:
    surface = _Surface()
    surface.artifacts = [
        _artifact_row("artifact_v1", "a.csv", version=1, cmf_id=1),
        _artifact_row("artifact_v2", "a.csv", version=2, prior_version=1, cmf_id=2),
    ]
    graph = _reader(surface).lineage("artifact_v2", direction="both", depth=3, complete=True)
    assert graph is not None
    assert {
        "from": "artifact_v2",
        "to": "artifact_v1",
        "type": "revision_of",
        "evidence": "hash-pair",
    } in graph["edges"]


def test_lineage_parents_become_used_edges_through_the_producing_activity() -> None:
    surface = _Surface()
    surface.artifacts = [
        _artifact_row("artifact_in", "a.csv", cmf_id=1),
        _artifact_row("artifact_out", "b.csv", call_id="call_1", cmf_id=2),
    ]
    surface.executions = [_execution_row("call_1")]
    surface.layers = [[{"id": "a.csv:v1", "parents": []}], [{"id": "b.csv:v1", "parents": ["a.csv:v1"]}]]
    graph = _reader(surface).lineage("artifact_out", direction="both", depth=3, complete=True)

    assert graph is not None
    edges = {(edge["from"], edge["to"], edge["type"]) for edge in graph["edges"]}
    assert ("artifact_in", "activity:call_1", "used") in edges
    assert ("activity:call_1", "artifact_out", "generated") in edges
    assert graph["truncated"] is None


def test_an_input_with_no_producing_call_is_reported_not_flattened() -> None:
    surface = _Surface()
    surface.artifacts = [
        _artifact_row("artifact_in", "a.csv", cmf_id=1),
        # No producer call_id: CLIO cannot attribute the input to an activity.
        _artifact_row("artifact_out", "b.csv", cmf_id=2),
    ]
    surface.layers = [[{"id": "b.csv:v1", "parents": ["a.csv:v1"]}]]
    graph = _reader(surface).lineage("artifact_out", direction="both", depth=3)
    assert graph is not None
    assert graph["truncated"]["reason"] == "cmf_lineage_edges_unmapped"
    assert graph["truncated"]["producer_unresolved"] == 1


def test_a_lineage_label_matching_no_known_artifact_is_reported() -> None:
    surface = _Surface()
    surface.artifacts = [_artifact_row("artifact_1", "a.csv", cmf_id=1)]
    surface.layers = [[{"id": "ghost.csv:9999", "parents": []}]]
    graph = _reader(surface).lineage("artifact_1", direction="both", depth=3)
    assert graph is not None
    assert graph["truncated"]["label_unmapped"] == 1


def test_an_unknown_artifact_is_none_so_the_route_can_404() -> None:
    surface = _Surface()
    surface.artifacts = [_artifact_row("artifact_1", "a.csv", cmf_id=1)]
    assert _reader(surface).lineage("artifact_absent", direction="both", depth=3) is None


def test_a_none_mechanism_artifact_is_a_gap_node() -> None:
    surface = _Surface()
    surface.artifacts = [_artifact_row("artifact_1", "a.csv", mechanism="none")]
    graph = _reader(surface).lineage("artifact_1", direction="both", depth=3)
    assert graph is not None
    assert graph["nodes"][0]["type"] == "gap"


def test_depth_bound_is_applied_to_the_component() -> None:
    surface = _Surface()
    surface.artifacts = [
        _artifact_row("artifact_v1", "a.csv", version=1, cmf_id=1),
        _artifact_row("artifact_v2", "a.csv", version=2, prior_version=1, cmf_id=2),
        _artifact_row("artifact_v3", "a.csv", version=3, prior_version=2, cmf_id=3),
    ]
    graph = _reader(surface).lineage("artifact_v3", direction="upstream", depth=1)
    assert graph is not None
    assert {node["id"] for node in graph["nodes"]} == {"artifact_v3", "artifact_v2"}
    assert graph["truncated"]["reason"] == "depth_horizon"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"clio_name": "direct"}, "direct"),
        ({"artifact_properties": {"clio_name": "nested"}}, "nested"),
        ({"properties": [{"name": "clio_name", "value": "listed"}]}, "listed"),
        ({"custom_properties_clio_name": "prefixed"}, "prefixed"),
        ({}, None),
    ],
)
def test_row_properties_are_read_from_every_server_row_shape(
    row: dict[str, Any], expected: Any
) -> None:
    assert cmf_property(row, "clio_name") == expected


def test_display_id_derivation_matches_the_pack_for_a_dataset() -> None:
    assert lineage_display_id("data/a.csv:abcdef12", "Dataset") == "a.csv:abcd"


def test_display_id_falls_back_to_the_name_when_the_pattern_does_not_fit() -> None:
    assert lineage_display_id("plain", "Model") == "plain"
