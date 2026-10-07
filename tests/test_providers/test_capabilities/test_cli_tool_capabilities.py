"""Tool filtering reflects shipped integration contracts and preserves native facts."""

from dataclasses import replace
from typing import Literal

from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.capabilities.endpoint import build_endpoint_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
)
from clio_agent.providers.capabilities.tags import capability_tags


def test_cli_tool_tags_preserve_the_native_and_text_transport_distinction() -> None:
    for dialect, prefix, mode in (
        ("codex", "codex_direct", "native"),
        ("claude_code", "claude_code", "text"),
    ):
        endpoint = build_endpoint_capabilities(
            "custom-preset", "local", dialect, "account-listed-model", custom_llm_provider=prefix
        )
        assert endpoint.tool_calling_mode.value == mode
        assert endpoint.tool_calling_mode.source == "dialect"
        # The fact belongs to the integration, with no model-name heuristic.
        model = ModelCapabilities(model_key="account-listed-model")
        effective = combine_capabilities(model, endpoint, None)
        assert effective.tool_use.value is True
        assert effective.tools.value is (mode == "native")
        assert not model.tools.known
        tags = capability_tags(effective, model_key=model.model_key).capabilities
        assert [tag.value for tag in tags] == ["tool_calling"]
        assert tags[0].evidence[0].source == "dialect"
        assert "typed tool calls" in tags[0].evidence[0].detail
        assert not effective.parallel_tool_calls.known


def test_tool_modes_survive_the_endpoint_cache_and_unknown_transports_stay_unknown() -> None:
    modes: tuple[Literal["native", "text"], ...] = ("native", "text")
    for mode in modes:
        endpoint = EndpointCapabilities(
            provider_id="test-tool-modes",
            api_base="b",
            tool_calling_mode=Fact(mode, "dialect", "now", "transport contract"),
        )
        invalidation.record_endpoint_capabilities(endpoint)
        assert (
            invalidation.get_endpoint_capabilities((endpoint.provider_id, endpoint.api_base))
            == endpoint
        )
    invalidation.invalidate_endpoint(("test-tool-modes", "b"))
    old = EndpointCapabilities(provider_id="other-provider", api_base="b")
    assert not old.tool_calling_mode.known


def test_generic_endpoint_parameters_do_not_imply_model_tool_support() -> None:
    endpoint = EndpointCapabilities(
        provider_id="generic",
        api_base="b",
        accepted_params=Fact(frozenset({"tools", "tool_choice"}), "litellm", "now", "API fields"),
    )
    model = ModelCapabilities(model_key="unknown-model")
    effective = combine_capabilities(model, endpoint, None)
    assert not effective.tools.known
    assert not effective.tool_use.known
    assert capability_tags(effective, model_key=model.model_key).capabilities == []
    reported = combine_capabilities(
        replace(model, tools=Fact(True, "server_report", "now")), endpoint, None
    )
    assert reported.tool_use.value is True
    assert [
        tag.value for tag in capability_tags(reported, model_key=model.model_key).capabilities
    ] == ["tool_calling"]


def test_tool_disabling_is_respected_and_text_adapter_is_not_native_support() -> None:
    model = ModelCapabilities(model_key="m", tools=Fact(False, "server_report", "now"))
    for dialect, prefix in (("codex", "codex_direct"), ("claude_code", "claude_code")):
        endpoint = build_endpoint_capabilities("p", "b", dialect, "m", custom_llm_provider=prefix)
        effective = combine_capabilities(model, endpoint, None)
        assert effective.tools.value is False
        assert effective.tool_use.value is (dialect == "claude_code")
        disabled = DeploymentCapabilities(
            provider_id="p",
            api_base="b",
            model_id="m",
            tools_enabled=Fact(False, "server_report", "now"),
        )
        blocked = combine_capabilities(model, endpoint, disabled)
        assert blocked.tools.value is False
        assert blocked.tool_use.value is False
        assert capability_tags(blocked, model_key="m").capabilities == []
