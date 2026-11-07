"""Inception Mercury: the provider row, the model record and the request side.

* the ``inception`` catalog row (LiteLLM's native ``inception/`` route) and its
  typed ``show_diffusion`` option (choices are enforced, never coerced);
* the shipped ``mercury-3`` overlay entry: limits, Mercury's own effort spelling
  and the ``snapshot`` stream shape;
* the request: CLIO ``minimal`` is sent as Mercury's ``instant``; ``diffusing``
  is sent only when the option is on AND the model record states a snapshot
  stream -- on any route (the direct provider or OpenRouter), never by provider id;
  an option that cannot be honoured is logged with a typed reason.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from clio_agent.config import LMProviderConfig
from clio_agent.lm import diffusion
from clio_agent.lm.request_builder import build_request_kwargs
from clio_agent.providers.capabilities import invalidation, model_overlay
from clio_agent.providers.capabilities.accessor import clear_cache
from clio_agent.providers.capabilities.endpoint import build_endpoint_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
)
from clio_agent.providers.catalog import (
    SHOW_DIFFUSION_OPTION,
    as_lm_presets,
    get_provider,
    normalize_provider_options,
)

_REPO = Path(__file__).resolve().parents[2]
_MERCURY_YAML = _REPO / "catalogs" / "models" / "mercury-3.yaml"
_INCEPTION_BASE = "https://api.inceptionlabs.ai/v1"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_NOW = "2026-10-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset_capability_state():
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


def _mercury_entry() -> model_overlay.OverlayEntry:
    [raw] = yaml.safe_load(_MERCURY_YAML.read_text(encoding="utf-8"))
    entry = model_overlay._entry_from_raw(raw, root="fetched", source_name=_MERCURY_YAML.name)
    assert entry is not None
    return entry


def _mercury_caps(model_key: str) -> ModelCapabilities:
    return model_overlay.entry_to_model_capabilities(model_key, _mercury_entry(), "mercury-3")


def _record(
    provider_id: str, api_base: str, model_id: str, litellm: str, model: ModelCapabilities
) -> None:
    """What a handshake leaves for one deployment: endpoint + model + deployment."""

    invalidation.record_endpoint_capabilities(
        build_endpoint_capabilities(
            provider_id, api_base, litellm, model_id, custom_llm_provider=litellm
        )
    )
    invalidation.record_model_capabilities(model)
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            model_id=model_id,
            model_key=Fact(model.model_key, "server_report", _NOW),
        )
    )


def _inception(**kwargs: Any) -> LMProviderConfig:
    _record("inception", _INCEPTION_BASE, "mercury-3", "inception", _mercury_caps("mercury-3"))
    return LMProviderConfig(
        provider="openai",
        provider_id="inception",
        model="mercury-3",
        api_base=_INCEPTION_BASE,
        **kwargs,
    )  # type: ignore[arg-type]


def _body(extras: dict[str, Any]) -> dict[str, Any]:
    return dict(extras.get("extra_body") or {})


# --------------------------------------------------------------------------- #
# The provider row
# --------------------------------------------------------------------------- #


def test_inception_row_uses_litellms_native_route() -> None:
    row = get_provider("inception")
    assert row is not None
    assert row.litellm_prefix == "inception"
    assert row.api_base == _INCEPTION_BASE
    assert row.api_key_env == "INCEPTION_API_KEY"
    [field] = row.configuration_fields
    assert field.id == SHOW_DIFFUSION_OPTION
    assert field.options == ("off", "on")
    assert field.placeholder == "off"


def test_show_diffusion_is_offered_on_every_route_that_serves_mercury() -> None:
    for provider_id in ("inception", "openrouter"):
        row = get_provider(provider_id)
        assert row is not None
        assert SHOW_DIFFUSION_OPTION in {f.id for f in row.configuration_fields}, provider_id


def test_show_diffusion_choices_are_enforced() -> None:
    assert normalize_provider_options("inception", {SHOW_DIFFUSION_OPTION: "on"}) == {
        SHOW_DIFFUSION_OPTION: "on"
    }
    assert normalize_provider_options("inception", {SHOW_DIFFUSION_OPTION: ""}) == {}
    with pytest.raises(ValueError, match="expected one of off, on"):
        normalize_provider_options("inception", {SHOW_DIFFUSION_OPTION: "yes"})


def test_presets_carry_the_option_choices() -> None:
    preset = next(p for p in as_lm_presets() if p.id == "inception")
    [field] = preset.configuration_fields
    assert field.options == ["off", "on"]


# --------------------------------------------------------------------------- #
# The model record
# --------------------------------------------------------------------------- #


def test_mercury_overlay_states_limits_effort_and_snapshot_shape() -> None:
    caps = _mercury_caps("mercury-3")
    assert caps.stream_shape.value == "snapshot"
    assert caps.stream_shape.source == "overlay"
    assert caps.context_max.value == 1_000_000
    assert caps.output_max.value == 131_072
    assert caps.tools.value is True
    spec = caps.thinking.value
    assert spec is not None
    assert spec.mechanism == "effort_levels"
    assert dict(spec.effort_by_level)["minimal"] == "instant"


def test_compiled_overlay_carries_mercury(tmp_path: Path) -> None:
    import json

    compiled = json.loads((_REPO / "catalogs" / "model-overlay.json").read_text(encoding="utf-8"))
    text = json.dumps(compiled)
    assert '"family": "mercury-3"' in text
    assert '"streamShape": "snapshot"' in text


def test_a_malformed_stream_shape_stays_unknown(caplog) -> None:
    entry = model_overlay.OverlayEntry(
        family="odd",
        match_patterns=("odd",),
        capabilities={"streamShape": "sideways"},
        quirks={},
        root="fetched",
    )
    with caplog.at_level(logging.WARNING):
        caps = model_overlay.entry_to_model_capabilities("odd", entry, "odd")
    assert not caps.stream_shape.known
    assert "malformed_streamShape" in caplog.text


def test_an_entry_without_a_stream_shape_stays_unknown() -> None:
    entry = model_overlay.OverlayEntry(
        family="bare", match_patterns=("bare",), capabilities={}, quirks={}, root="fetched"
    )
    assert not model_overlay.entry_to_model_capabilities("bare", entry, "bare").stream_shape.known


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


def test_minimal_effort_is_sent_as_mercurys_instant() -> None:
    extras = build_request_kwargs(_inception(thinking_level="minimal"))
    assert _body(extras)["reasoning_effort"] == "instant"


def test_high_effort_passes_through() -> None:
    extras = build_request_kwargs(_inception(thinking_level="high"))
    assert _body(extras)["reasoning_effort"] == "high"


def test_diffusion_is_not_requested_unless_the_option_is_on() -> None:
    for options in ({}, {SHOW_DIFFUSION_OPTION: "off"}):
        extras = build_request_kwargs(_inception(provider_options=options))
        assert diffusion.DIFFUSING_FIELD not in _body(extras)
        assert SHOW_DIFFUSION_OPTION not in extras


def test_show_diffusion_on_requests_diffusing_beside_the_effort() -> None:
    extras = build_request_kwargs(
        _inception(thinking_level="low", provider_options={SHOW_DIFFUSION_OPTION: "on"})
    )
    body = _body(extras)
    assert body[diffusion.DIFFUSING_FIELD] is True
    assert body["reasoning_effort"] == "low"
    # The CLIO option itself never reaches the wire.
    assert SHOW_DIFFUSION_OPTION not in extras
    # The stream path reads the request back off the LM's own kwargs.
    assert diffusion.diffusion_requested(type("LM", (), {"kwargs": extras})())


def test_the_model_capability_not_the_provider_decides_the_route() -> None:
    """OpenRouter's ``inception/mercury-3`` gets ``diffusing`` from the same record."""

    _record(
        "openrouter",
        _OPENROUTER_BASE,
        "inception/mercury-3",
        "openrouter",
        _mercury_caps("inception/mercury-3"),
    )
    config = LMProviderConfig(
        provider="openai",
        provider_id="openrouter",
        model="inception/mercury-3",
        api_base=_OPENROUTER_BASE,
        provider_options={SHOW_DIFFUSION_OPTION: "on"},
    )  # type: ignore[arg-type]
    assert _body(build_request_kwargs(config))[diffusion.DIFFUSING_FIELD] is True


