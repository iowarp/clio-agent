"""Wire tests for the request builder (model-capabilities brief Part 7 / P5).

Covers, per HTTP dialect (llama.cpp, vLLM, Ollama, LM Studio, OpenRouter):
thinking off, thinking at a level, a request with user-set sampling, and a
request with no sampling at all -- asserting the EXACT kwargs dict
:func:`clio_agent.lm.request_builder.build_request_kwargs` returns. Also
covers the parameter gate (an unsupported field never reaches the wire), stop
sequences only when accepted, the ``temperature=None`` default, and
grep-to-zero assertions for every symbol the model-capabilities brief 9.1
mandates deleting.

Each dialect test seeds the three capability records directly through
:mod:`clio_agent.providers.capabilities.invalidation` (never a live handshake
-- no network, no daemon, matching this campaign's resource rules) so
``build_request_kwargs``'s own accessor lookup resolves exactly the record
this test wrote.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.lm.request_builder import build_request_kwargs
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    unknown,
)
from tests._catalog_seed import seed_litellm_cost_map

_NOW = "2026-09-25T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset_capability_state():
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


def _seed(
    *,
    provider_id: str,
    api_base: str,
    model_id: str,
    dialect: str,
    accepted_params: frozenset[str],
    thinking_controls: frozenset[str] = frozenset(),
    thinking_spec: ThinkingSpec | None = None,
    template_caps: dict[str, bool] | None = None,
    route_params: frozenset[str] | None = None,
) -> None:
    """Write model/endpoint/deployment records an identity-matched lookup will find."""

    model_key = f"test-model:{provider_id}:{model_id}"
    invalidation.record_endpoint_capabilities(
        EndpointCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            dialect=dialect,
            accepted_params=Fact(accepted_params, "litellm", _NOW),
            thinking_controls=(
                Fact(thinking_controls, "dialect", _NOW) if thinking_controls else unknown()
            ),
        )
    )
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key=model_key,
            thinking=(Fact(thinking_spec, "server_report", _NOW) if thinking_spec else unknown()),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            model_id=model_id,
            model_key=Fact(model_key, "server_report", _NOW),
            template_caps=(
                Fact(template_caps, "server_report", _NOW) if template_caps else unknown()
            ),
            route_params=(Fact(route_params, "server_report", _NOW) if route_params else unknown()),
        )
    )


def _cfg(provider_id: str, model: str, **kwargs: object) -> LMProviderConfig:
    return LMProviderConfig(provider_id=provider_id, model=model, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# llama.cpp
# --------------------------------------------------------------------------- #

_LLAMA_CPP_BASE = "http://127.0.0.1:8088/v1"
_LLAMA_CPP_ACCEPTED = frozenset(
    {"reasoning_effort", "chat_template_kwargs", "top_k", "min_p", "stop", "temperature", "top_p"}
)


def _seed_llama_cpp(model_id: str = "qwen3-8b-gguf") -> None:
    _seed(
        provider_id="llama_cpp",
        api_base=_LLAMA_CPP_BASE,
        model_id=model_id,
        dialect="llama_cpp",
        accepted_params=_LLAMA_CPP_ACCEPTED,
        thinking_controls=frozenset({"reasoning_effort", "chat_template_kwargs"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
        template_caps={"supports_reasoning_effort": True, "supports_tools": True},
    )


def test_llama_cpp_thinking_off() -> None:
    _seed_llama_cpp()
    extras = build_request_kwargs(_cfg("llama_cpp", "qwen3-8b-gguf", thinking_level="off"))
    assert extras["extra_body"]["reasoning_effort"] == "none"


def test_llama_cpp_thinking_level() -> None:
    _seed_llama_cpp()
    extras = build_request_kwargs(_cfg("llama_cpp", "qwen3-8b-gguf", thinking_level="medium"))
    assert extras["extra_body"]["reasoning_effort"] == "medium"


def test_llama_cpp_falls_back_to_chat_template_kwargs_without_reasoning_effort_control() -> None:
    """When the endpoint only offers chat_template_kwargs (no reasoning_effort)."""
    _seed(
        provider_id="llama_cpp",
        api_base=_LLAMA_CPP_BASE,
        model_id="qwen3-8b-gguf",
        dialect="llama_cpp",
        accepted_params=_LLAMA_CPP_ACCEPTED,
        thinking_controls=frozenset({"chat_template_kwargs"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
    )
    extras = build_request_kwargs(_cfg("llama_cpp", "qwen3-8b-gguf", thinking_level="medium"))
    assert extras["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    off = build_request_kwargs(_cfg("llama_cpp", "qwen3-8b-gguf", thinking_level="off"))
    assert off["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_llama_cpp_with_user_sampling() -> None:
    _seed_llama_cpp()
    extras = build_request_kwargs(
        _cfg("llama_cpp", "qwen3-8b-gguf", top_k=20, min_p=0.05, top_p=0.9, temperature=0.6)
    )
    assert extras["top_p"] == 0.9
    assert extras["temperature"] == 0.6
    assert extras["extra_body"]["top_k"] == 20
    assert extras["extra_body"]["min_p"] == 0.05


def test_llama_cpp_with_no_sampling_omits_every_optional_sampling_field() -> None:
    _seed_llama_cpp()
    extras = build_request_kwargs(_cfg("llama_cpp", "qwen3-8b-gguf"))
    assert "temperature" not in extras
    assert "top_p" not in extras
    assert "top_k" not in (extras.get("extra_body") or {})
    assert "min_p" not in (extras.get("extra_body") or {})


def test_llama_cpp_parameter_gate_never_sends_an_unsupported_field() -> None:
    """presence_penalty is NOT in llama.cpp's accepted set here -- must never reach the wire."""
    _seed_llama_cpp()
    extras = build_request_kwargs(_cfg("llama_cpp", "qwen3-8b-gguf", presence_penalty=0.4))
    assert "presence_penalty" not in extras
    assert "presence_penalty" not in (extras.get("extra_body") or {})


