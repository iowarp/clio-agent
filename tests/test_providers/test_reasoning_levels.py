"""Per-model reasoning levels in the provider catalog (I).

Each model advertises only the thinking levels its provider really reports for
it AND that ``resolve_thinking`` maps -- never a hard-coded list.
"""

from __future__ import annotations

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.providers.handshake.model import ModelProfile
from clio_agent.providers.reasoning_levels import model_reasoning
from clio_agent.providers.thinking import accepted_levels, resolve_thinking


def _codex_profile(efforts: list[str] | None, default: str = "") -> ModelProfile:
    raw: dict[str, object] = {"default_reasoning_effort": default}
    if efforts is not None:
        raw["supported_reasoning_efforts"] = efforts
    return ModelProfile(id="gpt-5.6-sol", raw=raw)


def test_codex_levels_come_from_the_sdk_catalog() -> None:
    reasoning = model_reasoning(
        "codex", _codex_profile(["minimal", "low", "medium", "high", "xhigh"], "medium")
    )
    # minimal has no clio level; xhigh is passed through (Codex really has it).
    assert reasoning["levels"] == ["low", "medium", "high", "xhigh"]
    assert reasoning["default"] == "medium"
    assert reasoning["source"] == "codex_sdk"
    assert reasoning["supported"] is True


def test_codex_none_effort_is_the_off_level() -> None:
    reasoning = model_reasoning("codex", _codex_profile(["none", "low"], "none"))
    assert reasoning["levels"] == ["off", "low"]
    assert reasoning["default"] == "off"


def test_codex_without_reported_efforts_offers_nothing() -> None:
    reasoning = model_reasoning("codex", _codex_profile(None))
    assert reasoning["levels"] == []
    assert reasoning["supported"] is False
    assert reasoning["source"] == "codex_sdk_unreported"


def test_claude_code_offers_the_budget_ladder_with_shipped_default() -> None:
    sonnet = model_reasoning("claude_code", ModelProfile(id="sonnet"))
    assert sonnet["levels"] == ["off", "low", "medium", "high"]
    assert sonnet["default"] == "low"
    opus = model_reasoning("claude_code", ModelProfile(id="opus"))
    assert opus["default"] == ""  # the SDK's own default governs


def test_anthropic_levels_follow_litellm_model_info() -> None:
    thinking = model_reasoning("anthropic", ModelProfile(id="claude-sonnet-4-5"))
    assert thinking["levels"] == ["off", "low", "medium", "high"]
    assert thinking["default"] == "off"
    old = model_reasoning("anthropic", ModelProfile(id="claude-3-5-haiku-20241022"))
    assert old["levels"] == []
    assert old["supported"] is False


def test_alcf_gpt_oss_offers_low_medium_high() -> None:
    profile = ModelProfile(
        id="openai/gpt-oss-120b", is_reasoning=True, reasoning_param="openai_gptoss"
    )
    reasoning = model_reasoning("argonne", profile)
    assert reasoning["levels"] == ["low", "medium", "high"]
    assert reasoning["default"] == "medium"
    assert reasoning["parameter"] == "openai_gptoss"


def test_alcf_reasoning_model_that_ignores_effort_offers_no_levels() -> None:
    profile = ModelProfile(id="Qwen/Qwen3-32B", is_reasoning=True, reasoning_param="qwen3")
    reasoning = model_reasoning("argonne", profile)
    assert reasoning["supported"] is True  # it reasons...
    assert reasoning["levels"] == []  # ...but reasoning_effort does nothing


def test_openai_levels_include_xhigh_only_where_reported() -> None:
    assert model_reasoning("openai", ModelProfile(id="gpt-5"))["levels"] == [
        "low",
        "medium",
        "high",
    ]
    assert "xhigh" in model_reasoning("openai", ModelProfile(id="gpt-5.1-codex-max"))["levels"]
    assert model_reasoning("openai", ModelProfile(id="gpt-4o"))["levels"] == []


