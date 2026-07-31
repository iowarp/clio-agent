"""Cross-repository schema equality for iowarp/clio-agent#1121 (P2.3).

The moved runtime records are the same class objects exported by the installed
``clio-schemas`` package after #1120. Exact resource equality is still valuable: it
catches an in-repo shadow/monkeypatch and a package-pin/resource drift before either
can silently alter the wire contract.

The invoker records are not package-owned yet, so their dataclass and projected
field inventories are compared with a generated committed expectation. Regenerate
that file only through::

    uv run --no-sync --no-cache python scripts/generate_schema_equality_expectations.py
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Callable

import pytest
from clio_schemas import EdgeEvidence as PackageEdgeEvidence
from clio_schemas.export import read_committed
from pydantic import TypeAdapter

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.transform_types import EdgeEvidence, ProvEdge
from clio_agent.gact.artifacts.transforms import TransformRecord
from scripts.generate_schema_equality_expectations import (
    EXPECTATION_PATH,
    build_expectations,
)

ModelSchema = Callable[[], dict[str, Any]]


def _model_schema(model: type[Any]) -> dict[str, Any]:
    return model.model_json_schema()


RUNTIME_SCHEMAS: dict[str, tuple[ModelSchema, str, tuple[str, ...]]] = {
    "ArtifactRecord": (lambda: _model_schema(ArtifactRecord), "artifact_record.json", ()),
    "ArtifactVersion": (lambda: _model_schema(ArtifactVersion), "artifact_version.json", ()),
    "EdgeEvidence": (
        lambda: TypeAdapter(EdgeEvidence).json_schema(),
        "prov_edge.json",
        ("$defs", "EdgeEvidence"),
    ),
    "ProvEdge": (lambda: _model_schema(ProvEdge), "prov_edge.json", ()),
    "TransformRecord": (lambda: _model_schema(TransformRecord), "transform_record.json", ()),
}


def _committed_resource_schema(filename: str, nested_path: tuple[str, ...]) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(read_committed()[filename])
    for key in nested_path:
        schema = schema[key]
    return schema


def _assert_runtime_schema_matches_export(model_name: str) -> None:
    runtime_schema, filename, nested_path = RUNTIME_SCHEMAS[model_name]
    expected = _committed_resource_schema(filename, nested_path)
    actual = runtime_schema()
    assert actual == expected, (
        f"{model_name} diverged from installed clio-schemas resource {filename}; "
        "update the shared convergence contract and package pin together"
    )


@pytest.fixture(autouse=True)
def _optional_acceptance_sabotage(monkeypatch: pytest.MonkeyPatch) -> None:
    """One-field runtime monkeypatch used only for the documented red-suite proof."""

    if os.environ.get("CLIO_SCHEMA_EQUALITY_SABOTAGE") != "ArtifactVersion":
        return
    original = ArtifactVersion.model_json_schema

    def sabotaged_schema(cls: type[Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        del cls
        schema = deepcopy(original(*args, **kwargs))
        schema["properties"]["sabotage_field"] = {"type": "string"}
        return schema

    monkeypatch.setattr(ArtifactVersion, "model_json_schema", classmethod(sabotaged_schema))


@pytest.mark.parametrize("model_name", sorted(RUNTIME_SCHEMAS))
def test_runtime_model_schema_equals_installed_export(model_name: str) -> None:
    """Every moved model exactly equals its installed immutable package resource."""

    _assert_runtime_schema_matches_export(model_name)


def test_edge_evidence_is_the_package_enum() -> None:
    """The runtime enum is the installed package export, not a local duplicate."""

    assert EdgeEvidence is PackageEdgeEvidence


def test_invoker_wire_shapes_match_generated_expectation() -> None:
    """Invoker field drift in either direction must revise the convergence contract."""

    committed = json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))
    actual = build_expectations()
    assert actual == committed, (
        "invoker wire shape or RELAY_STATE_MAP drifted; revise the cross-repo "
        "convergence contract, then run the documented expectation generator"
    )


def test_task_spec_expectation_includes_1122_binding_fields() -> None:
    """The detached-executor bindings from #1122 are committed wire fields."""

    committed = json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))
    fields = set(committed["invoker_types"]["TaskSpec"]["wire_fields"])
    assert {"workspace_id", "session_mode", "session_scope_metadata"} <= fields


def test_shadowed_runtime_model_schema_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-field local shadow is detected even though the class is package-owned."""

    original = ArtifactVersion.model_json_schema

    def shadowed_schema(cls: type[Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        del cls
        schema = deepcopy(original(*args, **kwargs))
        schema["properties"]["shadow_field"] = {"type": "string"}
        return schema

    monkeypatch.setattr(ArtifactVersion, "model_json_schema", classmethod(shadowed_schema))
    with pytest.raises(AssertionError, match="convergence contract"):
        _assert_runtime_schema_matches_export("ArtifactVersion")
