"""Model-capability tags: effective facts projected onto ``clio_schemas.ModelCapabilityTags``.

Unit coverage for :mod:`clio_agent.providers.capabilities.tags` and the
overlay's explicit ``task``/``domains`` keys. The fixture-driven end-to-end
cases (the recorded OpenRouter listing, the recorded ALCF Sophia fleet) live
beside their handshake harnesses in ``test_openrouter_golden.py`` and
``test_alcf_modalities.py``.
"""

from __future__ import annotations

import logging

import pytest
from clio_schemas import ModelCapabilityTags

from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.capabilities.model_overlay import (
    OverlayEntry,
    entry_to_model_capabilities,
)
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    task_fact,
)
from clio_agent.providers.capabilities.tags import (
    MODEL_TYPE_FOR_TASK,
    capability_tags,
    model_type_for_task,
)

AT = "2026-09-26T00:00:00+00:00"


def _tags(
    model: ModelCapabilities | None, deployment: DeploymentCapabilities | None = None
) -> ModelCapabilityTags:
    effective = combine_capabilities(model, None, deployment)
    return capability_tags(effective, model_key=effective.model_key or "m")


def _values(tags: list) -> list[str]:
    return [tag.value for tag in tags]


def test_nothing_known_yields_no_tags() -> None:
    record = _tags(ModelCapabilities(model_key="allenai/Llama-3.1-Tulu-3-405B"))
    assert record.model_dump(exclude_defaults=True) == {"model_key": "allenai/Llama-3.1-Tulu-3-405B"}


def test_a_classifier_is_a_surrogate_with_its_hub_task_and_evidence() -> None:
    detail = "openrouter architecture.output_modalities=['decisions']"
    record = _tags(
        ModelCapabilities(
            model_key="~typesafe/jev-latest",
            task=Fact("text-classification", "openrouter", AT, detail),
            output_modalities=Fact(frozenset({"decisions"}), "openrouter", AT, "outputs"),
        )
    )
    assert record.model_type is not None and record.model_type.value == "classification"
    assert record.role is not None and record.role.value == "surrogate"
    assert _values(record.tasks) == ["text-classification"]
    assert _values(record.output_modalities) == ["scores"]
    evidence = record.model_type.evidence[0]
    assert (evidence.source, evidence.detail, evidence.observed_at) == ("openrouter", detail, AT)


def test_a_chat_model_is_general() -> None:
    record = _tags(
        ModelCapabilities(model_key="m", task=Fact("image-text-to-text", "hf_repo", AT, "pipeline_tag"))
    )
    assert record.model_type is not None and record.model_type.value == "chat"
    assert record.role is not None and record.role.value == "general"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"text"}, ["text"]),
        ({"image"}, ["image"]),
        ({"embeddings"}, ["embeddings"]),
        ({"transcription"}, ["text"]),
        ({"speech"}, ["audio"]),
        ({"rerank"}, ["scores"]),
        ({"decisions"}, ["scores"]),
        ({"image", "text"}, ["image", "text"]),
        ({"holograms"}, []),
    ],
)
def test_output_modalities_fold_onto_the_shared_vocabulary(raw: set[str], expected: list[str]) -> None:
    record = _tags(
        ModelCapabilities(
            model_key="m", output_modalities=Fact(frozenset(raw), "openrouter", AT, "outputs")
        )
    )
    assert _values(record.output_modalities) == expected


def test_outputs_come_from_the_task_only_when_no_source_states_them() -> None:
    sam3 = _tags(
        ModelCapabilities(
            model_key="sam3",
            task=Fact("mask-generation", "server_report", AT, "framework='sam3service'"),
        )
    )
    assert _values(sam3.output_modalities) == ["masks"]
    assert sam3.output_modalities[0].evidence[0].detail.startswith("task mask-generation produces masks")
    stated = _tags(
        ModelCapabilities(
            model_key="m",
            task=Fact("text-generation", "openrouter", AT, "t"),
            output_modalities=Fact(frozenset({"image", "text"}), "openrouter", AT, "o"),
        )
    )
    assert _values(stated.output_modalities) == ["image", "text"]


def test_capabilities_need_a_true_fact_and_reasoning_a_real_mechanism() -> None:
    record = _tags(
        ModelCapabilities(
            model_key="m",
            tools=Fact(True, "openrouter", AT, "tools"),
            parallel_tool_calls=Fact(False, "litellm", AT, "no"),
            structured_output=Fact(True, "openrouter", AT, "response_format"),
            thinking=Fact(ThinkingSpec(mechanism="effort_levels", levels=("low",)), "openrouter", AT, "r"),
        )
    )
    assert _values(record.capabilities) == ["tool_calling", "structured_output", "reasoning"]
    no_thinking = _tags(
        ModelCapabilities(model_key="m", thinking=Fact(ThinkingSpec(mechanism="none"), "openrouter", AT, "r"))
    )
    assert no_thinking.capabilities == []