def test_provider_without_a_thinking_mapping_offers_nothing() -> None:
    reasoning = model_reasoning("openrouter", ModelProfile(id="x", is_reasoning=True))
    assert reasoning["levels"] == []
    assert reasoning["source"] == "no_thinking_mapping"


@pytest.mark.parametrize(
    ("kind", "profile"),
    [
        ("codex", _codex_profile(["none", "low", "medium", "high", "xhigh"])),
        ("claude_code", ModelProfile(id="haiku")),
        ("anthropic", ModelProfile(id="claude-sonnet-4-5")),
        ("argonne", ModelProfile(id="openai/gpt-oss-20b", reasoning_param="openai_gptoss")),
        ("openai", ModelProfile(id="gpt-5.1-codex-max")),
    ],
)
def test_every_offered_level_is_mapped_by_resolve_thinking(
    kind: str, profile: ModelProfile
) -> None:
    levels = model_reasoning(kind, profile)["levels"]
    assert levels
    for level in levels:
        assert level in accepted_levels(kind)
        assert resolve_thinking(kind, level, 0).supported


def test_xhigh_maps_where_the_transport_has_it_and_is_typed_elsewhere() -> None:
    assert resolve_thinking("codex", "xhigh", 0).litellm_kwargs == {
        "codex_reasoning_effort": "xhigh"
    }
    assert resolve_thinking("openai", "xhigh", 0).litellm_kwargs == {"reasoning_effort": "xhigh"}
    plan = resolve_thinking("claude_code", "xhigh", 0)
    assert plan.supported is False
    assert plan.effective_level == "unsupported"
    assert "xhigh" in (plan.unsupported_reason or "")
    assert resolve_thinking("argonne", "xhigh", 0).supported is False


def test_config_accepts_xhigh() -> None:
    cfg = LMProviderConfig(provider="codex", model="gpt-5.6-sol", thinking_level="xhigh")
    assert cfg.thinking_level == "xhigh"


def test_catalog_row_carries_the_model_levels() -> None:
    from clio_agent.gact.provider_catalog import model_catalog_row
    from clio_agent.gact.types import LMProviderPreset
    from clio_agent.providers.handshake.model import (
        AuthState,
        ConnectivityState,
        HandshakeReport,
    )

    profile = ModelProfile(
        id="openai/gpt-oss-120b", is_reasoning=True, reasoning_param="openai_gptoss"
    )
    report = HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models=(profile,),
    )
    preset = LMProviderPreset(
        id="argonne_metis",
        label="ALCF Metis",
        provider="argonne",
        api_base="https://x",
        suggested_model="",
    )
    assert model_catalog_row(preset, report, profile)["reasoning"] == {
        "supported": True,
        "parameter": "openai_gptoss",
        "levels": ["low", "medium", "high"],
        "default": "medium",
        "source": "served_model_reasoning_parser",
    }


def test_codex_overlay_efforts_reach_the_catalog_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI catalog handshake forwards persisted efforts into the profile."""
    import asyncio

    from clio_agent.providers.handshake import HandshakeContext
    from clio_agent.providers.handshake.cli_catalog import CliCatalogHandshake

    def _overlay(*_args: object) -> dict[str, object]:
        return {
            "models": [
                {
                    "id": "gpt-5.6-sol",
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                    "default_reasoning_effort": "high",
                }
            ],
            "generated_at": "2026-09-23T00:00:00+00:00",
        }

    monkeypatch.setattr("clio_agent.providers.model_discovery.overlay_models_wire", _overlay)
    hs = CliCatalogHandshake(provider=None)
    ctx = HandshakeContext(provider_id="codex", provider_kind="codex", api_base="codex://sdk")
    rows = asyncio.run(hs.discover_models(None, ctx))
    profile = asyncio.run(hs.discover_model_config(None, ctx, rows[0]))
    reasoning = model_reasoning("codex", profile)
    assert reasoning["levels"] == ["low", "medium", "high", "xhigh"]
    assert reasoning["default"] == "high"
