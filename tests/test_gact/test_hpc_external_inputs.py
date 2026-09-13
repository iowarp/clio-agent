"""External read inputs remain visible without granting artifact-store custody."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.lineage import build_activity_lineage, build_lineage
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.artifacts.transform_edges import detect_used_edges
from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge
from clio_agent.gact.artifacts.transforms import (
    _record_transform_failure,
    record_transform,
    transform_from_payload,
)
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.gact.semantic_events import SSE_UI_EVENT_TYPES
from clio_agent.gact.types import Part
from tests.test_gact.test_artifacts_s5 import _make_app, _register_file


def test_external_input_is_used_in_lineage(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "reference.csv"
    external.write_text("value\n42\n", encoding="utf-8")
    app, session, _ = _make_app(root)
    _, version = _register_file(app, session, root, "report.txt", b"summary", "report")
    record = record_transform(
        app,
        session.id,
        tool_name="pandas_profile_csv",
        args={"data_path": str(external)},
        call_id="report",
        ok=True,
        result={},
        minted=[version],
        workspace_id="ws1",
    )
    assert record is not None
    assert len(record.used) == 1
    edge = record.used[0]
    assert edge.external_ref == f"external:{external}"
    assert edge.name == "reference.csv"
    assert not edge.sha256 and not edge.artifact_id
    from clio_agent.gact.artifacts.registry import get_registry

    graph = build_lineage(get_registry(app), artifact_id=version.artifact_id, direction="upstream")
    assert graph is not None
    assert any(node["id"] == edge.external_ref for node in graph["nodes"])


def test_external_reference_never_reads_or_hashes_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, session, _ = _make_app(tmp_path)
    external = tmp_path.parent / "outside.csv"

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external input must not be opened or hashed")

    monkeypatch.setattr("clio_agent.gact.artifacts.minting.compute_identity", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    scan = detect_used_edges(
        app,
        session.id,
        args={"file_path": str(external), "output_path": str(external)},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name="plot_line_plot",
        allow_external_inputs=True,
    )
    assert len(scan.edges) == 1
    assert scan.edges[0].note == "external_input_not_hashed"


def test_failed_call_does_not_assert_external_use(tmp_path: Path) -> None:
    app, session, _ = _make_app(tmp_path)
    record = record_transform(
        app,
        session.id,
        tool_name="pandas_profile_csv",
        args={"data_path": str(tmp_path.parent / "absent.csv")},
        call_id="failed",
        ok=False,
        result={},
        minted=[],
        workspace_id="ws1",
    )
    assert record is not None and not record.used


@pytest.mark.parametrize(
    ("tool_name", "args", "expected_args"),
    [
        ("fs_read_file", {"filepath": "source.csv"}, ["filepath"]),
        ("geo_filter_points_by_radius", {"data_path": "source.csv"}, ["data_path"]),
        (
            "pandas_merge_datasets",
            {"left_file": "a.csv", "right_file": "b.csv"},
            ["left_file", "right_file"],
        ),
        ("plot_line_plot", {"file_path": "source.csv", "output_path": "plot.png"}, ["file_path"]),
    ],
)
def test_only_declared_consuming_schema_args_become_external_uses(
    tmp_path: Path, tool_name: str, args: dict[str, str], expected_args: list[str]
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app, session, _ = _make_app(root)
    external_args = {key: str(tmp_path.parent / value) for key, value in args.items()}
    scan = detect_used_edges(
        app,
        session.id,
        args=external_args,
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name=tool_name,
        allow_external_inputs=True,
    )
    assert [edge.arg for edge in scan.edges] == expected_args


@pytest.mark.parametrize("tool_name", ["fs_list_directory", "unknown_reader"])
def test_read_only_or_path_shaped_unknown_tool_does_not_imply_consumption(
    tmp_path: Path, tool_name: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app, session, _ = _make_app(root)
    scan = detect_used_edges(
        app,
        session.id,
        args={
            "path": str(tmp_path.parent / "outside.csv"),
            "filepath": str(tmp_path.parent / "outside.csv"),
        },
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name=tool_name,
        allow_external_inputs=True,
    )
    assert not scan.edges
    if tool_name == "unknown_reader":
        assert {note["reason"] for note in scan.notes} == {"external_input_contract_unknown"}
    else:
        assert not scan.notes


def test_propose_edit_records_existing_external_source_but_not_new_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    existing = tmp_path.parent / "existing.txt"
    existing.write_text("old", encoding="utf-8")
    app, session, _ = _make_app(root)
    consumed = detect_used_edges(
        app,
        session.id,
        args={"filepath": str(existing), "new_content": "new"},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name="fs_propose_edit",
        allow_external_inputs=True,
    )
    created = detect_used_edges(
        app,
        session.id,
        args={"filepath": str(tmp_path.parent / "new.txt"), "new_content": "new"},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name="fs_propose_edit",
        allow_external_inputs=True,
    )
    assert [edge.name for edge in consumed.edges] == ["existing.txt"]
    assert not created.edges and not created.notes


def test_unknown_contract_warning_ignores_slash_containing_text_and_query(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app, session, _ = _make_app(root)
    scan = detect_used_edges(
        app,
        session.id,
        args={"content": "ratio 1/2", "query": "region/city"},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name="unknown_reader",
        allow_external_inputs=True,
    )
    assert not scan.edges and not scan.notes


def test_repeated_external_path_is_deduplicated_within_call_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app, session, _ = _make_app(root)
    external = str(tmp_path.parent / "shared.csv")
    first = record_transform(
        app,
        session.id,
        tool_name="pandas_merge_datasets",
        args={"left_file": external, "right_file": external},
        call_id="first",
        ok=True,
        result={},
        minted=[],
        workspace_id="ws1",
    )
    second = record_transform(
        app,
        session.id,
        tool_name="pandas_profile_csv",
        args={"data_path": external},
        call_id="second",
        ok=True,
        result={},
        minted=[],
        workspace_id="ws1",
    )
    assert first is not None and len(first.used) == 1
    assert second is not None and len(second.used) == 1
    assert first.call_id != second.call_id


def test_output_free_read_activity_and_external_source_survive_reload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app, session, _ = _make_app(root)
    external = tmp_path.parent / "observations.csv"
    record = record_transform(
        app,
        session.id,
        tool_name="fs_read_file",
        args={"filepath": str(external)},
        call_id="read-only",
        ok=True,
        result={"content": "value\n42\n"},
        minted=[],
        workspace_id="ws1",
    )
    assert record is not None and not record.generated
    reloaded_record = transform_from_payload(record.to_payload())
    assert reloaded_record is not None
    reloaded = ArtifactRegistry()
    reloaded.record_transform(reloaded_record)

    graph = build_activity_lineage(reloaded, "read-only")
    assert graph is not None
    assert graph["root"] == "activity:read-only"
    external_node = next(node for node in graph["nodes"] if node.get("external"))
    assert external_node["name"] == "observations.csv"
    assert "artifactId" not in external_node
    assert graph["edges"] == [
        {
            "from": f"external:{external}",
            "to": "activity:read-only",
            "type": "used",
            "evidence": "schema-arg",
        }
    ]


def test_provenance_incomplete_diagnostic_is_user_visible(tmp_path: Path) -> None:
    assert "artifact.provenance.incomplete" in SSE_UI_EVENT_TYPES
    app, session, arc = _make_app(tmp_path)
    _record_transform_failure(
        app,
        session.id,
        tool_name="pandas_profile_csv",
        call_id="successful-call",
        reason="RegistryError",
        detail="write failed",
        tool_ok=True,
    )
    diagnostic = next(
        event
        for event in arc.events
        if getattr(event, "event_type", "") == "artifact.provenance.incomplete"
    )
    assert diagnostic.payload["tool_success_retained"] is True
    assert diagnostic.payload["provenance_complete"] is False


def test_real_observer_persists_output_free_external_read_for_api_and_tool_row(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "real-source.csv"
    external.write_text("value\n42\n", encoding="utf-8")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        workspace = client.post(
            "/v1/workspaces", json={"name": "external read", "root_path": str(root)}
        ).json()
        session = client.post(
            "/v1/sessions", json={"title": "read", "workspace_id": workspace["id"]}
        ).json()
        observer = app.state.make_tool_observer()
        with _gact_app_context(app), _tool_session_context(session["id"]):
            observer("pandas_profile_csv", {"data_path": str(external)}, "started", None)
            observer(
                "pandas_profile_csv",
                {"data_path": str(external)},
                "completed",
                None,
                result={"rows_profiled": 1, "local_path": str(external)},
            )
        assert "reason=containment_rejected" not in caplog.text

        parts = app.state.live_assistant_parts[session["id"]]
        call_id = next(part.call_id for part in parts if part.type == "tool_call")
        result_part = next(part for part in parts if part.type == "tool_result")
        assert result_part.metadata["provenance_inputs"] == [
            {
                "name": "real-source.csv",
                "locator": str(external),
                "arg": "data_path",
                "evidence": "schema-arg",
                "note": "external_input_not_hashed",
            }
        ]
        reloaded_part = Part.model_validate(result_part.model_dump(mode="json"))
        assert (
            reloaded_part.metadata["provenance_inputs"] == result_part.metadata["provenance_inputs"]
        )
        response = client.get(f"/v1/transforms/{call_id}/lineage")
        assert response.status_code == 200
        graph = response.json()
        assert graph["root"] == f"activity:{call_id}"
        assert any(node.get("name") == "real-source.csv" for node in graph["nodes"])

        with _gact_app_context(app), _tool_session_context(session["id"]):
            observer("unknown_reader", {"path": str(external)}, "started", None)
            observer(
                "unknown_reader",
                {"path": str(external)},
                "completed",
                None,
                result={"status": "completed"},
            )
        warning_part = [
            part
            for part in app.state.live_assistant_parts[session["id"]]
            if part.type == "tool_result"
        ][-1]
        reloaded_warning_part = Part.model_validate(warning_part.model_dump(mode="json"))
        assert reloaded_warning_part.metadata["provenance_warnings"][0]["reason"] == (
            "external_input_contract_unknown"
        )


def test_out_of_root_output_that_is_not_an_input_echo_still_rejects_containment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    external_input = tmp_path / "input.csv"
    external_input.write_text("value\n42\n", encoding="utf-8")
    external_output = tmp_path / "actual-output.png"
    external_output.write_bytes(b"png")
    app, session, _ = _make_app(root)
    from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs

    minted = mint_tool_declared_outputs(
        app,
        session.id,
        tool_name="plot_line_plot",
        effective_args={"file_path": str(external_input)},
        call_id="outside-output",
        workspace_id="ws1",
        result={"local_path": str(external_output)},
    )
    assert not minted
    assert "reason=containment_rejected" in caplog.text
    assert "actual-output.png" in caplog.text


def test_write_capable_matching_input_result_preserves_mint_and_containment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    contained = root / "in-place.csv"
    contained.write_text("value\n43\n", encoding="utf-8")
    outside = tmp_path / "outside.csv"
    outside.write_text("value\n44\n", encoding="utf-8")
    app, session, _ = _make_app(root)
    from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs

    contained_minted = mint_tool_declared_outputs(
        app,
        session.id,
        tool_name="pandas_clean_data",
        effective_args={"file_path": str(contained)},
        call_id="contained-in-place",
        workspace_id="ws1",
        result={"local_path": str(contained)},
    )
    assert len(contained_minted) == 1
    assert contained_minted[0].path == str(contained)

    outside_minted = mint_tool_declared_outputs(
        app,
        session.id,
        tool_name="pandas_clean_data",
        effective_args={"file_path": str(outside)},
        call_id="outside-in-place",
        workspace_id="ws1",
        result={"local_path": str(outside)},
    )
    assert not outside_minted
    assert "reason=containment_rejected" in caplog.text
    assert "outside.csv" in caplog.text


def test_activity_lineage_caps_deduplicates_and_never_dangles(tmp_path: Path) -> None:
    app, session, _ = _make_app(tmp_path)
    record = record_transform(
        app,
        session.id,
        tool_name="pandas_profile_csv",
        args={},
        call_id="bounded",
        ok=True,
        result={},
        minted=[],
        workspace_id="ws1",
    )
    assert record is not None
    used = [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.SCHEMA_ARG,
            external_ref=f"external:C:/inputs/{index}.csv",
            name=f"{index}.csv",
        )
        for index in range(600)
    ]
    used.append(used[0])
    bounded = record.model_copy(update={"used": used})
    registry = ArtifactRegistry()
    registry.record_transform(bounded)
    graph = build_activity_lineage(registry, "bounded")
    assert graph is not None
    assert len(graph["nodes"]) == 500
    assert graph["truncated"] == {"reason": "node_cap", "nodes": 500}
    node_ids = {node["id"] for node in graph["nodes"]}
    assert len({(edge["from"], edge["to"], edge["type"]) for edge in graph["edges"]}) == len(
        graph["edges"]
    )
    assert all(edge["from"] in node_ids and edge["to"] in node_ids for edge in graph["edges"])
