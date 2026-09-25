"""Unit tests for :mod:`clio_agent.providers.capabilities.combine` (brief 5.5).

Covers the three-valued AND, minimum-limit combination, parameter intersection,
thinking-control selection, and that every effective value records which
record(s) decided it ("decided by").
"""

from __future__ import annotations

from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
)

_NOW = "2026-01-01T00:00:00+00:00"


def _fact(value: object, source: str = "server_report", observed_at: str = _NOW) -> Fact:
    return Fact(value=value, source=source, observed_at=observed_at)


def _model(**kwargs: object) -> ModelCapabilities:
    return ModelCapabilities(model_key="m", **kwargs)  # type: ignore[arg-type]


def _endpoint(**kwargs: object) -> EndpointCapabilities:
    return EndpointCapabilities(provider_id="p", api_base="http://x", **kwargs)  # type: ignore[arg-type]


def _deployment(**kwargs: object) -> DeploymentCapabilities:
    return DeploymentCapabilities(
        provider_id="p",
        api_base="http://x",
        model_id="m",
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# combine_capabilities with everything absent
# --------------------------------------------------------------------------- #


def test_all_none_degrades_to_unknown_everywhere() -> None:
    effective = combine_capabilities(None, None, None)
    assert not effective.context.known
    assert not effective.tools.known
    assert effective.thinking.spec is None
    assert effective.model_key is None


# --------------------------------------------------------------------------- #
# Three-valued AND (tools)
# --------------------------------------------------------------------------- #


def test_tri_and_both_true_is_true() -> None:
    model = _model(tools=_fact(True))
    deployment = _deployment(tools_enabled=_fact(True))
    effective = combine_capabilities(model, None, deployment)
    assert effective.tools.value is True
    assert effective.tools.decided_by == "model+deployment"


def test_tri_and_any_false_wins() -> None:
    model = _model(tools=_fact(True))
    deployment = _deployment(tools_enabled=_fact(False))
    effective = combine_capabilities(model, None, deployment)
    assert effective.tools.value is False


def test_tri_and_unknown_deployment_defers_to_known_model() -> None:
    model = _model(tools=_fact(True))
    deployment = _deployment()  # tools_enabled unknown
    effective = combine_capabilities(model, None, deployment)
    assert effective.tools.value is True
    assert effective.tools.decided_by == "model"


def test_tri_and_nothing_known_is_unknown() -> None:
    effective = combine_capabilities(_model(), None, _deployment())
    assert not effective.tools.known


def test_tri_and_records_provenance_source_and_observed_at() -> None:
    model = _model(
        tools=_fact(True, source="server_report", observed_at="2026-02-01T00:00:00+00:00")
    )
    effective = combine_capabilities(model, None, None)
    assert effective.tools.source == "server_report"
    assert effective.tools.observed_at == "2026-02-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Minimum-limit combination (context / output)
# --------------------------------------------------------------------------- #


def test_context_is_the_smaller_of_model_and_deployment() -> None:
    model = _model(context_max=_fact(128_000))
    deployment = _deployment(context_served=_fact(32_768))
    effective = combine_capabilities(model, None, deployment)
    assert effective.context.value == 32_768
    assert effective.context.decided_by == "deployment"
    assert "128000" in effective.context.reason or "128_000" in effective.context.reason


def test_context_uses_the_only_known_side_and_flags_the_other() -> None:
    model = _model(context_max=_fact(128_000))
    effective = combine_capabilities(model, None, _deployment())
    assert effective.context.value == 128_000
    assert effective.context.decided_by == "model"


def test_context_tie_credits_both_records() -> None:
    model = _model(context_max=_fact(65_536))
    deployment = _deployment(context_served=_fact(65_536))
    effective = combine_capabilities(model, None, deployment)
    assert effective.context.value == 65_536
    assert effective.context.decided_by == "model+deployment"


def test_output_max_combination_mirrors_context() -> None:
    model = _model(output_max=_fact(8192))
    deployment = _deployment(output_max=_fact(4096))
    effective = combine_capabilities(model, None, deployment)
    assert effective.output_max.value == 4096
    assert effective.output_max.decided_by == "deployment"


# --------------------------------------------------------------------------- #
# Modality intersection
# --------------------------------------------------------------------------- #


def test_modalities_intersect_when_both_known() -> None:
    model = _model(input_modalities=_fact(frozenset({"text", "image", "audio"})))
    deployment = _deployment(modalities_enabled=_fact(frozenset({"text", "image"})))
    effective = combine_capabilities(model, None, deployment)
    assert effective.input_modalities.value == frozenset({"text", "image"})
    assert effective.input_modalities.decided_by == "model+deployment"


def test_modalities_use_the_only_known_side() -> None:
    model = _model(input_modalities=_fact(frozenset({"text", "image"})))
    effective = combine_capabilities(model, None, _deployment())
    assert effective.input_modalities.value == frozenset({"text", "image"})
    assert effective.input_modalities.decided_by == "model"


# --------------------------------------------------------------------------- #
# Parameter intersection minus forbidden
# --------------------------------------------------------------------------- #


def test_accepted_params_intersects_route_params() -> None:
    endpoint = _endpoint(accepted_params=_fact(frozenset({"temperature", "top_p", "top_k"})))
    deployment = _deployment(route_params=_fact(frozenset({"temperature", "top_p"})))
    effective = combine_capabilities(None, endpoint, deployment)
    assert effective.accepted_params.value == frozenset({"temperature", "top_p"})
    assert "endpoint" in effective.accepted_params.decided_by
    assert "deployment" in effective.accepted_params.decided_by


def test_accepted_params_removes_model_forbidden_params() -> None:
    model = _model(forbidden_params=_fact(frozenset({"temperature"})))
    endpoint = _endpoint(accepted_params=_fact(frozenset({"temperature", "top_p"})))
    effective = combine_capabilities(model, endpoint, None)
    assert effective.accepted_params.value == frozenset({"top_p"})
    assert "temperature" in effective.accepted_params.reason


def test_accepted_params_unknown_without_an_endpoint_record() -> None:
    effective = combine_capabilities(None, None, _deployment(route_params=_fact(frozenset({"x"}))))
    assert not effective.accepted_params.known


# --------------------------------------------------------------------------- #
# Thinking control selection
# --------------------------------------------------------------------------- #


def test_thinking_none_mechanism_needs_no_control() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="none")))
    effective = combine_capabilities(model, None, None)
    assert effective.thinking.spec is not None
    assert effective.thinking.control is None
    assert effective.thinking.decided_by == "model"


