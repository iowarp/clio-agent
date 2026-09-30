"""Data-by-reference validation for dataUri-carrying components (#1533 S4).

The catalog's own JSON Schema can only check ``dataUri``'s SHAPE (the
``artifact://...`` pattern); it cannot see whether the artifact exists, is
readable, or actually carries the columns a ``*Field`` property names. These
tests drive :mod:`clio_agent.gact.a2ui_producer._data_reference` through the
real producer tools (``create_a2ui_surface`` / ``update_a2ui_components``),
covering every data-by-reference component: map, data-table, workflow, code,
mermaid, diff, chart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer import build_create_a2ui_surface_tool
from clio_agent.gact.app import build_app

WORKSPACE_ID = workspace_catalog_id()

CSV_ROWS = "time,lat,lon,label,entity\n0,34.0,-118.0,Site A,alpha\n1,34.1,-118.1,Site B,alpha\n"


@pytest.fixture(autouse=True)
def _prewarm_pyarrow() -> None:
    """Import pyarrow before this module's tests reach the table-query engine.

    The table-query engine's own ``import pyarrow as pa`` is otherwise this
    process's FIRST pyarrow import, deep inside a deferred (``noqa: PLC0415``)
    production import, which can race dspy's lazy numpy shim on first touch.
    This has to run at TEST time, not at collection/module-import time: under
    the flake-hunt job (many files collected per worker via
    ``--dist loadfile``), a module-level ``import pyarrow`` runs during
    collection, where it can itself lose that race against whatever else the
    worker already imported while collecting earlier files, turning a flaky
    runtime race into a hard collection ``ImportError``. Every other suite
    that exercises the table-query engine (e.g. test_artifact_table_query.py)
    imports pyarrow at module scope instead, but none of those carry the
    ``concurrency`` marker collection sweeps here, so they never hit this.
    """

    import pyarrow  # noqa: F401


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _workspace_session(c: TestClient, root: Path) -> tuple[str, str]:
    wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


def _producer_session(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str, Path]:
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    root = tmp_path / "ws"
    root.mkdir()
    c = _client(tmp_path)
    _wid, sid = _workspace_session(c, root)
    app = c.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}})
    remember_client_capabilities(app, sid, caps)
    return app, sid, root


def _create(components: list[dict[str, Any]]) -> dict[str, Any]:
    return build_create_a2ui_surface_tool()(surface_id="surface", components=components)


def _export_data_uri(app: Any, sid: str, root: Path, name: str, content: str) -> str:
    """Write ``content`` under the workspace and export it, returning its dataUri."""

    (root / name).write_text(content, encoding="utf-8")
    result = _create(
        [
            {"id": "root", "component": "clio.data-table.v1", "dataUri": name},
        ]
    )
    # This first surface is throwaway -- it only exists to run the export
    # boundary once and hand back the minted artifact:// reference.
    assert result.get("ok") is not False, result
    [exported] = result["exported_artifacts"]
    return str(exported["uri"])


# --------------------------------------------------------------------------- #
# dataUri must resolve to a REGISTERED, readable artifact
# --------------------------------------------------------------------------- #


def test_unregistered_data_uri_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": "artifact://artifact_doesnotexist",
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_data_reference_not_found"
    assert "artifact_doesnotexist" in result["detail"]


# --------------------------------------------------------------------------- #
# map / chart: *Field names checked against the referenced dataset's columns
# --------------------------------------------------------------------------- #


def test_map_data_uri_with_real_field_names_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "stations.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.map.v1",
                "dataUri": uri,
                "latitudeField": "lat",
                "longitudeField": "lon",
                "labelField": "label",
            }
        ]
    )

    assert result.get("ok") is not False, result
    assert result["rendered"] is True


def test_map_data_uri_with_a_wrong_field_name_names_available_columns(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "stations.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.map.v1",
                "dataUri": uri,
                "latitudeField": "latitude",  # wrong -- the real column is "lat"
                "longitudeField": "lon",
                "labelField": "label",
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "latitudeField='latitude'" in result["detail"]
    assert "lat" in result["detail"] and "lon" in result["detail"]


def test_chart_data_uri_with_real_field_names_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "series.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.chart.v1",
                "dataUri": uri,
                "preset": "trajectories",
                "xField": "time",
                "yField": "lat",
                "entityField": "entity",
            }
        ]
    )

    assert result.get("ok") is not False, result


def test_chart_data_uri_with_a_wrong_field_name_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "series.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.chart.v1",
                "dataUri": uri,
                "preset": "trajectories",
                "xField": "timestamp",  # wrong -- the real column is "time"
                "yField": "lat",
                "entityField": "entity",
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "xField='timestamp'" in result["detail"]


def test_data_table_data_uri_needs_no_field_validation(tmp_path: Path, monkeypatch: Any) -> None:
    """data-table carries no ``*Field`` properties of its own -- existence only."""

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create([{"id": "root", "component": "clio.data-table.v1", "dataUri": uri}])

    assert result.get("ok") is not False, result


def test_field_validation_refuses_a_non_tabular_data_uri(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "notes.txt", "just some text, not a table")

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.map.v1",
                "dataUri": uri,
                "latitudeField": "lat",
                "longitudeField": "lon",
                "labelField": "label",
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_data_reference_unsupported_format"


# --------------------------------------------------------------------------- #
# workflow: dataUri content must be {"nodes": [...], "edges": [...]}
# --------------------------------------------------------------------------- #


def test_workflow_data_uri_with_the_right_shape_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(
        app,
        sid,
        root,
        "workflow.json",
        '{"nodes": [{"id": "a", "label": "A", "status": "done"}], "edges": []}',
    )

    result = _create([{"id": "root", "component": "clio.workflow.v1", "dataUri": uri}])

    assert result.get("ok") is not False, result


def test_workflow_data_uri_that_is_not_json_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "workflow.json", "not json at all {")

    result = _create([{"id": "root", "component": "clio.workflow.v1", "dataUri": uri}])

    assert result["ok"] is False
    assert result["reason"] == "a2ui_data_reference_shape_invalid"


def test_workflow_data_uri_missing_nodes_or_edges_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "workflow.json", '{"nodes": []}')

    result = _create([{"id": "root", "component": "clio.workflow.v1", "dataUri": uri}])

    assert result["ok"] is False
    assert result["reason"] == "a2ui_data_reference_shape_invalid"
    assert "nodes" in result["detail"] and "edges" in result["detail"]


# --------------------------------------------------------------------------- #
# code / mermaid / diff: dataUri content just needs to be readable text
# --------------------------------------------------------------------------- #


def test_code_data_uri_readable_text_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "script.py", "print('hello')\n")

    result = _create(
        [{"id": "root", "component": "clio.code.v1", "dataUri": uri, "language": "python"}]
    )

    assert result.get("ok") is not False, result


def test_mermaid_data_uri_readable_text_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "diagram.mmd", "graph TD; A-->B;\n")

    result = _create([{"id": "root", "component": "clio.mermaid.v1", "dataUri": uri}])

    assert result.get("ok") is not False, result


def test_diff_data_uri_readable_text_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "change.diff", "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n")

    result = _create([{"id": "root", "component": "clio.diff.v1", "dataUri": uri, "path": "a.py"}])

    assert result.get("ok") is not False, result


def test_text_component_data_uri_that_is_undecodable_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    (root / "binary.dat").write_bytes(b"\xff\xfe\x00\x01\x80\x81")
    exported = _create([{"id": "root", "component": "clio.data-table.v1", "dataUri": "binary.dat"}])
    assert exported.get("ok") is not False, exported
    uri = str(exported["exported_artifacts"][0]["uri"])

    result = _create(
        [{"id": "root", "component": "clio.code.v1", "dataUri": uri, "language": "python"}]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_data_reference_unreadable"


# --------------------------------------------------------------------------- #
# selectionField (map/data-table, ahead of the clio-schemas contract landing
# it as a declared property): validated exactly like every other *Field the
# moment a caller supplies it, via the module directly (the CURRENT pinned
# catalog schema does not yet declare the property, so a full producer-tool
# round trip would fail schema validation on a correct value too).
# --------------------------------------------------------------------------- #


def test_map_selection_field_is_validated_against_real_columns(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from clio_agent.gact.a2ui_producer._data_reference import validate_component_data_references

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "stations.csv", CSV_ROWS)

    ok = validate_component_data_references(
        app,
        [
            {
                "id": "root",
                "component": "clio.map.v1",
                "dataUri": uri,
                "latitudeField": "lat",
                "longitudeField": "lon",
                "labelField": "label",
                "selectionField": "entity",
            }
        ],
    )
    assert ok is None

    bad = validate_component_data_references(
        app,
        [
            {
                "id": "root",
                "component": "clio.map.v1",
                "dataUri": uri,
                "latitudeField": "lat",
                "longitudeField": "lon",
                "labelField": "label",
                "selectionField": "not_a_real_column",
            }
        ],
    )
    assert bad is not None
    assert bad["reason"] == "a2ui_field_not_in_dataset"
    assert "selectionField='not_a_real_column'" in bad["detail"]


def test_data_table_selection_field_is_validated_against_real_columns(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from clio_agent.gact.a2ui_producer._data_reference import validate_component_data_references

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    ok = validate_component_data_references(
        app,
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "selectionField": "entity",
            }
        ],
    )
    assert ok is None

    bad = validate_component_data_references(
        app,
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "selectionField": "nope",
            }
        ],
    )
    assert bad is not None
    assert bad["reason"] == "a2ui_field_not_in_dataset"


# --------------------------------------------------------------------------- #
# dataQuery: filter/aggregate columns are SOURCE columns; *Field/sort/columns
# are OUTPUT columns (source unchanged, or the aggregate's own <col>_<fn>
# names when dataQuery.aggregate is set) -- #1533 S4 adversarial review item 2.
# --------------------------------------------------------------------------- #


def test_data_query_filter_column_against_a_bogus_source_column_is_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "dataQuery": {"filter": [{"column": "not_a_column", "op": "eq", "value": "x"}]},
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "dataQuery.filter[].column='not_a_column'" in result["detail"]
    assert "source column" in result["detail"]


def test_data_query_aggregate_groupby_against_a_bogus_source_column_is_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "dataQuery": {
                    "aggregate": {
                        "groupBy": ["not_a_column"],
                        "metrics": [{"column": "lat", "fn": "mean"}],
                    },
                    "columns": ["not_a_column", "lat_mean"],
                },
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "dataQuery.aggregate.groupBy='not_a_column'" in result["detail"]


def test_data_query_aggregate_metric_column_against_a_bogus_source_column_is_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "dataQuery": {
                    "aggregate": {
                        "groupBy": ["entity"],
                        "metrics": [{"column": "not_a_column", "fn": "mean"}],
                    },
                    "columns": ["entity", "not_a_column_mean"],
                },
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "dataQuery.aggregate.metrics[].column='not_a_column'" in result["detail"]


def test_data_query_sort_column_after_aggregate_must_be_an_output_column(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The reviewer's exact probe, mirrored at the producer boundary: after an
    aggregate, ``sort`` (which runs on the AGGREGATED table) must name an
    output column (``lat_mean``), never the source column it was built from
    (``lat``)."""

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "dataQuery": {
                    "aggregate": {
                        "groupBy": ["entity"],
                        "metrics": [{"column": "lat", "fn": "mean"}],
                    },
                    "columns": ["entity", "lat_mean"],
                    "sort": [{"column": "lat", "desc": True}],
                },
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "dataQuery.sort[].column='lat'" in result["detail"]
    assert "aggregate output" in result["detail"]


