"""Workstream A — typed signature outputs (the DSPy-native mechanism).

Proves a marketplace expert can declare *typed* signature outputs
(``list[float]``, ``Literal[...]``, ``optional[...]``, nested ``object`` -> a
Pydantic model) and that they become real typed DSPy ``OutputField``s — the
foundation for deleting the regex ``_infer_*`` backfill (case contamination) in
later increments.
"""

from __future__ import annotations

import typing

import pytest

from clio_agent.gact.app import _blueprint_runtime_signature, _parse_field_annotation
from clio_agent.gact.expert_packs import _coerce_signature_fields


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
