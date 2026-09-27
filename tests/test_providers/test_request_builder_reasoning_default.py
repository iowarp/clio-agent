"""No reasoning-disable reaches the wire unless a person chose "off" AND the
model's own facts say reasoning can be disabled.

Live bug on 0.9.4.19: a turn on OpenRouter's free router (``openrouter/free``,
no settings changed) sent ``reasoning: {"enabled": false}``; the router picked
a model whose reasoning is mandatory and the turn failed with HTTP 400
"Reasoning is mandatory for this endpoint and cannot be disabled". An unset
level was treated as "off", and "off" was sent to models that never stated it
can be disabled.

Records are seeded through :mod:`invalidation` (no network), the router rows
through the real OpenRouter dialect parser on the live ``/models`` rows.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.lm import dialect_wire
from clio_agent.lm.request_builder import build_request_kwargs
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache
from clio_agent.providers.capabilities.combine import ThinkingDecision
from clio_agent.providers.capabilities.dialects import openrouter
from clio_agent.providers.capabilities.endpoint import build_endpoint_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    unknown,
)
from clio_agent.providers.handshake.model import DiscoveredModel

_NOW = "2026-09-27T00:00:00+00:00"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

#: The live ``openrouter/free`` row's capability-bearing fields (2026-09-27).
_FREE_ROUTER_ROW: dict[str, Any] = {
    "id": "openrouter/free",
    "context_length": 200000,
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
    "pricing": {"prompt": "0", "completion": "0"},
    "top_provider": {"context_length": None, "max_completion_tokens": None},
    "supported_parameters": [
        "frequency_penalty",
        "include_reasoning",
        "max_tokens",
        "min_p",
        "presence_penalty",
        "reasoning",
        "reasoning_effort",
        "repetition_penalty",
        "response_format",
        "seed",
        "stop",
        "structured_outputs",
        "temperature",
        "tool_choice",
        "tools",
        "top_k",
        "top_p",
    ],
}
_AUTO_ROUTER_ROW: dict[str, Any] = {
    **_FREE_ROUTER_ROW,
    "id": "openrouter/auto",
    "pricing": {"prompt": "-1", "completion": "-1"},
}


@pytest.fixture(autouse=True)
def _reset_capability_state():
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


def _record_openrouter_row(row: dict[str, Any]) -> None:
    """What a handshake records for one OpenRouter row: endpoint + model + deployment."""
    model, deployment = openrouter.parse_model_row(
        row, provider_id="openrouter", api_base=_OPENROUTER_BASE, observed_at=_NOW
    )
    endpoint = build_endpoint_capabilities(
        "openrouter", _OPENROUTER_BASE, "openrouter", row["id"], custom_llm_provider="openrouter"
    )
    assert endpoint.thinking_controls.value, "the openrouter endpoint must carry a control"
    invalidation.record_endpoint_capabilities(endpoint)
    invalidation.record_model_capabilities(model)
    invalidation.record_deployment_capabilities(deployment)


def _seed(
    provider_id: str,
    api_base: str,
    model_id: str,
    dialect: str,
    *,
    accepted: frozenset[str],
    controls: frozenset[str],
    spec: ThinkingSpec,
    template_caps: dict[str, Any] | None = None,
) -> None:
    model_key = f"test-model:{provider_id}:{model_id}"
    invalidation.record_endpoint_capabilities(
        EndpointCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            dialect=dialect,
            accepted_params=Fact(accepted, "litellm", _NOW),
            thinking_controls=Fact(controls, "dialect", _NOW),
        )
    )
    invalidation.record_model_capabilities(
        ModelCapabilities(model_key=model_key, thinking=Fact(spec, "server_report", _NOW))
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            model_id=model_id,
            model_key=Fact(model_key, "server_report", _NOW),
            template_caps=Fact(template_caps, "server_report", _NOW)
            if template_caps
            else unknown(),
        )
    )


def _cfg(provider_id: str, model: str, **kwargs: object) -> LMProviderConfig:
    return LMProviderConfig(provider_id=provider_id, model=model, **kwargs)  # type: ignore[arg-type]


def _disables(extras: dict[str, Any]) -> list[str]:
    """Every reasoning-DISABLE spelling any dialect could put on the wire."""
    body = extras.get("extra_body") or {}
    found = []
    for where in (extras, body):
        reasoning = where.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("enabled") is False:
            found.append("reasoning.enabled=false")
        if where.get("reasoning_effort") == "none":
            found.append("reasoning_effort=none")
        if where.get("include_reasoning") is False:
            found.append("include_reasoning=false")
        if where.get("think") is False:
            found.append("think=false")
        kwargs = where.get("chat_template_kwargs") or {}
        if any(value is False for value in kwargs.values()):
            found.append("chat_template_kwargs=false")
    if extras.get("codex_reasoning_effort") == "none":
        found.append("codex_reasoning_effort=none")
    return found


# --------------------------------------------------------------------------- #
# The live bug: OpenRouter routers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("row", [_FREE_ROUTER_ROW, _AUTO_ROUTER_ROW], ids=["free", "auto"])
def test_router_with_no_level_leaves_reasoning_unspecified(row: dict[str, Any]) -> None:
    _record_openrouter_row(row)
    extras = build_request_kwargs(_cfg("openrouter", row["id"]))
    body = extras.get("extra_body") or {}
    assert "reasoning" not in body
    assert "reasoning" not in extras
    assert "include_reasoning" not in body and "reasoning_effort" not in body
    assert _disables(extras) == []


def test_router_passes_an_explicit_level_through() -> None:
    _record_openrouter_row(_FREE_ROUTER_ROW)
    extras = build_request_kwargs(_cfg("openrouter", "openrouter/free", thinking_level="high"))
    assert extras["extra_body"]["reasoning"] == {"effort": "high"}


def test_openrouter_off_is_not_sent_without_evidence_it_can_be_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OpenRouter's unified effort ladder never states reasoning can be disabled
    (gpt-oss-120b's reasoning is mandatory): an explicit off is logged, not sent."""
    _record_openrouter_row({**_FREE_ROUTER_ROW, "id": "openai/gpt-oss-120b:free"})
    with caplog.at_level(logging.WARNING, logger="clio_agent.lm.request_builder"):
        extras = build_request_kwargs(
            _cfg("openrouter", "openai/gpt-oss-120b:free", thinking_level="off")
        )
    assert "reasoning" not in (extras.get("extra_body") or {})
    assert _disables(extras) == []
    assert any(
        "thinking_off_not_sent" in r.getMessage()
        and dialect_wire.REASONING_OFF_NOT_EVIDENCED in r.getMessage()
        for r in caplog.records
    )