def test_data_query_columns_after_aggregate_must_be_output_columns(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "dataQuery": {
                    "aggregate": {
                        "groupBy": ["entity"],
                        "metrics": [{"column": "lat", "fn": "mean"}],
                    },
                    "columns": ["lat"],  # source name, not the output "lat_mean"
                },
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "dataQuery.columns[]='lat'" in result["detail"]
    assert "aggregate output" in result["detail"]


def test_map_field_after_aggregate_validates_against_aggregate_output(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.map.v1",
                "dataUri": uri,
                "dataQuery": {
                    "aggregate": {
                        "groupBy": ["entity"],
                        "metrics": [
                            {"column": "lat", "fn": "mean"},
                            {"column": "lon", "fn": "mean"},
                        ],
                    },
                    "columns": ["entity", "lat_mean", "lon_mean"],
                },
                "latitudeField": "lat_mean",
                "longitudeField": "lon_mean",
                "labelField": "entity",
            }
        ]
    )

    assert result.get("ok") is not False, result


def test_map_field_after_aggregate_naming_the_source_column_is_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.map.v1",
                "dataUri": uri,
                "dataQuery": {
                    "aggregate": {
                        "groupBy": ["entity"],
                        "metrics": [
                            {"column": "lat", "fn": "mean"},
                            {"column": "lon", "fn": "mean"},
                        ],
                    },
                    "columns": ["entity", "lat_mean", "lon_mean"],
                },
                "latitudeField": "lat",  # source name, not the aggregate output "lat_mean"
                "longitudeField": "lon_mean",
                "labelField": "entity",
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "latitudeField='lat'" in result["detail"]
    assert "aggregate output" in result["detail"]


