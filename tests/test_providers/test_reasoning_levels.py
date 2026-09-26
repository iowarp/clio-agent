"""Per-model reasoning levels in the provider catalog (I).

Each model advertises only the thinking levels its provider really reports for
it AND that ``resolve_thinking`` maps -- never a hard-coded list.
"""

from __future__ import annotations

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.providers.handshake.model import DiscoveredModel
from clio_agent.providers.reasoning_levels import model_reasoning
from clio_agent.providers.thinking import accepted_levels, resolve_thinking


def _codex_profile(efforts: list[str] | None, default: str = "") -> DiscoveredModel:
    raw: dict[str, object] = {"default_reasoning_effort": default}
    if efforts is not None:
        raw["supported_reasoning_efforts"] = efforts
    return DiscoveredModel(id="gpt-5.6-sol", raw=raw)


def test_codex_levels_come_from_the_maintained_catalog() -> None:
    reasoning = model_reasoning(
        "codex", _codex_profile(["minimal", "low", "medium", "high", "xhigh"], "medium")
    )
    # Every effort the SDK reports is offered -- minimal included, none dropped.
    assert reasoning["levels"] == ["minimal", "low", "medium", "high", "xhigh"]
    assert reasoning["default"] == "medium"
    assert reasoning["source"] == "codex_catalog"
    assert reasoning["supported"] is True


def test_codex_none_effort_is_the_off_level() -> None:
    reasoning = model_reasoning("codex", _codex_profile(["none", "low"], "none"))
    assert reasoning["levels"] == ["off", "low"]
    assert reasoning["default"] == "off"


def test_codex_without_reported_efforts_offers_nothing() -> None:
    reasoning = model_reasoning("codex", _codex_profile(None))
    assert reasoning["levels"] == []
    assert reasoning["supported"] is False
    assert reasoning["source"] == "codex_catalog_unreported"


def test_codex_offers_max_and_ultra_efforts_the_catalog_reports() -> None:
    """A model reporting 'max'/'ultra' (#1436) offers them -- not dropped as unmapped."""
    reasoning = model_reasoning(
        "codex", _codex_profile(["medium", "high", "xhigh", "max", "ultra"], "high")
    )
    assert reasoning["levels"] == ["medium", "high", "xhigh", "max", "ultra"]
    assert reasoning["default"] == "high"
    assert "reason" not in reasoning


_CLI_EFFORT = ["low", "medium", "high", "xhigh", "max"]


def test_claude_code_offers_the_cli_reported_effort_levels() -> None:
    fable = model_reasoning(
        "claude_code",
        DiscoveredModel(id="claude-fable-5-1", raw={"supported_effort_levels": _CLI_EFFORT}),
    )
    assert fable["levels"] == ["off", "low", "medium", "high", "xhigh", "max"]
    assert fable["source"] == "claude_code_sdk"
    assert fable["default"] == "high"  # the SDK documents high as its default effort
    sonnet = model_reasoning(
        "claude_code",
        DiscoveredModel(id="claude-sonnet-5", raw={"supported_effort_levels": _CLI_EFFORT}),
    )
    assert sonnet["default"] == "low"  # clio ships low for sonnet


def test_claude_code_model_without_effort_offers_the_thinking_budget() -> None:
    haiku = model_reasoning(
        "claude_code",
        DiscoveredModel(id="claude-haiku-4-5-20251001", raw={"supported_effort_levels": []}),
    )
    assert haiku["levels"] == ["off", "low", "medium", "high"]
    assert haiku["source"] == "claude_code_sdk_thinking_budget"


def test_claude_code_missing_effort_evidence_is_typed() -> None:
    row = {"effort_evidence_failure": "claude_code_cli_model_catalog_unavailable: boom"}
    block = model_reasoning("claude_code", DiscoveredModel(id="claude-sonnet-5", raw=row))
    assert block["levels"] == ["off", "low", "medium", "high"]
    assert block["reason"].startswith("claude_code_cli_model_catalog_unavailable")


def test_anthropic_adaptive_model_offers_output_config_effort() -> None:
    block = model_reasoning("anthropic", DiscoveredModel(id="claude-opus-4-7"))
    assert block["levels"] == ["off", "low", "medium", "high", "xhigh", "max"]
    assert block["source"] == "litellm_model_info_effort"
    assert model_reasoning("anthropic", DiscoveredModel(id="claude-sonnet-4-6"))["levels"] == [
        "off",
        "low",
        "medium",
        "high",
        "max",
    ]


