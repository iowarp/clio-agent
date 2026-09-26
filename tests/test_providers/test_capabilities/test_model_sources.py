"""Unit tests for :mod:`clio_agent.providers.capabilities.model_sources` (brief 5.1)."""

from __future__ import annotations

from clio_agent.providers.capabilities.model_sources import (
    community_catalog_facts,
    merge_model_layers,
    resolve_model_capabilities,
)
from clio_agent.providers.capabilities.records import Fact, ModelCapabilities, unknown

_NOW = "2026-01-01T00:00:00+00:00"


def _fact(value: object, source: str = "server_report") -> Fact:
    return Fact(value=value, source=source, observed_at=_NOW)


class _NullOverlaySource:
    """A fake overlay that always answers "no entry" -- for precedence tests
    that must not depend on the REAL overlay's seeded catalog content."""

    def facts(self, model_key: str) -> ModelCapabilities | None:
        del model_key
        return None


def test_merge_model_layers_first_known_layer_wins_per_field() -> None:
    user = ModelCapabilities(model_key="m", context_max=_fact(100_000, "user"))
    overlay = ModelCapabilities(
        model_key="m", context_max=_fact(50_000, "overlay"), tools=_fact(True, "overlay")
    )
    server = ModelCapabilities(
        model_key="m", tools=_fact(False, "server_report"), output_max=_fact(4096, "server_report")
    )

    merged = merge_model_layers("m", user, overlay, server)

    # user's context_max wins over overlay's (higher precedence, listed first).
    assert merged.context_max.value == 100_000
    assert merged.context_max.source == "user"
    # overlay's tools wins over server's (overlay listed before server).
    assert merged.tools.value is True
    assert merged.tools.source == "overlay"
    # output_max known only at the server tier.
    assert merged.output_max.value == 4096


def test_merge_model_layers_skips_none_layers() -> None:
    server = ModelCapabilities(model_key="m", tools=_fact(True))
    merged = merge_model_layers("m", None, None, server, None)
    assert merged.tools.value is True


def test_merge_model_layers_unknown_field_stays_unknown() -> None:
    merged = merge_model_layers("m", ModelCapabilities(model_key="m"))
    assert not merged.context_max.known
    assert not merged.tools.known


def test_merge_model_layers_stamps_the_given_model_key_not_the_layers() -> None:
    layer = ModelCapabilities(model_key="something-else", tools=_fact(True))
    merged = merge_model_layers("canonical-key", layer)
    assert merged.model_key == "canonical-key"


def test_merge_model_layers_skips_a_layer_field_that_is_unknown() -> None:
    """An unknown Fact on a higher layer does not shadow a known lower layer."""
    user = ModelCapabilities(model_key="m", context_max=unknown())
    server = ModelCapabilities(model_key="m", context_max=_fact(8192))
    merged = merge_model_layers("m", user, server)
    assert merged.context_max.value == 8192


def test_community_catalog_facts_resolves_a_known_model() -> None:
    facts = community_catalog_facts("gpt-4o-mini")
    assert facts is not None
    assert facts.context_max.known
    assert facts.context_max.source in {"models.dev", "litellm", "db"}


def test_community_catalog_facts_returns_none_for_a_total_miss() -> None:
    facts = community_catalog_facts("definitely-not-a-real-model-xyz-123")
    assert facts is None


def test_community_catalog_facts_returns_none_for_empty_id() -> None:
    assert community_catalog_facts("") is None


def test_resolve_model_capabilities_precedence_user_beats_everything() -> None:
    user_override = ModelCapabilities(model_key="gpt-4o-mini", context_max=_fact(999, "user"))
    result = resolve_model_capabilities(
        "gpt-4o-mini",
        user_override=user_override,
        server_report=ModelCapabilities(
            model_key="gpt-4o-mini", context_max=_fact(1, "server_report")
        ),
    )
    assert result.context_max.value == 999
    assert result.context_max.source == "user"


def test_resolve_model_capabilities_falls_through_to_community_catalog() -> None:
    result = resolve_model_capabilities("gpt-4o-mini")
    assert result.context_max.known  # resolved from the community-catalog tier
    assert result.context_max.source != "user"


def test_resolve_model_capabilities_default_overlay_is_a_noop_for_an_unmatched_model() -> None:
    """A model no seeded family's matchPatterns hits behaves as if no overlay ran."""
    with_null_overlay = resolve_model_capabilities("gpt-4o-mini", overlay=_NullOverlaySource())
    with_default_overlay = resolve_model_capabilities("gpt-4o-mini")
    assert with_default_overlay.context_max.value == with_null_overlay.context_max.value


def test_resolve_model_capabilities_default_overlay_is_the_real_p6_catalog() -> None:
    """P6 wiring: the default overlay is real and beats server_report (brief 5.1 order)."""
    result = resolve_model_capabilities(
        "qwen3.6-27b",
        server_report=ModelCapabilities(model_key="qwen3.6-27b", context_max=_fact(1)),
    )
    assert result.context_max.value == 262144
    assert result.context_max.source == "overlay"
