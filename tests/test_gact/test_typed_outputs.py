"""Workstream A — typed signature outputs (the DSPy-native mechanism).

Proves a marketplace expert can declare *typed* signature outputs
(``list[float]``, ``Literal[...]``, ``optional[...]``, nested ``object`` -> a
Pydantic model) and that they become real typed DSPy ``OutputField``s — the
foundation for deleting the regex ``_infer_*`` backfill (case contamination) in
later increments.
"""

from __future__ import annotations

import typing
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from clio_agent.gact.agents.builders import _injected_workflow_state_field_type
from clio_agent.gact.app import _blueprint_runtime_signature, _parse_field_annotation
from clio_agent.gact.expert_packs import _coerce_signature_fields
from clio_agent.gact.workflow_state.schema import GENERIC_WORKFLOW_STATE_SCHEMA
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA


def test_parse_field_annotation_dsl():
    assert _parse_field_annotation({"type": "list[float]"}, model_name="t") == list[float]
    assert _parse_field_annotation({"type": "str"}, model_name="t") is str
    assert _parse_field_annotation({"type": "int"}, model_name="t") is int

    lit = _parse_field_annotation({"type": 'Literal["staged","blocked"]'}, model_name="t")
    assert typing.get_origin(lit) is typing.Literal
    assert set(typing.get_args(lit)) == {"staged", "blocked"}

    opt = _parse_field_annotation({"type": "optional[float]"}, model_name="t")
    assert type(None) in typing.get_args(opt)
    assert float in typing.get_args(opt)


def test_parse_nested_object_becomes_pydantic_model():
    model = _parse_field_annotation(
        {
            "type": "object",
            "fields": {
                "status": {"type": 'Literal["staged","missing"]'},
                "analysis_ready": {"type": "bool"},
                "local_path": {"type": "optional[str]"},
            },
        },
        model_name="resolver_acquisition",
    )
    from pydantic import BaseModel

    assert issubclass(model, BaseModel)
    assert set(model.model_fields) == {"status", "analysis_ready", "local_path"}
    # required vs optional inferred from optional[...]
    assert model.model_fields["status"].is_required()
    assert not model.model_fields["local_path"].is_required()


def test_empty_literal_rejected():
    with pytest.raises(ValueError):
        _parse_field_annotation({"type": "Literal[]"}, model_name="t")


def test_coerce_signature_fields_preserves_types():
    # mapping form kept verbatim (carries per-field type)
    mapping = {"region": {"description": "bbox", "type": "list[float]"}}
    assert _coerce_signature_fields(mapping) == mapping
    # list-of-dicts kept (not stringified)
    rows = _coerce_signature_fields([{"name": "region", "type": "list[float]"}, "bare"])
    assert rows[0]["type"] == "list[float]"
    assert rows[1] == "bare"
    # bare CSV -> names
    assert _coerce_signature_fields("a, b, c") == ["a", "b", "c"]


class _Def:
    """Minimal AgentDef stand-in for the signature builder."""

    id = "geography"
    module = {"kind": "predict"}
    signature = {
        "inputs": {"question": {"type": "string"}},
        "outputs": {
            "answer": {"type": "string"},
            "region": {"description": "bbox [minlon,minlat,maxlon,maxlat]", "type": "list[float]"},
            "status": {"type": 'Literal["staged","blocked"]'},
        },
    }
    structured_outputs = {"workflow_state": True}


def test_pack_declared_outputs_are_typed_outputfields():
    sig = _blueprint_runtime_signature(_Def())
    fields = sig.output_fields
    assert fields["region"].annotation == list[float]
    assert typing.get_origin(fields["status"].annotation) is typing.Literal
    assert fields["answer"].annotation is str
    # structured-output defaults still present (this increment does not flip them)
    assert "workflow_state" in fields


# ---------------------------------------------------------------------------
# Consumer A (#648): the auto-injected workflow_state field is TYPED FROM THE
# PACK SCHEMA (Phase C slice C).
# ---------------------------------------------------------------------------


def _section_model(model: type[BaseModel], section: str) -> type[BaseModel]:
    """Unwrap the ``Optional[<SectionModel>]`` annotation of a declared section."""
    annotation = model.model_fields[section].annotation
    inner = next(a for a in typing.get_args(annotation) if a is not type(None))
    assert issubclass(inner, BaseModel)
    return inner


def test_generic_schema_keeps_free_dict_field_type():
    # No declared sections -> the historical free dict, byte-identical.
    assert _injected_workflow_state_field_type(GENERIC_WORKFLOW_STATE_SCHEMA) == dict[str, Any]


def test_generic_schema_injects_free_dict_via_signature():
    # No active app/session -> resolver returns GENERIC -> dict[str, Any] injected.
    sig = _blueprint_runtime_signature(_Def())
    assert sig.output_fields["workflow_state"].annotation == dict[str, Any]


def test_pack_schema_builds_typed_nested_model():
    model = _injected_workflow_state_field_type(EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    assert issubclass(model, BaseModel)
    # extra="allow" at the top level -> undeclared sections tolerated.
    assert model.model_config.get("extra") == "allow"
    assert "acquisition" in model.model_fields
    acquisition = _section_model(model, "acquisition")
    assert acquisition.model_config.get("extra") == "allow"
    # the section's status is a real Optional[Literal[...]] drawn from status_ranks.
    status_annotation = acquisition.model_fields["status"].annotation
    literal = next(a for a in typing.get_args(status_annotation) if a is not type(None))
    assert typing.get_origin(literal) is typing.Literal
    assert set(typing.get_args(literal)) == {"staged", "metadata_only", "blocked", "missing"}
    # the section field itself is optional (defaults to None).
    assert not model.model_fields["acquisition"].is_required()


def test_typed_model_allows_undeclared_key_and_section():
    # The strict-adapter regression guard: extra="allow" at BOTH levels means an
    # undeclared key inside a declared section AND a wholly undeclared section both
    # validate (never hard-fail an otherwise-correct run).
    model = _injected_workflow_state_field_type(EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    obj = model.model_validate(
        {
            "acquisition": {"status": "staged", "metadata_path": "/tmp/x.csv"},
            "station_catalog": {"station_ids": ["P123"], "status": "anything"},
        }
    )
    assert obj.acquisition.status == "staged"
    # undeclared key preserved through the section-level extra="allow".
    assert obj.acquisition.metadata_path == "/tmp/x.csv"
    # undeclared section preserved wholesale through the top-level extra="allow".
    assert obj.station_catalog == {"station_ids": ["P123"], "status": "anything"}


def test_typed_model_rejects_out_of_vocabulary_status():
    # The typing is real: a declared section's status outside its Literal fails.
    model = _injected_workflow_state_field_type(EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    with pytest.raises(ValidationError):
        model.model_validate({"acquisition": {"status": "not_a_real_status"}})


def test_signature_injects_typed_model_when_schema_active(monkeypatch):
    # When the session's active blueprint declares a schema, the injected
    # workflow_state field is the nested typed model (not the free dict).
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._active_workflow_state_schema",
        lambda app, sid: EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )
    sig = _blueprint_runtime_signature(_Def())
    annotation = sig.output_fields["workflow_state"].annotation
    assert annotation != dict[str, Any]
    assert issubclass(annotation, BaseModel)
    assert "acquisition" in annotation.model_fields
