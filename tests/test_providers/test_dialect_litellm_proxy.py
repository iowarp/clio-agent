"""Contract tests for the LiteLLM proxy dialect adapter, against RECORDED responses.

Fixtures: ``tests/fixtures/capabilities/litellm_proxy/`` (model-capabilities
brief Part 6, LiteLLM proxy section). ``v1_model_info.json`` gives one alias
(``team-gpt``) TWO underlying deployments with disagreeing limits and
``supports_parallel_function_calling`` -- the multi-deployment narrowing case
the brief calls out ("keep only the capabilities all of them share and the
smallest limits") -- and one alias (``team-vision``) with a single deployment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clio_agent.providers.capabilities.dialects import litellm_proxy

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "capabilities" / "litellm_proxy"


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_list_aliases_reads_v1_models() -> None:
    payload = _load("v1_models.json")

    assert litellm_proxy.list_aliases(payload) == ["team-gpt", "team-vision"]


def test_multi_deployment_alias_keeps_smallest_limits() -> None:
    payload = _load("v1_model_info.json")

    _model, deployment = litellm_proxy.parse_model_info(
        payload, "team-gpt", provider_id="litellm_proxy", api_base="https://proxy.internal/v1"
    )

    # deployment-a has 128000/16384, deployment-b has 64000/4096 -- the alias
    # can only promise the smaller of the two.
    assert deployment.context_served.value == 64000
    assert deployment.output_max.value == 4096
    assert deployment.fingerprint == "litellm_proxy:alias=team-gpt:deployments=2"


def test_multi_deployment_alias_keeps_only_unanimous_capabilities() -> None:
    payload = _load("v1_model_info.json")

    model, _deployment = litellm_proxy.parse_model_info(
        payload, "team-gpt", provider_id="litellm_proxy", api_base="https://proxy.internal/v1"
    )

    # both deployments support function calling and response schema...
    assert model.tools.value is True
    assert model.structured_output.value is True
    # ...but only deployment-a supports parallel function calling, so the
    # alias as a whole must NOT claim it.
    assert model.parallel_tool_calls.value is False


def test_single_deployment_alias_reports_its_own_limits_directly() -> None:
    payload = _load("v1_model_info.json")

    model, deployment = litellm_proxy.parse_model_info(
        payload, "team-vision", provider_id="litellm_proxy", api_base="https://proxy.internal/v1"
    )

    assert deployment.context_served.value == 128000
    assert deployment.output_max.value == 16384
    assert model.parallel_tool_calls.value is True
    assert deployment.fingerprint == "litellm_proxy:alias=team-vision:deployments=1"


def test_unknown_alias_yields_unknown_facts_not_a_crash() -> None:
    payload = _load("v1_model_info.json")

    model, deployment = litellm_proxy.parse_model_info(
        payload, "no-such-alias", provider_id="litellm_proxy", api_base="https://proxy.internal/v1"
    )

    assert not model.tools.known
    assert not deployment.context_served.known
    assert deployment.fingerprint == ""


def test_build_endpoint_capabilities_is_multi_model() -> None:
    endpoint = litellm_proxy.build_endpoint_capabilities(
        "litellm_proxy", "https://proxy.internal/v1", "team-gpt"
    )

    assert endpoint.dialect == "litellm_proxy"
    assert endpoint.multi_model is True
