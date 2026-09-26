"""Unit tests for the per-dialect ``ThinkingSpec`` sourcing modules
(model-capabilities brief Part 7 follow-up: codex/claude_code/anthropic/
openai thinking off the SAME real-data path every other dialect uses).

Covers ``providers.capabilities.dialects.codex``/``.claude_code``/
``.cloud_thinking`` in isolation (pure data -> ``Fact[ThinkingSpec]``, no
network, no handshake) -- the full end-to-end wire shape these facts feed is
covered by ``tests/test_providers/test_request_builder.py``'s per-dialect
wire tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.capabilities.dialects import claude_code, cloud_thinking, codex
from clio_agent.providers.fetched_catalog import FetchedCatalog
from clio_agent.providers.model_discovery import claude_code_catalog
from tests._catalog_seed import seed_litellm_cost_map

# --------------------------------------------------------------------------- #
# codex
# --------------------------------------------------------------------------- #


def test_codex_offers_every_sdk_reported_effort_including_max_and_ultra() -> None:
    """A model reporting 'max'/'ultra' (#1436) offers them -- not dropped as unmapped."""
    fact = codex.build_thinking_spec(
        {
            "supported_reasoning_efforts": ["medium", "high", "xhigh", "max", "ultra"],
        }
    )
    assert fact.known
    spec = fact.value
    assert spec is not None
    assert spec.mechanism == "effort_levels"
    assert spec.levels == ("medium", "high", "xhigh", "max", "ultra")
    assert spec.effort_by_level == {
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
        "ultra": "ultra",
    }


def test_codex_none_effort_maps_to_the_off_level() -> None:
    fact = codex.build_thinking_spec({"supported_reasoning_efforts": ["none", "low"]})
    assert fact.value is not None
    assert fact.value.levels == ("off", "low")
    assert fact.value.effort_by_level == {"off": "none", "low": "low"}


def test_codex_without_reported_efforts_is_unknown() -> None:
    fact = codex.build_thinking_spec({})
    assert not fact.known
    assert "supported_reasoning_efforts" in (fact.detail or "")


def test_codex_all_unmapped_efforts_is_unknown() -> None:
    fact = codex.build_thinking_spec({"supported_reasoning_efforts": ["not-a-real-effort"]})
    assert not fact.known


# --------------------------------------------------------------------------- #
# claude_code
# --------------------------------------------------------------------------- #


def test_claude_code_offers_the_cli_reported_effort_levels() -> None:
    fact = claude_code.build_thinking_spec(
        {"supported_effort_levels": ["low", "medium", "high", "xhigh", "max"]}
    )
    assert fact.value is not None
    assert fact.value.mechanism == "effort_levels"
    assert fact.value.levels == ("low", "medium", "high", "xhigh", "max")
    assert fact.source == "server_report"


def test_claude_code_model_without_effort_still_gets_a_real_budget_spec() -> None:
    """A model the CLI lists with no effort levels (haiku) still accepts
    thinking via a token budget -- never mechanism='none'."""
    fact = claude_code.build_thinking_spec({"supported_effort_levels": []})
    assert fact.value is not None
    assert fact.value.mechanism == "budget_tokens"
    assert fact.value.budget_range is None  # falls to the generic ladder


def test_claude_code_missing_effort_evidence_is_typed_unknown() -> None:
    fact = claude_code.build_thinking_spec(
        {"effort_evidence_failure": "claude_code_cli_model_catalog_unavailable: boom"}
    )
    assert not fact.known
    assert "claude_code_cli_model_catalog_unavailable" in (fact.detail or "")


def test_claude_code_no_evidence_at_all_is_unknown() -> None:
    fact = claude_code.build_thinking_spec({})
    assert not fact.known


def test_claude_code_shipped_default_effort_reads_the_raw_field() -> None:
    assert claude_code.shipped_default_effort({"shipped_default_effort": "low"}) == "low"
    assert claude_code.shipped_default_effort({}) == ""


def test_resolve_configured_model_id_follows_the_cli_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured claude_code alias ('sonnet') resolves to its catalog id --
    the SAME resolution the provider itself uses, never a hand-typed table.

    Patches ``read_overlay`` directly (never writes the real user-data-dir
    overlay file) so this stays hermetic.
    """
    overlay = {
        "claude_code": {
            "models": [
                {"id": "claude-sonnet-5", "name": "Sonnet", "cli_values": ["sonnet"]},
            ]
        }
    }
    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.overlay.read_overlay", lambda: overlay
    )
    assert claude_code.resolve_configured_model_id("sonnet") == "claude-sonnet-5"
    # Already a real id: unchanged.
    assert claude_code.resolve_configured_model_id("claude-sonnet-5") == "claude-sonnet-5"
    # Unknown alias / no matching row: falls back to the input unchanged.
    assert claude_code.resolve_configured_model_id("haiku") == "haiku"
    assert claude_code.resolve_configured_model_id("") == ""


@pytest.fixture
def isolated_claude_code_disk_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> FetchedCatalog:
    """Point the maintained-catalog singleton at a per-test disk cache (never
    the real network or another test's cache file) -- same pattern as
    ``test_claude_code_catalog.py``'s ``isolated_catalog`` fixture.
    """
    fresh = FetchedCatalog(
        "claude-code-models-test",
        claude_code_catalog.CLAUDE_CODE_CATALOG_URL,
        parse=claude_code_catalog._parse_catalog,
        ttl_s=claude_code_catalog.DEFAULT_TTL_S,
        max_bytes=claude_code_catalog._MAX_BYTES,
        timeout_s=claude_code_catalog._FETCH_TIMEOUT_S,
        cache_path=tmp_path / "claude-code-models.json",
    )
    monkeypatch.setattr(claude_code_catalog, "_CATALOG", fresh)
    return fresh


def test_shipped_default_effort_for_model_reads_the_maintained_catalog(
    monkeypatch: pytest.MonkeyPatch,
    isolated_claude_code_disk_catalog: FetchedCatalog,
) -> None:
    import httpx

    payload = (
        '{"schema_version":1,"models":['
        '{"id":"claude-sonnet-5","name":"Sonnet","shipped_default_effort":"low"},'
        '{"id":"claude-opus-5","name":"Opus"}'
        "]}"
    )
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: httpx.Response(
            200,
            text=payload,
            request=httpx.Request("GET", claude_code_catalog.CLAUDE_CODE_CATALOG_URL),
        ),
    )
    claude_code_catalog.refresh_claude_code_catalog()

    assert claude_code.shipped_default_effort_for_model("claude-sonnet-5") == "low"
    # A model the catalog lists with no shipped default: empty, never guessed.
    assert claude_code.shipped_default_effort_for_model("claude-opus-5") == ""
    # A model the catalog doesn't list at all: empty.
    assert claude_code.shipped_default_effort_for_model("claude-unknown-9") == ""


def test_shipped_default_effort_for_model_with_no_cache_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No prior successful fetch and no network: never guesses, never raises."""

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("shipped_default_effort_for_model must never hit the network")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    assert claude_code.shipped_default_effort_for_model("claude-sonnet-5") == ""


# --------------------------------------------------------------------------- #
# anthropic / openai (LiteLLM introspection)
# --------------------------------------------------------------------------- #


def test_anthropic_adaptive_model_offers_output_config_effort() -> None:
    fact = cloud_thinking.build_thinking_spec_anthropic("claude-opus-4-7")
    assert fact.value is not None
    assert fact.value.mechanism == "effort_levels"
    assert set(fact.value.levels) == {"low", "medium", "high", "xhigh", "max"}
    assert fact.source == "litellm"


def test_anthropic_non_adaptive_model_falls_back_to_a_budget_spec() -> None:
    fact = cloud_thinking.build_thinking_spec_anthropic("claude-3-5-haiku-20241022")
    assert fact.value is not None
    assert fact.value.mechanism == "budget_tokens"
    assert fact.source == "litellm"


def test_openai_reasoning_model_offers_effort_levels() -> None:
    seed_litellm_cost_map()
    fact = cloud_thinking.build_thinking_spec_openai("gpt-5")
    assert fact.value is not None
    assert fact.value.mechanism == "effort_levels"
    assert "medium" in fact.value.levels


def test_openai_non_reasoning_model_is_unknown() -> None:
    fact = cloud_thinking.build_thinking_spec_openai("gpt-4o")
    assert not fact.known