def test_data_query_sort_and_columns_without_aggregate_validate_against_source(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    uri = _export_data_uri(app, sid, root, "rows.csv", CSV_ROWS)

    ok = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "dataQuery": {"sort": [{"column": "lat", "desc": True}]},
            }
        ]
    )
    assert ok.get("ok") is not False, ok

    bad = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": uri,
                "dataQuery": {"sort": [{"column": "not_a_column"}]},
            }
        ]
    )
    assert bad["ok"] is False
    assert bad["reason"] == "a2ui_field_not_in_dataset"
    assert "dataQuery.sort[].column='not_a_column'" in bad["detail"]
    assert "rows.csv" in bad["detail"]


# --------------------------------------------------------------------------- #
# Bounded streaming reads (#1533 S4 adversarial review item 6): workflow/text
# shape checks read in fixed-size chunks via an incremental UTF-8 decoder, so
# a multi-byte character split across a chunk boundary still decodes
# correctly -- proving this is NOT a naive per-chunk decode (which would
# incorrectly reject valid UTF-8 split across chunks).
# --------------------------------------------------------------------------- #


def test_text_component_data_uri_with_a_multibyte_char_at_the_chunk_boundary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from clio_agent.gact.a2ui_producer._data_reference import _STREAM_CHUNK_BYTES

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    # A 4-byte UTF-8 character (U+1D11E, MUSICAL SYMBOL G CLEF) positioned so
    # its bytes straddle the chunk boundary exactly: 2 bytes land in the
    # first chunk, 2 bytes in the second.
    content = ("a" * (_STREAM_CHUNK_BYTES - 2)) + "\U0001d11e" + ("b" * 100)
    uri = _export_data_uri(app, sid, root, "big_script.py", content)

    result = _create(
        [{"id": "root", "component": "clio.code.v1", "dataUri": uri, "language": "python"}]
    )

    assert result.get("ok") is not False, result