def test_openrouter_off_is_sent_when_the_model_lists_off() -> None:
    _seed(
        "openrouter",
        _OPENROUTER_BASE,
        "some/hybrid",
        "openrouter",
        accepted=frozenset({"stop"}),
        controls=frozenset({"reasoning_object"}),
        spec=ThinkingSpec(mechanism="effort_levels", levels=("off", "low", "medium", "high")),
    )
    extras = build_request_kwargs(_cfg("openrouter", "some/hybrid", thinking_level="off"))
    assert extras["extra_body"]["reasoning"] == {"enabled": False}


# --------------------------------------------------------------------------- #
# Every other reasoning-capable dialect: unset never disables
# --------------------------------------------------------------------------- #

_TOGGLE = ThinkingSpec(mechanism="on_off", template_kwarg="enable_thinking")
_EFFORT_WITH_OFF = ThinkingSpec(mechanism="effort_levels", levels=("off", "low", "medium", "high"))
_EFFORT_NO_OFF = ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high"))

#: (provider_id, api_base, model_id, dialect, controls, spec, template_caps) --
#: each a model where an explicit "off" IS sendable, so only the unset level
#: distinguishes "default" from "disable".
_OFF_CAPABLE = [
    ("llama_cpp", "http://127.0.0.1:8088/v1", "qwen3", "llama_cpp",
     frozenset({"reasoning_effort"}), _EFFORT_WITH_OFF, None),
    ("llama_cpp", "http://127.0.0.1:8088/v1", "qwen3-tpl", "llama_cpp",
     frozenset({"chat_template_kwargs"}), _TOGGLE, None),
    ("vllm", "http://127.0.0.1:8000/v1", "Qwen/Qwen3-8B", "vllm",
     frozenset({"chat_template_kwargs"}), _TOGGLE, None),
    ("vllm", "http://127.0.0.1:8000/v1", "budget/model", "vllm",
     frozenset({"thinking_token_budget"}), ThinkingSpec(mechanism="budget_tokens"), None),
    ("ollama", "http://127.0.0.1:11434", "qwen3:8b", "ollama",
     frozenset({"think"}), _TOGGLE, None),
    ("lm_studio", "http://127.0.0.1:1234/v1", "qwen3-8b", "lm_studio",
     frozenset({"reasoning_effort"}), _EFFORT_NO_OFF, {"reasoning_allowed_options": ["off", "on"]}),
    ("openai", "https://api.openai.com/v1", "gpt-5-compat", "openai",
     frozenset({"reasoning_effort"}), _EFFORT_WITH_OFF, None),
    ("codex", "codex://direct", "gpt-5.6-sol", "codex",
     frozenset({"reasoning_effort"}), _EFFORT_WITH_OFF, None),
]  # fmt: skip