def test_an_option_the_model_cannot_honour_is_logged_typed(caplog) -> None:
    plain = ModelCapabilities(model_key="openai/gpt-4o-mini")
    _record("openrouter", _OPENROUTER_BASE, "openai/gpt-4o-mini", "openrouter", plain)
    config = LMProviderConfig(
        provider="openai",
        provider_id="openrouter",
        model="openai/gpt-4o-mini",
        api_base=_OPENROUTER_BASE,
        provider_options={SHOW_DIFFUSION_OPTION: "on"},
    )  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger=diffusion.__name__):
        extras = build_request_kwargs(config)
    assert diffusion.DIFFUSING_FIELD not in _body(extras)
    assert SHOW_DIFFUSION_OPTION not in extras
    assert "diffusion_not_sent" in caplog.text
    assert "reason=model_stream_shape_unknown" in caplog.text


def test_an_append_shaped_model_is_refused_with_its_own_reason(caplog) -> None:
    caps = ModelCapabilities(
        model_key="mercury-coder", stream_shape=Fact("append", "overlay", _NOW)
    )
    _record("inception", _INCEPTION_BASE, "mercury-coder", "inception", caps)
    config = LMProviderConfig(
        provider="openai",
        provider_id="inception",
        model="mercury-coder",
        api_base=_INCEPTION_BASE,
        provider_options={SHOW_DIFFUSION_OPTION: "on"},
    )  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger=diffusion.__name__):
        extras = build_request_kwargs(config)
    assert diffusion.DIFFUSING_FIELD not in _body(extras)
    assert "reason=model_stream_shape_append" in caplog.text