def test_thinking_always_on_needs_no_control() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="always_on")))
    effective = combine_capabilities(model, None, None)
    assert effective.thinking.control is None
    assert "no control needed" in effective.thinking.reason


def test_thinking_effort_levels_chooses_reasoning_effort_first() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="effort_levels")))
    endpoint = _endpoint(
        thinking_controls=_fact(frozenset({"reasoning_effort", "chat_template_kwargs"}))
    )
    effective = combine_capabilities(model, endpoint, None)
    assert effective.thinking.control == "reasoning_effort"


def test_thinking_on_off_prefers_chat_template_kwargs_over_think() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="on_off")))
    endpoint = _endpoint(thinking_controls=_fact(frozenset({"think", "chat_template_kwargs"})))
    effective = combine_capabilities(model, endpoint, None)
    assert effective.thinking.control == "chat_template_kwargs"


def test_thinking_budget_tokens_prefers_thinking_token_budget() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="budget_tokens")))
    endpoint = _endpoint(
        thinking_controls=_fact(frozenset({"chat_template_kwargs", "thinking_token_budget"}))
    )
    effective = combine_capabilities(model, endpoint, None)
    assert effective.thinking.control == "thinking_token_budget"


def test_thinking_not_controllable_here_when_endpoint_has_no_matching_control() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="effort_levels")))
    # "thinking_token_budget" only carries budget_tokens, never effort_levels.
    endpoint = _endpoint(thinking_controls=_fact(frozenset({"thinking_token_budget"})))
    effective = combine_capabilities(model, endpoint, None)
    assert effective.thinking.control is None
    assert "not controllable here" in effective.thinking.reason


def test_thinking_not_controllable_when_endpoint_reports_nothing() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="on_off")))
    effective = combine_capabilities(model, None, None)
    assert effective.thinking.control is None
    assert "not controllable here" in effective.thinking.reason


def test_thinking_deployment_can_deny_a_control_the_endpoint_offers() -> None:
    model = _model(thinking=_fact(ThinkingSpec(mechanism="effort_levels")))
    endpoint = _endpoint(thinking_controls=_fact(frozenset({"reasoning_effort"})))
    deployment = _deployment(template_caps=_fact({"supports_reasoning_effort": False}))
    effective = combine_capabilities(model, endpoint, deployment)
    assert effective.thinking.control is None


def test_thinking_unknown_when_model_thinking_is_unknown() -> None:
    effective = combine_capabilities(_model(), _endpoint(), None)
    assert effective.thinking.spec is None
    assert effective.thinking.decided_by == "unknown"


# --------------------------------------------------------------------------- #
# Recommended sampling narrowed to the effective parameter set
# --------------------------------------------------------------------------- #


def test_sampling_is_narrowed_to_the_effective_parameter_set() -> None:
    model = _model(
        sampling_thinking=_fact({"temperature": 0.6, "top_p": 0.95, "top_k": 20}),
    )
    endpoint = _endpoint(accepted_params=_fact(frozenset({"temperature", "top_p"})))
    effective = combine_capabilities(model, endpoint, None)
    assert effective.sampling_thinking.value == {"temperature": 0.6, "top_p": 0.95}
    assert "top_k" not in effective.sampling_thinking.value


def test_sampling_withheld_when_parameter_set_unknown() -> None:
    model = _model(sampling_thinking=_fact({"temperature": 0.6}))
    effective = combine_capabilities(model, None, None)
    assert not effective.sampling_thinking.known


# --------------------------------------------------------------------------- #
# model_key resolution
# --------------------------------------------------------------------------- #


def test_model_key_prefers_the_deployment_link_when_known() -> None:
    model = ModelCapabilities(model_key="fallback-key")
    deployment = _deployment(model_key=_fact("linked-key"))
    effective = combine_capabilities(model, None, deployment)
    assert effective.model_key == "linked-key"


def test_model_key_falls_back_to_the_model_record_key() -> None:
    model = ModelCapabilities(model_key="model-key")
    effective = combine_capabilities(model, None, _deployment())
    assert effective.model_key == "model-key"
