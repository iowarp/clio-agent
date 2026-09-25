"""Contract tests for the OpenRouter dialect adapter, against a RECORDED response.

Fixture: ``tests/fixtures/capabilities/openrouter/api_v1_models.json`` (shaped
on OpenRouter's public, documented ``/api/v1/models`` schema -- model-capabilities
brief Part 6). Covers ``context_length``/``architecture.input_modalities`` (model),
``top_provider.*`` (deployment), and ``supported_parameters`` -> ``route_params``
directly, with no dialect table involved (the brief is explicit that OpenRouter
gives ``route_params`` for free, unlike llama.cpp/vLLM).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clio_agent.providers.capabilities.dialects import openrouter

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "capabilities" / "openrouter"


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _row(model_id: str) -> dict[str, Any]:
    payload = _load("api_v1_models.json")
    return next(r for r in payload["data"] if r["id"] == model_id)


def test_parse_model_row_reads_model_and_deployment_fields() -> None:
    model, deployment = openrouter.parse_model_row(
        _row("qwen/qwen3-235b-a22b"),
        provider_id="openrouter",
        api_base="https://openrouter.ai/api/v1",
    )

    assert model.context_max.value == 131072
    assert model.context_max.source == "openrouter"
    assert model.input_modalities.value == frozenset({"text"})
    assert model.model_type.value == "chat"  # architecture.output_modalities = ["text"]
    assert model.model_type.source == "openrouter"

    assert deployment.context_served.value == 40960  # top_provider, smaller than context_length
    assert deployment.output_max.value == 8192
    assert deployment.route_params.value == frozenset(
        {"temperature", "top_p", "top_k", "reasoning", "tools", "tool_choice"}
    )


def test_parse_model_row_vision_model_reports_image_modality() -> None:
    model, _deployment = openrouter.parse_model_row(
        _row("openai/gpt-4o-mini"),
        provider_id="openrouter",
        api_base="https://openrouter.ai/api/v1",
    )

    assert model.input_modalities.value == frozenset({"text", "image"})


def test_route_params_fingerprint_changes_with_supported_parameters() -> None:
    _model_a, dep_a = openrouter.parse_model_row(
        _row("qwen/qwen3-235b-a22b"),
        provider_id="openrouter",
        api_base="https://openrouter.ai/api/v1",
    )
    _model_b, dep_b = openrouter.parse_model_row(
        _row("openai/gpt-4o-mini"),
        provider_id="openrouter",
        api_base="https://openrouter.ai/api/v1",
    )

    assert dep_a.fingerprint != dep_b.fingerprint
    assert dep_a.fingerprint.startswith("openrouter:route_params=")


def test_parse_model_row_missing_fields_are_unknown() -> None:
    model, deployment = openrouter.parse_model_row(
        {"id": "bare/model"}, provider_id="openrouter", api_base="https://openrouter.ai/api/v1"
    )

    assert not model.context_max.known
    assert not model.input_modalities.known
    assert not model.model_type.known
    assert not deployment.context_served.known
    assert not deployment.route_params.known
    assert deployment.fingerprint == ""


def test_require_parameters_flag_shape() -> None:
    assert openrouter.REQUIRE_PARAMETERS_FLAG == {"provider": {"require_parameters": True}}


def test_output_modalities_without_text_name_a_generation_type() -> None:
    assert openrouter.model_type_from_output_modalities(["image"]) == "image_generation"
    assert openrouter.model_type_from_output_modalities(["audio"]) == "audio_speech"
    assert openrouter.model_type_from_output_modalities(["image", "text"]) == "chat"
    assert openrouter.model_type_from_output_modalities(None) is None
