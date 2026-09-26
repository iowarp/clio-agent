"""The capability-record JSON codec round-trips every fact type exactly."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

from clio_agent.providers.capabilities.model_facts import (
    ParameterCount,
    Price,
    ReleaseDate,
    TokenPricing,
)
from clio_agent.providers.capabilities.record_codec import decode_record, encode_record
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
)


def _fact(value: Any, source: Any = "openrouter") -> Fact[Any]:
    return Fact(value, source, "2026-09-26T00:00:00+00:00", f"detail of {value!r}")


def test_a_model_record_with_every_fact_type_round_trips() -> None:
    model = ModelCapabilities(
        model_key="qwen/qwen3.6-35b-a3b",
        task=_fact("text-generation"),
        context_max=_fact(262_144),
        input_modalities=_fact(frozenset({"text", "image"})),
        tools=_fact(True),
        thinking=_fact(
            ThinkingSpec(
                mechanism="effort_levels",
                levels=("low", "high"),
                effort_by_level={"low": "low", "high": "high"},
                budget_range=(128, 4096),
                template_kwarg="enable_thinking",
            )
        ),
        sampling_thinking=_fact({"temperature": 0.6, "top_k": 20.0}, "hf_repo"),
        description=_fact("A [router](https://x.y)"),
        released_at=_fact(ReleaseDate("2026-04", "month"), "models.dev"),
        parameters=_fact(ParameterCount(total=35, experts_total=256, experts_active=8), "hf_repo"),
        catalog_pricing=_fact(
            TokenPricing(Price("usd", Decimal("0.15")), Price("variable")), "litellm"
        ),
        hf_repo=_fact("Qwen/Qwen3.6-35B-A3B"),
    )
    encoded = json.loads(json.dumps(encode_record(model)))  # JSON-safe
    assert "output_max" not in encoded  # unknown facts are not written
    assert decode_record(ModelCapabilities, encoded) == model


def test_a_deployment_record_round_trips_and_takes_the_current_endpoint() -> None:
    deployment = DeploymentCapabilities(
        provider_id="openrouter",
        api_base="https://old.example/api/v1",
        model_id="openrouter/auto",
        model_key=_fact("openrouter/auto", "server_report"),
        route_params=_fact(frozenset({"tools"}), "server_report"),
        template_caps=_fact({"supports_tools": True, "levels": ["a"]}, "server_report"),
        pricing=_fact(TokenPricing(Price("subscription"), Price("subscription")), "dialect"),
        free=_fact(False, "server_report"),
        router=_fact(True, "server_report"),
        fingerprint="openrouter:route_params=['tools']",
    )
    decoded = decode_record(
        DeploymentCapabilities,
        json.loads(json.dumps(encode_record(deployment))),
        api_base="https://openrouter.ai/api/v1",
    )
    assert decoded == replace(deployment, api_base="https://openrouter.ai/api/v1")


def test_an_undecodable_fact_becomes_unknown_not_a_guess() -> None:
    encoded = encode_record(ModelCapabilities(model_key="m", context_max=_fact(10)))
    encoded["context_max"]["value"] = "ten"
    encoded["tools"] = {"value": True, "source": "overlay"}
    decoded = decode_record(ModelCapabilities, encoded)
    assert decoded is not None
    assert not decoded.context_max.known
    assert decoded.tools.value is True
    # A record without its identity is unusable, not half-built.
    assert decode_record(ModelCapabilities, {"tools": {"value": True, "source": "x"}}) is None
