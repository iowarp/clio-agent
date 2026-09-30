"""Generated one-line component signatures for the catalog skill index (#1533).

The catalog index (``a2ui-catalog-<slug>``'s generated ``SKILL.md`` body) must
list every component on one line: its ``description`` then a compact
signature GENERATED from the schema (required vs optional properties,
inline-value-or-``dataUri`` alternatives) -- never hand-written, so it can
never drift from what the validator actually enforces.
"""

from __future__ import annotations

import json
from pathlib import Path

import clio_schemas

from clio_agent.gact.a2ui_catalogs.component_signature import generate_component_signature


def _workspace_catalog_components() -> dict[str, object]:
    path = (
        Path(clio_schemas.__file__).parent
        / "schemas"
        / "a2ui"
        / "catalogs"
        / "clio-workspace"
        / "v1"
        / "catalog.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))["components"]


# --------------------------------------------------------------------------- #
# Simple scalar components: required, then optional
# --------------------------------------------------------------------------- #


def test_simple_component_lists_required_then_optional() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["clio.metric.v1"])
    assert signature == "label, value; detail?, trend?, unit?"


def test_simple_component_with_no_optional_properties() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["Icon"])
    assert signature == "name"


# --------------------------------------------------------------------------- #
# Data-by-reference components: "inline | dataUri" alternatives
# --------------------------------------------------------------------------- #


def test_map_signature_shows_points_or_datauri_alternative() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["clio.map.v1"])

    assert signature.startswith("points[] | dataUri + latitudeField/longitudeField/labelField ")
    # The four OPTIONAL dataset-column properties are grouped, never hidden.
    assert "categoryField" in signature
    assert "detailField" in signature
    assert "idField" in signature
    assert "selectionField" in signature
    # dataQuery is optional and dependent on dataUri -- its own bracket group.
    assert "[dataQuery]" in signature
    # title (and the other plain optional properties) trail with '?'.
    assert "title?" in signature
    # selectionField is required-when-bound (a properties-value conditional,
    # not the plain oneOf/if-required pattern) -- pointed at, not spelled out.
    assert "see schema for conditional field requirements" in signature


def test_data_table_signature_shows_columns_required_only_with_rows() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["clio.data-table.v1"])

    assert signature == (
        "rows[] + columns | dataUri [selectionField] [dataQuery]; "
        "action?, selection?, see schema for conditional field requirements"
    )


def test_workflow_signature_shows_nodes_and_edges_together_or_datauri() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["clio.workflow.v1"])

    assert signature == "nodes[]+edges[] | dataUri; action?, selected?"


def test_code_signature_keeps_language_required_regardless_of_source() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["clio.code.v1"])

    assert signature == "language, code | dataUri; title?"


def test_mermaid_signature_shows_source_or_datauri() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["clio.mermaid.v1"])

    assert signature == "source | dataUri; title?"


def test_chart_signature_names_field_properties_and_flags_preset_specifics() -> None:
    components = _workspace_catalog_components()
    signature = generate_component_signature(components["clio.chart.v1"])

    assert "spec | preset" in signature
    assert "data[] | dataUri" in signature
    for field in ("colorField", "entityField", "facetField", "selectionField", "xField", "yField"):
        assert field in signature
    assert "[dataQuery]" in signature
    assert "see schema for conditional field requirements" in signature


# --------------------------------------------------------------------------- #
# Generated, never hand-written: every real catalog component renders cleanly
# --------------------------------------------------------------------------- #


def test_every_workspace_catalog_component_gets_a_nonempty_signature() -> None:
    components = _workspace_catalog_components()
    for name, schema in components.items():
        signature = generate_component_signature(schema)
        assert signature, f"{name} produced an empty signature"
        assert "\n" not in signature, f"{name} signature is not one line: {signature!r}"


def test_signature_is_empty_for_a_non_schema_input() -> None:
    assert generate_component_signature(None) == ""
    assert generate_component_signature("not a schema") == ""


# --------------------------------------------------------------------------- #
# The generated catalog skill index actually carries the signature
# --------------------------------------------------------------------------- #


def test_catalog_skill_index_line_carries_description_then_signature() -> None:
    from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs
    from clio_agent.gact.a2ui_catalogs.skills import generate_catalog_skill_body

    _basic, workspace = load_builtin_catalogs()
    body = generate_catalog_skill_body(workspace)

    assert "- `clio.map.v1` — Points on an interactive map" in body
    assert "`points[] | dataUri + latitudeField/longitudeField/labelField" in body