def test_anthropic_levels_follow_litellm_model_info() -> None:
    thinking = model_reasoning("anthropic", DiscoveredModel(id="claude-sonnet-4-5"))
    assert thinking["levels"] == ["off", "low", "medium", "high"]
    assert thinking["default"] == "off"
    old = model_reasoning("anthropic", DiscoveredModel(id="claude-3-5-haiku-20241022"))
    assert old["levels"] == []
    assert old["supported"] is False


def test_alcf_gpt_oss_offers_low_medium_high() -> None:
    # is_reasoning/reasoning_param are now the CALLER's effective-capabilities
    # decision (brief 5.5), not flat profile fields -- passed as keywords.
    profile = DiscoveredModel(id="openai/gpt-oss-120b")
    reasoning = model_reasoning(
        "argonne", profile, is_reasoning=True, reasoning_param="openai_gptoss"
    )
    assert reasoning["levels"] == ["low", "medium", "high"]
    assert reasoning["default"] == "medium"
    assert reasoning["parameter"] == "openai_gptoss"


def test_alcf_reasoning_model_that_ignores_effort_offers_no_levels() -> None:
    profile = DiscoveredModel(id="Qwen/Qwen3-32B")
    reasoning = model_reasoning("argonne", profile, is_reasoning=True, reasoning_param="qwen3")
    assert reasoning["supported"] is True  # it reasons...
    assert reasoning["levels"] == []  # ...but reasoning_effort does nothing


def test_openai_levels_include_xhigh_only_where_reported() -> None:
    assert model_reasoning("openai", DiscoveredModel(id="gpt-5"))["levels"] == [
        "minimal",
        "low",
        "medium",
        "high",
    ]
    assert "xhigh" in model_reasoning("openai", DiscoveredModel(id="gpt-5.1-codex-max"))["levels"]
    assert model_reasoning("openai", DiscoveredModel(id="gpt-4o"))["levels"] == []


def test_provider_without_a_thinking_mapping_offers_nothing() -> None:
    reasoning = model_reasoning("openrouter", DiscoveredModel(id="x"), is_reasoning=True)
    assert reasoning["levels"] == []
    assert reasoning["source"] == "no_thinking_mapping"


@pytest.mark.parametrize(
    ("kind", "profile"),
    [
        ("codex", _codex_profile(["none", "low", "medium", "high", "xhigh"])),
        ("claude_code", DiscoveredModel(id="haiku")),
        ("claude_code", DiscoveredModel(id="opus", raw={"supported_effort_levels": _CLI_EFFORT})),
        ("anthropic", DiscoveredModel(id="claude-opus-4-7")),
        ("openai", DiscoveredModel(id="gpt-5")),
        ("anthropic", DiscoveredModel(id="claude-sonnet-4-5")),
        ("argonne", DiscoveredModel(id="openai/gpt-oss-20b")),
        ("openai", DiscoveredModel(id="gpt-5.1-codex-max")),
    ],
)
def test_every_offered_level_is_mapped_by_resolve_thinking(
    kind: str, profile: DiscoveredModel
) -> None:
    levels = model_reasoning(kind, profile)["levels"]
    assert levels
    from clio_agent.providers.reasoning_levels import model_effort_levels

    effort = model_effort_levels(kind, profile.id, raw=profile.raw)
    for level in levels:
        assert level in accepted_levels(kind)
        assert resolve_thinking(kind, level, 0, effort_levels=effort).supported


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

    # No handshake ran for this identity, so the effective capabilities have no
    # endpoint/deployment thinking-control evidence to choose a "parameter"
    # from (brief 5.5/7 -- that's now the CHOSEN wire control, not a raw
    # per-adapter parser name); "supported"/"levels" still come from the id-
    # based gpt-oss heuristic in reasoning_levels._served, independent of it.
    profile = DiscoveredModel(id="openai/gpt-oss-120b")
    report = HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base="https://x",
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
        "parameter": "",
        "levels": ["low", "medium", "high"],
        "default": "medium",
        "default_source": "provider",
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
    ctx = HandshakeContext(provider_id="codex", provider_kind="codex", api_base="codex://direct")
    rows = asyncio.run(hs.discover_models(None, ctx))
    facts = asyncio.run(hs.discover_model_config(None, ctx, rows[0]))
    reasoning = model_reasoning("codex", facts.discovered)
    assert reasoning["levels"] == ["low", "medium", "high", "xhigh"]
    assert reasoning["default"] == "high"