def test_llama_cpp_stop_sequences_sent_when_accepted() -> None:
    _seed_llama_cpp()
    extras = build_request_kwargs(_cfg("llama_cpp", "qwen3-8b-gguf"))
    assert extras["stop"] == [
        "[[ ## observation",
        "[[ ## thought_",
        "[[ ## tool_name_",
        "[[ ## tool_args_",
    ]


def test_llama_cpp_stop_sequences_withheld_when_not_accepted() -> None:
    _seed(
        provider_id="llama_cpp",
        api_base=_LLAMA_CPP_BASE,
        model_id="reasoning-only-model",
        dialect="llama_cpp",
        accepted_params=frozenset({"temperature"}),  # no "stop"
    )
    extras = build_request_kwargs(_cfg("llama_cpp", "reasoning-only-model"))
    assert "stop" not in extras


# --------------------------------------------------------------------------- #
# vLLM
# --------------------------------------------------------------------------- #

_VLLM_BASE = "http://127.0.0.1:8000/v1"
_VLLM_ACCEPTED = frozenset(
    {"chat_template_kwargs", "top_k", "min_p", "repetition_penalty", "stop", "temperature"}
)


def test_vllm_thinking_off_and_on_via_chat_template_kwargs() -> None:
    _seed(
        provider_id="vllm",
        api_base=_VLLM_BASE,
        model_id="Qwen/Qwen3-8B",
        dialect="vllm",
        accepted_params=_VLLM_ACCEPTED,
        thinking_controls=frozenset({"chat_template_kwargs"}),
        thinking_spec=ThinkingSpec(mechanism="on_off", template_kwarg="enable_thinking", levels=()),
    )
    off = build_request_kwargs(_cfg("vllm", "Qwen/Qwen3-8B", thinking_level="off"))
    assert off["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    on = build_request_kwargs(_cfg("vllm", "Qwen/Qwen3-8B", thinking_level="low"))
    assert on["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_vllm_gpt_oss_style_effort_via_its_own_template_kwarg() -> None:
    """gpt-oss's own template kwarg name IS its effort spelling (Part 7 table)."""
    _seed(
        provider_id="vllm",
        api_base=_VLLM_BASE,
        model_id="openai/gpt-oss-20b",
        dialect="vllm",
        accepted_params=_VLLM_ACCEPTED,
        thinking_controls=frozenset({"chat_template_kwargs"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels",
            levels=("low", "medium", "high"),
            template_kwarg="reasoning_effort",
        ),
    )
    extras = build_request_kwargs(_cfg("vllm", "openai/gpt-oss-20b", thinking_level="high"))
    assert extras["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}


def test_vllm_budget_control() -> None:
    _seed(
        provider_id="vllm",
        api_base=_VLLM_BASE,
        model_id="some/budget-model",
        dialect="vllm",
        accepted_params=_VLLM_ACCEPTED,
        thinking_controls=frozenset({"thinking_token_budget"}),
        thinking_spec=ThinkingSpec(mechanism="budget_tokens", budget_range=(0, 16384)),
    )
    extras = build_request_kwargs(_cfg("vllm", "some/budget-model", thinking_level="high"))
    assert extras["thinking_token_budget"] == 16384
    off = build_request_kwargs(_cfg("vllm", "some/budget-model", thinking_level="off"))
    assert off["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_vllm_with_user_sampling_and_repetition_penalty() -> None:
    _seed(
        provider_id="vllm",
        api_base=_VLLM_BASE,
        model_id="Qwen/Qwen3-8B",
        dialect="vllm",
        accepted_params=_VLLM_ACCEPTED,
    )
    extras = build_request_kwargs(_cfg("vllm", "Qwen/Qwen3-8B", top_k=10, min_p=0.02))
    assert extras["extra_body"]["top_k"] == 10
    assert extras["extra_body"]["min_p"] == 0.02


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #

_OLLAMA_BASE = "http://127.0.0.1:11434"
_OLLAMA_ACCEPTED = frozenset({"think", "stop", "temperature"})


def test_ollama_think_boolean_when_model_reports_no_levels() -> None:
    _seed(
        provider_id="ollama",
        api_base=_OLLAMA_BASE,
        model_id="qwen3:8b",
        dialect="ollama",
        accepted_params=_OLLAMA_ACCEPTED,
        thinking_controls=frozenset({"think"}),
        thinking_spec=ThinkingSpec(mechanism="on_off"),
    )
    on = build_request_kwargs(_cfg("ollama", "qwen3:8b", thinking_level="low"))
    assert on["extra_body"]["think"] is True
    off = build_request_kwargs(_cfg("ollama", "qwen3:8b", thinking_level="off"))
    assert off["extra_body"]["think"] is False


def test_ollama_think_level_string_for_gpt_oss() -> None:
    _seed(
        provider_id="ollama",
        api_base=_OLLAMA_BASE,
        model_id="gpt-oss:20b",
        dialect="ollama",
        accepted_params=_OLLAMA_ACCEPTED,
        thinking_controls=frozenset({"think"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
    )
    extras = build_request_kwargs(_cfg("ollama", "gpt-oss:20b", thinking_level="high"))
    assert extras["extra_body"]["think"] == "high"


def test_ollama_never_sends_reasoning_effort() -> None:
    """The historical bug this brief fixes: ollama has no reasoning_effort field."""
    _seed(
        provider_id="ollama",
        api_base=_OLLAMA_BASE,
        model_id="qwen3:8b",
        dialect="ollama",
        accepted_params=_OLLAMA_ACCEPTED,
        thinking_controls=frozenset({"think"}),
        thinking_spec=ThinkingSpec(mechanism="on_off"),
    )
    extras = build_request_kwargs(_cfg("ollama", "qwen3:8b", thinking_level="medium"))
    assert "reasoning_effort" not in extras
    assert "reasoning_effort" not in (extras.get("extra_body") or {})


def test_ollama_with_no_evidence_sends_no_thinking_directive() -> None:
    """Fail closed: no model/deployment record yet -> no thinking config guessed."""
    extras = build_request_kwargs(_cfg("ollama", "unknown-model", thinking_level="high"))
    assert "think" not in extras
    assert "think" not in (extras.get("extra_body") or {})


# --------------------------------------------------------------------------- #
# LM Studio
# --------------------------------------------------------------------------- #

_LM_STUDIO_BASE = "http://127.0.0.1:1234/v1"
_LM_STUDIO_ACCEPTED = frozenset({"reasoning_effort", "stop", "temperature", "top_p"})


def test_lm_studio_reasoning_effort_from_allowed_options() -> None:
    _seed(
        provider_id="lm_studio",
        api_base=_LM_STUDIO_BASE,
        model_id="qwen3-8b",
        dialect="lm_studio",
        accepted_params=_LM_STUDIO_ACCEPTED,
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
        template_caps={"reasoning_allowed_options": ["low", "medium", "high"]},
    )
    extras = build_request_kwargs(_cfg("lm_studio", "qwen3-8b", thinking_level="medium"))
    assert extras["reasoning_effort"] == "medium"


def test_lm_studio_refuses_a_level_outside_allowed_options() -> None:
    """Fail closed: the server told us exactly which values it accepts."""
    _seed(
        provider_id="lm_studio",
        api_base=_LM_STUDIO_BASE,
        model_id="qwen3-8b",
        dialect="lm_studio",
        accepted_params=_LM_STUDIO_ACCEPTED,
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
        template_caps={"reasoning_allowed_options": ["low"]},
    )
    extras = build_request_kwargs(_cfg("lm_studio", "qwen3-8b", thinking_level="high"))
    assert "reasoning_effort" not in extras


def test_lm_studio_off() -> None:
    _seed(
        provider_id="lm_studio",
        api_base=_LM_STUDIO_BASE,
        model_id="qwen3-8b",
        dialect="lm_studio",
        accepted_params=_LM_STUDIO_ACCEPTED,
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
    )
    extras = build_request_kwargs(_cfg("lm_studio", "qwen3-8b", thinking_level="off"))
    assert extras["reasoning_effort"] == "none"


def test_lm_studio_never_receives_chat_template_kwargs() -> None:
    """The supplement table REMOVES chat_template_kwargs from LM Studio's set."""
    from clio_agent.providers.capabilities.endpoint import resolve_accepted_params

    fact = resolve_accepted_params("lm_studio", "qwen3-8b", custom_llm_provider="lm_studio")
    assert "chat_template_kwargs" not in (fact.value or frozenset())


def test_lm_studio_with_user_sampling() -> None:
    _seed(
        provider_id="lm_studio",
        api_base=_LM_STUDIO_BASE,
        model_id="qwen3-8b",
        dialect="lm_studio",
        accepted_params=_LM_STUDIO_ACCEPTED,
    )
    extras = build_request_kwargs(_cfg("lm_studio", "qwen3-8b", top_p=0.8, temperature=0.5))
    assert extras["top_p"] == 0.8
    assert extras["temperature"] == 0.5


# --------------------------------------------------------------------------- #
# OpenRouter
# --------------------------------------------------------------------------- #

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENROUTER_ACCEPTED = frozenset({"top_k", "stop", "temperature"})


def test_openrouter_reasoning_effort_and_require_parameters_flag() -> None:
    _seed(
        provider_id="openrouter",
        api_base=_OPENROUTER_BASE,
        model_id="openai/gpt-oss-120b",
        dialect="openrouter",
        accepted_params=_OPENROUTER_ACCEPTED,
        thinking_controls=frozenset({"reasoning_object"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
    )
    extras = build_request_kwargs(_cfg("openrouter", "openai/gpt-oss-120b", thinking_level="high"))
    assert extras["extra_body"]["reasoning"] == {"effort": "high"}
    assert extras["extra_body"]["provider"] == {"require_parameters": True}


def test_openrouter_off_is_reasoning_disabled() -> None:
    _seed(
        provider_id="openrouter",
        api_base=_OPENROUTER_BASE,
        model_id="openai/gpt-oss-120b",
        dialect="openrouter",
        accepted_params=_OPENROUTER_ACCEPTED,
        thinking_controls=frozenset({"reasoning_object"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high")),
    )
    extras = build_request_kwargs(_cfg("openrouter", "openai/gpt-oss-120b", thinking_level="off"))
    assert extras["extra_body"]["reasoning"] == {"enabled": False}


def test_openrouter_top_k_gated_by_route_params() -> None:
    """A model without top_k in its OWN supported_parameters must not receive it,
    even though the endpoint's generic set includes it."""
    _seed(
        provider_id="openrouter",
        api_base=_OPENROUTER_BASE,
        model_id="some/model-without-top-k",
        dialect="openrouter",
        accepted_params=_OPENROUTER_ACCEPTED,
        route_params=frozenset({"temperature", "stop"}),  # no "top_k"
    )
    extras = build_request_kwargs(_cfg("openrouter", "some/model-without-top-k", top_k=20))
    assert "top_k" not in extras
    assert "top_k" not in (extras.get("extra_body") or {})


def test_openrouter_require_parameters_absent_when_nothing_optional_sent() -> None:
    _seed(
        provider_id="openrouter",
        api_base=_OPENROUTER_BASE,
        model_id="bare/model",
        dialect="openrouter",
        accepted_params=frozenset(),  # nothing accepted at all
    )
    extras = build_request_kwargs(_cfg("openrouter", "bare/model"))
    assert "extra_body" not in extras or "provider" not in extras.get("extra_body", {})


# --------------------------------------------------------------------------- #
# codex (SDK transport) -- ThinkingSpec sourced from providers.capabilities.
# dialects.codex, driven by the SDK's own reported supportedReasoningEfforts.
# --------------------------------------------------------------------------- #

_CODEX_BASE = "codex://direct"
_CODEX_LEVELS = ("low", "medium", "high", "xhigh")


def _seed_codex(model_id: str = "gpt-5.6-sol", levels: tuple[str, ...] = _CODEX_LEVELS) -> None:
    _seed(
        provider_id="codex",
        api_base=_CODEX_BASE,
        model_id=model_id,
        dialect="codex",
        accepted_params=frozenset(),
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels",
            levels=levels,
            effort_by_level={level: ("none" if level == "off" else level) for level in levels},
        ),
    )


def test_codex_thinking_off_sends_none_when_the_model_lists_it() -> None:
    _seed_codex(levels=("off", *_CODEX_LEVELS))
    extras = build_request_kwargs(_cfg("codex", "gpt-5.6-sol", thinking_level="off"))
    assert extras["codex_reasoning_effort"] == "none"


def test_codex_thinking_off_is_omitted_when_the_model_does_not_list_it() -> None:
    """The backend refuses an unlisted effort (live: gpt-6-astra rejects 'none')."""
    _seed_codex()
    extras = build_request_kwargs(_cfg("codex", "gpt-5.6-sol", thinking_level="off"))
    assert "codex_reasoning_effort" not in extras
    unset = build_request_kwargs(_cfg("codex", "gpt-5.6-sol"))
    assert "codex_reasoning_effort" not in unset


@pytest.mark.parametrize("level", _CODEX_LEVELS)
def test_codex_thinking_each_level(level: str) -> None:
    _seed_codex()
    extras = build_request_kwargs(_cfg("codex", "gpt-5.6-sol", thinking_level=level))
    assert extras["codex_reasoning_effort"] == level


# --------------------------------------------------------------------------- #
# claude_code (SDK transport) -- ThinkingSpec sourced from providers.
# capabilities.dialects.claude_code, driven by the CLI's own
# supportedEffortLevels (or a generic token budget for a model with none).
# --------------------------------------------------------------------------- #

_CLAUDE_CODE_BASE = "claude-code://sdk"
_CLAUDE_CODE_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _seed_claude_code_effort(model_id: str = "sonnet") -> None:
    _seed(
        provider_id="claude_code",
        api_base=_CLAUDE_CODE_BASE,
        model_id=model_id,
        dialect="claude_code",
        accepted_params=frozenset(),
        thinking_controls=frozenset({"effort", "claude_code_thinking"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels",
            levels=_CLAUDE_CODE_LEVELS,
            effort_by_level={level: level for level in _CLAUDE_CODE_LEVELS},
        ),
    )


def _seed_claude_code_budget(model_id: str = "haiku") -> None:
    _seed(
        provider_id="claude_code",
        api_base=_CLAUDE_CODE_BASE,
        model_id=model_id,
        dialect="claude_code",
        accepted_params=frozenset(),
        thinking_controls=frozenset({"claude_code_thinking"}),
        thinking_spec=ThinkingSpec(mechanism="budget_tokens"),
    )


def test_claude_code_thinking_off() -> None:
    _seed_claude_code_effort()
    extras = build_request_kwargs(_cfg("claude_code", "sonnet", thinking_level="off"))
    assert extras["claude_code_thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("level", _CLAUDE_CODE_LEVELS)
def test_claude_code_thinking_each_effort_level(level: str) -> None:
    _seed_claude_code_effort()
    extras = build_request_kwargs(_cfg("claude_code", "sonnet", thinking_level=level))
    assert extras["claude_code_thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "effort": level,
    }


def test_claude_code_budget_mechanism_off() -> None:
    _seed_claude_code_budget()
    extras = build_request_kwargs(_cfg("claude_code", "haiku", thinking_level="off"))
    assert extras["claude_code_thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(("level", "budget"), [("low", 2048), ("medium", 8192), ("high", 24576)])
def test_claude_code_budget_mechanism_uses_the_generic_ladder(level: str, budget: int) -> None:
    """A model the CLI lists with no per-model effort levels (e.g. haiku)
    still gets real thinking, via CLIO's own generic level->budget ladder."""
    _seed_claude_code_budget()
    extras = build_request_kwargs(_cfg("claude_code", "haiku", thinking_level=level))
    assert extras["claude_code_thinking"] == {
        "type": "enabled",
        "budget_tokens": budget,
        "display": "summarized",
    }


# --------------------------------------------------------------------------- #
# anthropic -- ThinkingSpec sourced from providers.capabilities.dialects.
# cloud_thinking, driven by LiteLLM's own adaptive-thinking introspection.
# --------------------------------------------------------------------------- #

_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_ANTHROPIC_LEVELS = ("low", "medium", "high", "max")


def _seed_anthropic_effort(model_id: str = "claude-opus-4-7") -> None:
    _seed(
        provider_id="anthropic",
        api_base=_ANTHROPIC_BASE,
        model_id=model_id,
        dialect="anthropic",
        accepted_params=frozenset(),
        thinking_controls=frozenset({"reasoning_effort", "anthropic_thinking"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels", levels=_ANTHROPIC_LEVELS, effort_by_level={}
        ),
    )


def _seed_anthropic_budget(model_id: str = "claude-sonnet-4-5") -> None:
    _seed(
        provider_id="anthropic",
        api_base=_ANTHROPIC_BASE,
        model_id=model_id,
        dialect="anthropic",
        accepted_params=frozenset(),
        thinking_controls=frozenset({"anthropic_thinking"}),
        thinking_spec=ThinkingSpec(mechanism="budget_tokens"),
    )


def test_anthropic_effort_off_sends_nothing() -> None:
    """The Anthropic API's own default is thinking off -- nothing to send."""
    _seed_anthropic_effort()
    extras = build_request_kwargs(_cfg("anthropic", "claude-opus-4-7", thinking_level="off"))
    assert "reasoning_effort" not in extras
    assert "thinking" not in extras


@pytest.mark.parametrize("level", _ANTHROPIC_LEVELS)
def test_anthropic_effort_each_level(level: str) -> None:
    _seed_anthropic_effort()
    extras = build_request_kwargs(_cfg("anthropic", "claude-opus-4-7", thinking_level=level))
    assert extras["reasoning_effort"] == level


def test_anthropic_budget_off_sends_nothing() -> None:
    _seed_anthropic_budget()
    extras = build_request_kwargs(_cfg("anthropic", "claude-sonnet-4-5", thinking_level="off"))
    assert "thinking" not in extras


@pytest.mark.parametrize(("level", "budget"), [("low", 2048), ("medium", 8192), ("high", 24576)])
def test_anthropic_budget_mechanism_uses_the_generic_ladder(level: str, budget: int) -> None:
    """A model with no adaptive-effort evidence still gets a real thinking
    token budget (a platform-wide fact), off the same generic ladder."""
    _seed_anthropic_budget()
    extras = build_request_kwargs(_cfg("anthropic", "claude-sonnet-4-5", thinking_level=level))
    assert extras["thinking"] == {"type": "enabled", "budget_tokens": budget}


# --------------------------------------------------------------------------- #
# openai -- ThinkingSpec sourced from providers.capabilities.dialects.
# cloud_thinking, driven by the LiteLLM model map's own supports_reasoning /
# supports_*_reasoning_effort flags.
# --------------------------------------------------------------------------- #

_OPENAI_BASE = "https://api.openai.com/v1"
_OPENAI_LEVELS = ("minimal", "low", "medium", "high")


def _seed_openai(model_id: str, *, levels: tuple[str, ...]) -> None:
    _seed(
        provider_id="openai",
        api_base=_OPENAI_BASE,
        model_id=model_id,
        dialect="openai",
        accepted_params=frozenset(),
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=levels, effort_by_level={}),
    )


def test_openai_off_omitted_when_the_model_reports_no_off_level() -> None:
    """Some reasoning models accept no explicit off -- omitting the field IS
    the correct "off" (never a guessed 'none' value the model may reject)."""
    _seed_openai("gpt-5", levels=_OPENAI_LEVELS)
    extras = build_request_kwargs(_cfg("openai", "gpt-5", thinking_level="off"))
    assert "reasoning_effort" not in extras


def test_openai_off_sends_none_when_the_model_reports_an_off_level() -> None:
    _seed_openai("gpt-5-compat", levels=("off", *_OPENAI_LEVELS))
    extras = build_request_kwargs(_cfg("openai", "gpt-5-compat", thinking_level="off"))
    assert extras["reasoning_effort"] == "none"


@pytest.mark.parametrize("level", _OPENAI_LEVELS)
def test_openai_each_level(level: str) -> None:
    _seed_openai("gpt-5", levels=_OPENAI_LEVELS)
    extras = build_request_kwargs(_cfg("openai", "gpt-5", thinking_level=level))
    assert extras["reasoning_effort"] == level


# --------------------------------------------------------------------------- #
# temperature=None default (Part 7 item 1)
# --------------------------------------------------------------------------- #


def test_lmproviderconfig_temperature_defaults_to_none() -> None:
    assert LMProviderConfig(provider="lm_studio", model="m").temperature is None


def test_temperature_omitted_by_default_even_when_accepted() -> None:
    _seed(
        provider_id="lm_studio",
        api_base=_LM_STUDIO_BASE,
        model_id="m",
        dialect="lm_studio",
        accepted_params=frozenset({"temperature"}),
    )
    extras = build_request_kwargs(_cfg("lm_studio", "m"))
    assert "temperature" not in extras


def test_planner_role_sends_planner_temperature_when_effective() -> None:
    _seed(
        provider_id="lm_studio",
        api_base=_LM_STUDIO_BASE,
        model_id="m",
        dialect="lm_studio",
        accepted_params=frozenset({"temperature"}),
    )
    extras = build_request_kwargs(_cfg("lm_studio", "m"), role="planner")
    assert extras["temperature"] == 0.3  # LMProviderConfig.planner_temperature default


def test_planner_role_omits_temperature_when_not_effective() -> None:
    _seed(
        provider_id="lm_studio",
        api_base=_LM_STUDIO_BASE,
        model_id="m",
        dialect="lm_studio",
        accepted_params=frozenset(),  # temperature NOT accepted
    )
    extras = build_request_kwargs(_cfg("lm_studio", "m"), role="planner")
    assert "temperature" not in extras


# --------------------------------------------------------------------------- #
# Grep-to-zero: every deleted symbol the brief names (9.1 / PR body item 11).
#
# Two different kinds of check are needed:
#
# * A symbol that must be gone as a CODE identifier entirely
#   (`_provider_lm_kwargs`, `_thinking_kwargs`, `_reasoning_model_capability`,
#   `_uses_local_reasoning_model_profile`, `CLIO_LM_DISABLE_THINKING`,
#   `_ARGONNE_MODELS`, the `temperature: float = 0.0` field default) is
#   grepped against a COMMENT/DOCSTRING-STRIPPED copy of the source -- a
#   comment or docstring that *documents* the deletion (e.g. this test file's
#   own module docstring, or `lm/request_builder.py`'s "replaces
#   _provider_lm_kwargs/_thinking_kwargs" note) is expected prose, not a
#   surviving symbol, and must not fail this check.
# * `supports_vision`/`max_tokens_default` are checked by INTROSPECTING the
#   three dataclasses/models the brief names (`Provider`, `LMProviderConfig`,
#   `LMProviderPreset`), not by grepping the bare string -- that string is
#   still legitimate, still-used vocabulary elsewhere (e.g.
#   `resolve_declared_native_tools(supports_vision=...)`, `_effective_lm_config`'s
#   derived `cfg["supports_vision"]` key): the brief deletes the STATIC
#   per-provider FLAG, not the concept of "does this turn support vision".
# --------------------------------------------------------------------------- #

_SRC = Path(__file__).resolve().parents[2] / "src" / "clio_agent"

_TRIPLE_QUOTED = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)


def _code_only(text: str) -> str:
    """``text`` with every docstring/comment stripped, code lines only."""

    without_docstrings = _TRIPLE_QUOTED.sub("", text)
    lines = []
    for line in without_docstrings.splitlines():
        stripped = line.split("#", 1)[0] if "#" in line else line
        lines.append(stripped)
    return "\n".join(lines)


_DELETED_CODE_SYMBOL_PATTERNS: tuple[str, ...] = (
    r"CLIO_LM_DISABLE_THINKING",
    r"_provider_lm_kwargs",
    # NOT `_legacy_thinking_kwargs` -- request_builder.py's own, intentionally
    # kept, differently-named replacement (module docstring explains why).
    r"(?<!_legacy)_thinking_kwargs\b",
    r"_reasoning_model_capability",
    r"_uses_local_reasoning_model_profile",
    r"_ARGONNE_MODELS",
    r"temperature: float = 0\.0",
)


@pytest.mark.parametrize("pattern", _DELETED_CODE_SYMBOL_PATTERNS)
def test_deleted_symbol_has_zero_code_occurrences_in_src(pattern: str) -> None:
    hits: list[str] = []
    for path in _SRC.rglob("*.py"):
        code = _code_only(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(code.splitlines(), start=1):
            if re.search(pattern, line):
                hits.append(f"{path}:{lineno}: {line.strip()}")
    assert not hits, f"{pattern!r} still present in code:\n" + "\n".join(hits)


def test_provider_dataclass_has_no_supports_vision_or_max_tokens_default_field() -> None:
    import dataclasses

    from clio_agent.providers.catalog_types import Provider

    field_names = {f.name for f in dataclasses.fields(Provider)}
    assert "supports_vision" not in field_names
    assert "max_tokens_default" not in field_names


def test_lm_provider_config_has_no_supports_vision_field() -> None:
    import dataclasses

    from clio_agent.config import LMProviderConfig

    field_names = {f.name for f in dataclasses.fields(LMProviderConfig)}
    assert "supports_vision" not in field_names


def test_lm_provider_preset_wire_type_has_no_supports_vision_field() -> None:
    from clio_agent.gact.lm_provider_types import LMProviderPreset

    assert "supports_vision" not in LMProviderPreset.model_fields


def test_model_catalog_assignment_is_zero_everywhere() -> None:
    """``model_catalog=`` (an assignment, not the bare field declaration) is zero.

    claude_code's own former exception (the fable/haiku/sonnet/opus rows) is
    gone: its vision/pdf modality evidence now comes from the maintained
    catalog document (``catalogs/claude-code-models.json``, read through
    ``ClaudeCodeCatalogHandshake``/the refresh overlay), never a second,
    hand-typed candidate list in ``providers/catalog.py``.
    """

    hits: list[tuple[Path, int]] = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if re.search(r"model_catalog=", line):
                hits.append((path, lineno))
    assert not hits, f"model_catalog= still present: {hits}"


# --------------------------------------------------------------------------- #
# Local-first thinking: a freshly bound cloud model (no handshake recorded yet)
# still gets its requested effort. The spec comes from LiteLLM's local map --
# the same fact the openai-compatible handshake records later.
# --------------------------------------------------------------------------- #


def test_openai_effort_applies_before_any_handshake() -> None:
    seed_litellm_cost_map()
    extras = build_request_kwargs(_cfg("openai", "gpt-5", thinking_level="high"))
    assert extras["reasoning_effort"] == "high"


def test_local_thinking_names_its_source_and_never_guesses() -> None:
    seed_litellm_cost_map()
    from clio_agent.lm.request_builder import local_first_effective

    effective = local_first_effective(
        "openai", "https://api.openai.com/v1", "gpt-5", dialect="openai", litellm_prefix="openai"
    )
    assert effective.thinking.known
    assert "litellm" in effective.thinking.source.split("+")
    # No per-model thinking source for this dialect: stays unknown, never guessed.
    unknown_dialect = local_first_effective(
        "vllm",
        "http://127.0.0.1:8000/v1",
        "some/model",
        dialect="vllm",
        litellm_prefix="hosted_vllm",
    )
    assert not unknown_dialect.thinking.known