def _seed_row(row: tuple[Any, ...]) -> tuple[str, str]:
    provider_id, api_base, model_id, dialect, controls, spec, caps = row
    _seed(
        provider_id,
        api_base,
        model_id,
        dialect,
        accepted=frozenset({"reasoning_effort", "chat_template_kwargs", "think", "stop"}),
        controls=controls,
        spec=spec,
        template_caps=caps,
    )
    return provider_id, model_id


@pytest.mark.parametrize("row", _OFF_CAPABLE, ids=lambda r: f"{r[3]}-{r[2]}")
def test_unset_level_never_sends_a_disable(row: tuple[Any, ...]) -> None:
    provider_id, model_id = _seed_row(row)
    assert _disables(build_request_kwargs(_cfg(provider_id, model_id))) == []


@pytest.mark.parametrize("row", _OFF_CAPABLE, ids=lambda r: f"{r[3]}-{r[2]}")
def test_explicit_off_still_disables_where_evidenced(row: tuple[Any, ...]) -> None:
    provider_id, model_id = _seed_row(row)
    extras = build_request_kwargs(_cfg(provider_id, model_id, thinking_level="off"))
    assert _disables(extras) != []


@pytest.mark.parametrize(
    ("provider_id", "api_base", "model_id", "dialect", "controls", "spec"),
    [
        # gpt-oss: effort-only template; its reasoning cannot be disabled.
        ("llama_cpp", "http://127.0.0.1:8088/v1", "gpt-oss-20b", "llama_cpp",
         frozenset({"reasoning_effort"}), _EFFORT_NO_OFF),
        ("vllm", "http://127.0.0.1:8000/v1", "openai/gpt-oss-120b", "vllm",
         frozenset({"chat_template_kwargs"}),
         ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high"),
                      template_kwarg="reasoning_effort")),
        ("lm_studio", "http://127.0.0.1:1234/v1", "gpt-oss", "lm_studio",
         frozenset({"reasoning_effort"}), _EFFORT_NO_OFF),
        ("ollama", "http://127.0.0.1:11434", "gpt-oss:20b", "ollama",
         frozenset({"think"}), _EFFORT_NO_OFF),
    ],
)  # fmt: skip
def test_explicit_off_is_not_sent_to_a_model_that_never_states_it(
    provider_id: str,
    api_base: str,
    model_id: str,
    dialect: str,
    controls: frozenset[str],
    spec: ThinkingSpec,
) -> None:
    _seed(
        provider_id,
        api_base,
        model_id,
        dialect,
        accepted=frozenset({"reasoning_effort", "chat_template_kwargs", "think", "stop"}),
        controls=controls,
        spec=spec,
    )
    extras = build_request_kwargs(_cfg(provider_id, model_id, thinking_level="off"))
    assert _disables(extras) == []
    assert "chat_template_kwargs" not in (extras.get("extra_body") or {})


@pytest.mark.parametrize("dialect", ["litellm_proxy", "openrouter", "vllm", "llama_cpp"])
@pytest.mark.parametrize("level", [None, "off"])
def test_thinking_wire_sends_nothing_for_unset_or_unevidenced_off(
    dialect: str, level: str | None
) -> None:
    decision = ThinkingDecision(spec=_EFFORT_NO_OFF, control="reasoning_effort", decided_by="t")
    assert dialect_wire.thinking_wire(dialect, decision, level=level) == {}


# --------------------------------------------------------------------------- #
# The picker offers "off" on exactly the same evidence
# --------------------------------------------------------------------------- #


def _offered(spec: ThinkingSpec, dialect: str) -> list[str]:
    from clio_agent.gact.provider_catalog import _reasoning_wire_block

    thinking = SimpleNamespace(spec=spec, control="reasoning_effort", decided_by="model")
    return list(_reasoning_wire_block(thinking, DiscoveredModel(id="m"), dialect=dialect)["levels"])


def test_picker_offers_off_only_where_it_is_sent() -> None:
    model, _deployment = openrouter.parse_model_row(
        _FREE_ROUTER_ROW, provider_id="openrouter", api_base=_OPENROUTER_BASE, observed_at=_NOW
    )
    router_spec = model.thinking.value
    assert router_spec is not None
    assert _offered(router_spec, "openrouter") == ["low", "medium", "high"]
    assert _offered(_EFFORT_WITH_OFF, "openrouter") == ["off", "low", "medium", "high"]
    assert _offered(_TOGGLE, "vllm") == ["off", "low", "medium", "high"]
    # claude_code's "off" is the Agent SDK's own disabled option, valid for every model.
    assert _offered(_EFFORT_NO_OFF, "claude_code")[0] == "off"
