"""Unit tests for the cloud-provider "no restriction" deployment defaults.

Model-capabilities brief Part 4.2 / Part 6 (cloud section): a cloud endpoint
has no vision projector, no template, nothing at that layer that could narrow
a model's own capabilities, so those facts are recorded as
``no_restriction(...)`` (``True``, ``source="dialect"``) rather than left
unknown -- distinct from an unprobed self-hosted server, which really IS
unknown.
"""

from __future__ import annotations

from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.capabilities.dialects import cloud
from clio_agent.providers.capabilities.records import Fact, ModelCapabilities


def test_build_deployment_capabilities_reports_no_restriction_not_unknown() -> None:
    deployment = cloud.build_deployment_capabilities("openai", "https://api.openai.com/v1", "gpt-5")

    assert deployment.modalities_enabled.value == frozenset({"text", "image", "pdf", "audio", "video"})
    assert deployment.modalities_enabled.source == "dialect"
    assert deployment.tools_enabled.value is True
    assert deployment.tools_enabled.source == "dialect"


def test_no_restriction_deployment_never_widens_the_models_own_modalities() -> None:
    """A cloud deployment's "every modality" must not ADD a modality the model lacks."""
    model = ModelCapabilities(
        model_key="gpt-5",
        input_modalities=Fact(frozenset({"text"}), "models.dev", "2026-01-01T00:00:00+00:00"),
    )
    deployment = cloud.build_deployment_capabilities("openai", "https://api.openai.com/v1", "gpt-5")

    effective = combine_capabilities(model, None, deployment)

    assert effective.input_modalities.value == frozenset({"text"})


def test_numeric_limits_stay_unknown_not_guessed() -> None:
    deployment = cloud.build_deployment_capabilities("openai", "https://api.openai.com/v1", "gpt-5")

    assert not deployment.context_served.known
    assert not deployment.output_max.known
    assert deployment.fingerprint == ""


def test_cloud_dialects_covers_the_brief_list() -> None:
    assert cloud.CLOUD_DIALECTS == {
        "nvidia_nim",
        "azure",
        "bedrock",
        "vertex_ai",
        "gemini",
        "openai",
        "anthropic",
    }