def test_workflow_data_uri_with_a_multibyte_char_at_the_chunk_boundary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from clio_agent.gact.a2ui_producer._data_reference import _STREAM_CHUNK_BYTES

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    padding = "a" * (_STREAM_CHUNK_BYTES - 2 - len('{"nodes": [{"id": "a", "label": "'))
    label = padding + "\U0001d11e" + "tail"
    workflow = json.dumps({"nodes": [{"id": "a", "label": label}], "edges": []})
    uri = _export_data_uri(app, sid, root, "big_workflow.json", workflow)

    result = _create([{"id": "root", "component": "clio.workflow.v1", "dataUri": uri}])

    assert result.get("ok") is not False, result


# --------------------------------------------------------------------------- #
# S8: every new reason ships an actionable hint (completeness guard)
# --------------------------------------------------------------------------- #


def test_new_data_reference_reasons_have_default_hints() -> None:
    from clio_agent.gact.a2ui_producer._refusal import _DEFAULT_HINTS, KNOWN_REFUSAL_REASONS

    for reason in (
        "a2ui_data_reference_not_found",
        "a2ui_data_reference_unreadable",
        "a2ui_data_reference_unsupported_format",
        "a2ui_field_not_in_dataset",
        "a2ui_data_reference_shape_invalid",
    ):
        assert reason in KNOWN_REFUSAL_REASONS
        assert _DEFAULT_HINTS.get(reason)