def test_agreeing_sources_each_become_evidence() -> None:
    record = _tags(
        ModelCapabilities(model_key="m", tools=Fact(True, "openrouter", AT, "tools")),
        DeploymentCapabilities(
            provider_id="p",
            api_base="b",
            model_id="m",
            tools_enabled=Fact(True, "server_report", AT, "parser"),
        ),
    )
    assert [e.source for e in record.capabilities[0].evidence] == ["openrouter", "server_report"]


def test_free_and_router_flags_carry_the_pricing_evidence() -> None:
    detail = "openrouter pricing prompt='0' completion='0'"
    record = _tags(
        None,
        DeploymentCapabilities(
            provider_id="openrouter",
            api_base="b",
            model_id="openrouter/free",
            free=Fact(True, "server_report", AT, detail),
            router=Fact(True, "server_report", AT, "openrouter model author='openrouter'"),
        ),
    )
    assert record.free is not None and record.free.value is True
    assert record.free.evidence[0].detail == detail
    assert record.router is not None and record.router.value is True


def test_a_value_with_no_attributable_source_is_not_tagged() -> None:
    """A fact that names no source (``unknown``) is no evidence: unknown shows nothing."""

    record = _tags(
        ModelCapabilities(
            model_key="m",
            task=Fact("text-to-image", "unknown", AT, "no source"),
            input_modalities=Fact(frozenset({"text"}), "unknown", AT, ""),
        )
    )
    assert record.model_type is None and record.role is None and record.tasks == []
    assert record.input_modalities == [] and record.output_modalities == []


def test_a_clio_task_is_a_scientific_surrogate() -> None:
    record = _tags(
        ModelCapabilities(
            model_key="microsoft/aurora",
            task=task_fact("clio:weather-emulation", source="overlay", observed_at=AT, detail="o"),
            domains=Fact(frozenset({"climate"}), "overlay", AT, "o"),
        )
    )
    assert record.model_type is not None and record.model_type.value == "scientific_surrogate"
    assert record.role is not None and record.role.value == "surrogate"
    assert _values(record.domains) == ["climate"]


def test_model_types_cover_the_owner_examples() -> None:
    assert model_type_for_task("text-classification") == "classification"
    assert model_type_for_task("text-to-image") == "image_generation"
    assert model_type_for_task("feature-extraction") == "embedding"
    assert model_type_for_task("mask-generation") == "segmentation"
    assert model_type_for_task("automatic-speech-recognition") == "audio_transcription"
    assert model_type_for_task("depth-estimation") == "other"
    assert set(MODEL_TYPE_FOR_TASK.values()) <= {
        "chat", "embedding", "rerank", "audio_transcription", "audio_speech",
        "image_generation", "image_edit", "video_generation", "ocr",
        "segmentation", "classification", "forecasting",
    }  # fmt: skip


# --------------------------------------------------------------------------- overlay keys


def _entry(**capabilities: object) -> OverlayEntry:
    return OverlayEntry(
        family="fam",
        match_patterns=("fam",),
        capabilities={"chat": False, **capabilities},
        quirks={},
        root="user",
    )


def test_overlay_explicit_task_and_domains() -> None:
    facts = entry_to_model_capabilities(
        "fam", _entry(task="time-series-forecasting", domains=["climate", "physics"]), "fam"
    )
    assert facts.task.value == "time-series-forecasting" and facts.task.source == "overlay"
    assert facts.domains.value == frozenset({"climate", "physics"})


def test_overlay_explicit_task_outranks_its_flags() -> None:
    facts = entry_to_model_capabilities("fam", _entry(task="mask-generation", embeddings=True), "fam")
    assert facts.task.value == "mask-generation"


def test_overlay_rejects_unknown_task_and_domain_loudly(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    facts = entry_to_model_capabilities(
        "fam", _entry(task="weather", embeddings=True, domains=["weather", "climate"]), "fam"
    )
    assert facts.task.value == "feature-extraction"  # falls to the flags, never a near match
    assert facts.domains.value == frozenset({"climate"})
    assert "reason=unknown_task" in caplog.text
    assert "reason=unknown_domain" in caplog.text
    none = entry_to_model_capabilities("fam", _entry(domains=["weather"]), "fam")
    assert not none.domains.known


def test_an_endpoint_only_capability_is_not_a_model_tag() -> None:
    """OpenRouter's API accepting ``response_format`` says nothing about one route."""
    from clio_agent.providers.capabilities.records import EndpointCapabilities

    effective = combine_capabilities(
        ModelCapabilities(model_key="typesafe/jev-router"),
        EndpointCapabilities(
            provider_id="openrouter",
            api_base="b",
            structured_output_modes=Fact(frozenset({"json_schema"}), "dialect", AT, "endpoint"),
        ),
        None,
    )
    assert effective.structured_output.value is True  # the request builder may still use it
    assert capability_tags(effective, model_key="typesafe/jev-router").capabilities == []