def test_claude_code_effort_maps_to_the_sdk_effort_option() -> None:
    plan = resolve_thinking("claude_code", "max", 0, effort_levels=_CLI_EFFORT)
    assert plan.sdk_thinking == {"type": "adaptive", "display": "summarized", "effort": "max"}
    off = resolve_thinking("claude_code", "off", 0, effort_levels=_CLI_EFFORT)
    assert off.sdk_thinking == {"type": "disabled"}
    budget = resolve_thinking("claude_code", "high", 0, effort_levels=None)
    assert budget.sdk_thinking == {
        "type": "enabled",
        "budget_tokens": 24576,
        "display": "summarized",
    }


def test_build_sdk_options_splits_effort_into_its_own_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    captured: dict[str, object] = {}

    class _Options:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules, "claude_agent_sdk", types.SimpleNamespace(ClaudeAgentOptions=_Options)
    )
    from clio_agent.providers.claude_code_options import build_sdk_options

    thinking = {"type": "adaptive", "display": "summarized", "effort": "xhigh"}
    build_sdk_options(model="sonnet", cwd=None, stream=False, thinking=thinking)
    assert captured["effort"] == "xhigh"
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert thinking["effort"] == "xhigh"  # the caller's (pool-key) dict is untouched


def test_anthropic_effort_maps_to_reasoning_effort() -> None:
    effort = ["low", "medium", "high", "max"]
    assert resolve_thinking("anthropic", "max", 0, effort_levels=effort).litellm_kwargs == {
        "reasoning_effort": "max"
    }
    assert resolve_thinking("anthropic", "xhigh", 0, effort_levels=effort).supported is False


def test_codex_minimal_and_openai_none_are_mapped() -> None:
    assert resolve_thinking("codex", "minimal", 0).litellm_kwargs == {
        "codex_reasoning_effort": "minimal"
    }
    plan = resolve_thinking("openai", "off", 0, effort_levels=["off", "low", "medium", "high"])
    assert plan.litellm_kwargs == {"reasoning_effort": "none"}


def test_claude_code_alias_resolves_to_its_overlay_row() -> None:
    from clio_agent.providers.model_discovery import ProviderDiscoveryResult, record_refresh
    from clio_agent.providers.reasoning_levels import model_effort_levels

    record_refresh(
        ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[
                {
                    "id": "claude-sonnet-5",
                    "name": "Sonnet",
                    "supported_effort_levels": _CLI_EFFORT,
                    "cli_values": ["sonnet"],
                }
            ],
            source="claude_code_catalog",
        )
    )
    assert model_effort_levels("claude_code", "sonnet") == tuple(_CLI_EFFORT)
    assert model_effort_levels("claude_code", "claude-sonnet-5") == tuple(_CLI_EFFORT)
    assert model_effort_levels("claude_code", "haiku") is None


def test_resolve_configured_model_id_follows_the_cli_alias() -> None:
    """A configured claude_code alias ('sonnet') resolves to its catalog id.

    GET /v1/providers/lm reports the configured alias as `model`; a client
    matching that value against the catalog's full model ids (e.g.
    'claude-sonnet-5') needs the same resolution the provider itself uses --
    not a hand-typed table -- to find the row's reasoning levels.
    """
    from clio_agent.providers.model_discovery import ProviderDiscoveryResult, record_refresh
    from clio_agent.providers.reasoning_levels import resolve_configured_model_id

    record_refresh(
        ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[
                {
                    "id": "claude-sonnet-5",
                    "name": "Sonnet",
                    "supported_effort_levels": _CLI_EFFORT,
                    "cli_values": ["sonnet"],
                }
            ],
            source="claude_code_catalog",
        )
    )
    assert resolve_configured_model_id("claude_code", "sonnet") == "claude-sonnet-5"
    # Already a real id: unchanged.
    assert resolve_configured_model_id("claude_code", "claude-sonnet-5") == "claude-sonnet-5"
    # Unknown alias / no discovery yet: falls back to the input unchanged.
    assert resolve_configured_model_id("claude_code", "haiku") == "haiku"
    # Non-aliasing providers: always unchanged.
    assert resolve_configured_model_id("codex", "gpt-5.6-sol") == "gpt-5.6-sol"
    assert resolve_configured_model_id("claude_code", "") == ""


def test_default_source_names_a_clio_shipped_default() -> None:
    effort = {"supported_effort_levels": ["low", "medium", "high", "xhigh", "max"]}
    sonnet = model_reasoning("claude_code", DiscoveredModel(id="claude-sonnet-5", raw=effort))
    assert (sonnet["default"], sonnet["default_source"]) == ("low", "clio_shipped")
    opus = model_reasoning("claude_code", DiscoveredModel(id="claude-opus-5", raw=effort))
    assert (opus["default"], opus["default_source"]) == ("high", "provider")
