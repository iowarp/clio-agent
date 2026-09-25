"""Tests for handshake -> config feedback (resolve_effective_max_tokens + apply_handshake).

``apply_handshake`` now folds the EFFECTIVE capabilities (model-capabilities
brief 5.5) for ``(provider_id, api_base, model_id)`` rather than reading a flat
per-provider profile off the report. ``_seed_and_report`` builds a
:class:`HandshakeReport` the same way a real handshake would: it writes
``ModelCapabilities``/``DeploymentCapabilities`` into the shared capability
store (:mod:`clio_agent.providers.capabilities.invalidation`) and returns a
report carrying only the bare discovered identity, exactly what
``ProviderHandshake`` produces today.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from clio_agent import conf
from clio_agent.config import LMProviderConfig, resolve_effective_max_tokens
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    unknown,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    HandshakeReport,
)

_PROVIDER_ID = "p"
_API_BASE = ""
_NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clear_capability_store():
    invalidation.clear_all()
    yield
    invalidation.clear_all()


def _seed_and_report(
    model_id: str,
    *,
    context_max: int | None = None,
    context_served: int | None = None,
    output_limit: int | None = None,
    native_tool_calling: bool = False,
    is_reasoning: bool = False,
    provider_id: str = _PROVIDER_ID,
    api_base: str = _API_BASE,
) -> HandshakeReport:
    """Seed the capability store for ``model_id`` and return the matching report.

    ``context_max`` is the model's own ceiling (e.g. an offline catalog's
    published max, or a server-reported model max); ``context_served`` is what
    THIS deployment is actually serving (e.g. vLLM ``max_model_len``, LM
    Studio's ``loaded_context_length``) -- the two combine per brief 5.5 as
    ``min(context_max, context_served)``.
    """
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key=model_id,
            context_max=Fact(value=context_max, source="litellm", observed_at=_NOW)
            if context_max is not None
            else unknown(),
            output_max=Fact(value=output_limit, source="server_report", observed_at=_NOW)
            if output_limit is not None
            else unknown(),
            tools=Fact(value=True, source="server_report", observed_at=_NOW)
            if native_tool_calling
            else unknown(),
            thinking=Fact(
                value=ThinkingSpec(mechanism="on_off"), source="server_report", observed_at=_NOW
            )
            if is_reasoning
            else unknown(),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            model_id=model_id,
            model_key=Fact(value=model_id, source="server_report", observed_at=_NOW),
            context_served=Fact(value=context_served, source="server_report", observed_at=_NOW)
            if context_served is not None
            else unknown(),
        )
    )
    return HandshakeReport(
        provider_id=provider_id,
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base=api_base,
        models=(DiscoveredModel(id=model_id),),
    )


# ---- resolve_effective_max_tokens precedence (output cap, no magic) ----
def test_user_override_always_wins() -> None:
    assert (
        resolve_effective_max_tokens(
            user_max_tokens=5000,
            provider_default=4096,
            output_limit=32768,
            context_window=262144,
        )
        == 5000
    )


def test_output_limit_is_the_default() -> None:
    assert (
        resolve_effective_max_tokens(
            user_max_tokens=0, provider_default=4096, output_limit=32768, context_window=262144
        )
        == 32768
    )


def test_output_limit_capped_to_context() -> None:
    # an output limit larger than the window is capped to the window
    assert (
        resolve_effective_max_tokens(
            user_max_tokens=0, provider_default=4096, output_limit=300000, context_window=131072
        )
        == 131072
    )


def test_no_output_limit_uses_provider_default() -> None:
    # no discovered output cap -> keep the provider default (no magic), capped to window
    assert (
        resolve_effective_max_tokens(
            user_max_tokens=0, provider_default=4096, output_limit=None, context_window=262144
        )
        == 4096
    )


def test_no_window_no_output_uses_provider_default() -> None:
    assert (
        resolve_effective_max_tokens(
            user_max_tokens=0, provider_default=4096, output_limit=None, context_window=None
        )
        == 4096
    )


# ---- apply_handshake ----
def test_apply_handshake_preserves_uncapped_output() -> None:
    cfg = LMProviderConfig(provider="argonne", model="openai/gpt-oss-120b", api_key="x")
    assert cfg.max_tokens == 0
    cfg.apply_handshake(
        _seed_and_report(
            "openai/gpt-oss-120b",
            context_served=65536,
            output_limit=32768,
            native_tool_calling=True,
        ),
        user_set_max_tokens=False,
    )
    assert cfg.context_window == 65536
    assert cfg.chosen_context == 65536
    assert cfg.native_tool_calling is True
    assert cfg.max_tokens == 0  # discovery must not introduce a client cap


def test_apply_handshake_no_output_limit_keeps_provider_default() -> None:
    cfg = LMProviderConfig(provider="argonne", model="openai/gpt-oss-120b", api_key="x")
    cfg.apply_handshake(
        _seed_and_report("openai/gpt-oss-120b", context_served=65536),  # no output_limit
        user_set_max_tokens=False,
    )
    assert cfg.chosen_context == 65536
    assert cfg.max_tokens == 0


def test_apply_handshake_user_max_tokens_preserved() -> None:
    cfg = LMProviderConfig(
        provider="argonne", model="openai/gpt-oss-120b", api_key="x", max_tokens=12345
    )
    cfg.apply_handshake(
        _seed_and_report("openai/gpt-oss-120b", context_served=65536), user_set_max_tokens=True
    )
    assert cfg.max_tokens == 12345  # user choice untouched
    assert cfg.chosen_context == 65536  # context still recorded


def test_apply_handshake_records_reasoning() -> None:
    """``is_reasoning`` reflects the model's thinking mechanism being known.

    ``reasoning_param`` is now the endpoint control CHOSEN to carry it (brief
    5.5/7), not an arbitrary parser name a deployment happened to report --
    with no endpoint record seeded here, nothing can be chosen, so it stays
    unset. The dialect-specific parser-naming concept the old flat field carried
    folds into Part 7's request builder, a later slice.
    """
    cfg = LMProviderConfig(provider="argonne", model="nvidia/nemotron-3-super-120b", api_key="x")
    cfg.apply_handshake(
        _seed_and_report(
            "nvidia/nemotron-3-super-120b",
            context_served=262144,
            is_reasoning=True,
        ),
        user_set_max_tokens=False,
    )
    assert cfg.is_reasoning is True
    assert cfg.reasoning_param is None


def test_apply_handshake_uses_loaded_window_when_present() -> None:
    cfg = LMProviderConfig(provider="lm_studio", model="qwopus3.5-9b-v3", api_key="x")
    cfg.apply_handshake(
        _seed_and_report(
            "qwopus3.5-9b-v3",
            context_max=262144,
            context_served=8192,
        ),
        user_set_max_tokens=False,
    )
    # effective window is the smaller (served/loaded) one, so max_tokens is floored
    assert cfg.chosen_context == 8192


def test_apply_handshake_noop_when_no_profile_match() -> None:
    cfg = LMProviderConfig(provider="argonne", model="openai/gpt-oss-120b", api_key="x")
    before = cfg.max_tokens
    rep = HandshakeReport(
        provider_id="p",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base="",
        models=(
            DiscoveredModel(id="other/a"),
            DiscoveredModel(id="other/b"),
        ),
    )
    cfg.apply_handshake(rep, user_set_max_tokens=False)
    assert cfg.max_tokens == before
    assert cfg.context_window is None


# ---- #1326: native_context_window carry + CLIO_LM_CONTEXT_WINDOW override ----


def test_apply_handshake_carries_native_context_window() -> None:
    """The model's own ceiling (context_max) is stamped onto the config."""
    cfg = LMProviderConfig(provider="argonne", model="ibm/granite-4.2-30b", api_key="x")
    with patch.dict("os.environ", {"CLIO_LM_CONTEXT_WINDOW": "0"}, clear=False):
        conf.reload()
        cfg.apply_handshake(
            _seed_and_report(
                "ibm/granite-4.2-30b",
                context_served=32768,
                context_max=131072,
            ),
        )
        conf.reload()
    assert cfg.native_context_window == 131072
    assert cfg.chosen_context == 32768  # served window, no override


def test_apply_handshake_context_window_env_override_wins() -> None:
    """CLIO_LM_CONTEXT_WINDOW >0 overrides the discovered window in chosen_context."""
    cfg = LMProviderConfig(provider="argonne", model="ibm/granite-4.2-30b", api_key="x")
    with patch.dict("os.environ", {"CLIO_LM_CONTEXT_WINDOW": "131072"}, clear=False):
        conf.reload()
        cfg.apply_handshake(
            _seed_and_report("ibm/granite-4.2-30b", context_served=32768),
        )
        conf.reload()
    assert cfg.chosen_context == 131072  # override wins
    assert cfg.context_window == 32768  # served window unchanged


def test_apply_handshake_zero_override_uses_discovered() -> None:
    """CLIO_LM_CONTEXT_WINDOW=0 (the default) falls back to the discovered window."""
    cfg = LMProviderConfig(provider="argonne", model="openai/gpt-oss-120b", api_key="x")
    with patch.dict("os.environ", {"CLIO_LM_CONTEXT_WINDOW": "0"}, clear=False):
        conf.reload()
        cfg.apply_handshake(
            _seed_and_report("openai/gpt-oss-120b", context_served=65536),
        )
        conf.reload()
    assert cfg.chosen_context == 65536


def test_apply_handshake_below_native_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A context_window_below_native warning fires when served < native."""
    import logging

    cfg = LMProviderConfig(provider="argonne", model="ibm/granite-4.2-30b", api_key="x")
    with caplog.at_level(logging.WARNING, logger="clio_agent.config"):
        with patch.dict("os.environ", {"CLIO_LM_CONTEXT_WINDOW": "0"}, clear=False):
            conf.reload()
            cfg.apply_handshake(
                _seed_and_report(
                    "ibm/granite-4.2-30b",
                    context_served=32768,
                    context_max=131072,
                ),
            )
            conf.reload()
    warning_records = [r for r in caplog.records if "context_window_below_native" in r.message]
    assert warning_records, "Expected context_window_below_native warning in log"
    assert "32768" in warning_records[0].message
    assert "131072" in warning_records[0].message


def test_apply_handshake_no_warning_when_served_equals_native(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No context_window_below_native warning when served == native."""
    import logging

    cfg = LMProviderConfig(provider="argonne", model="openai/gpt-oss-120b", api_key="x")
    with caplog.at_level(logging.WARNING, logger="clio_agent.config"):
        with patch.dict("os.environ", {"CLIO_LM_CONTEXT_WINDOW": "0"}, clear=False):
            conf.reload()
            cfg.apply_handshake(
                _seed_and_report(
                    "openai/gpt-oss-120b",
                    context_served=131072,
                    context_max=131072,
                ),
            )
            conf.reload()
    warning_records = [r for r in caplog.records if "context_window_below_native" in r.message]
    assert not warning_records, "Unexpected context_window_below_native warning"


# ---- #1326 followup: native_context_window populated from offline catalog ----


@pytest.mark.asyncio
async def test_native_context_window_populated_from_catalog_and_warning_fires(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end: a vLLM-shaped openai_compat payload with max_model_len below the
    model's offline catalog native max populates ``ModelCapabilities.context_max``
    in ``discover_model_config`` itself (an offline-only lookup, no network) and
    fires context_window_below_native through apply_handshake.

    Uses gpt-4o (native=128000 in LiteLLM catalog) served at max_model_len=16384.
    Confirms the served context (deployment.context_served) is NOT overwritten
    with the native value.
    """
    import logging

    from clio_agent.providers.handshake.base import HandshakeContext
    from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake

    # Fake async HTTP client — no network calls needed; only the /models row is used.
    class _NoNetClient:
        async def get(self, url: str, **_: object) -> None:
            raise AssertionError("No network call expected in discover_model_config")

    ctx = HandshakeContext(
        provider_id="vllm-test",
        provider_kind="vllm",
        api_base="http://localhost:8000/v1",
        allow_external_sources=False,
    )
    # Simulate a vLLM /models row: gpt-4o served with a reduced max_model_len.
    vllm_row = {"id": "gpt-4o", "object": "model", "max_model_len": 16384}

    handshake = OpenAICompatHandshake(provider=object())
    facts = await handshake.discover_model_config(_NoNetClient(), ctx, vllm_row)

    # The served window stays as vLLM reported it — native is never written back.
    assert facts.deployment.context_served.value == 16384
    # native (model.context_max) must come from the offline catalog (LiteLLM: gpt-4o=128000).
    assert facts.model.context_max.known, "context_max should be populated from the offline catalog"
    assert facts.model.context_max.value > facts.deployment.context_served.value

    invalidation.record_model_capabilities(facts.model)
    invalidation.record_deployment_capabilities(facts.deployment)

    # Now wire through apply_handshake and confirm the warning fires.
    cfg = LMProviderConfig(provider="vllm", model="gpt-4o")
    report = HandshakeReport(
        provider_id="vllm-test",
        provider_kind="vllm",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base="http://localhost:8000/v1",
        models=(facts.discovered,),
    )
    with caplog.at_level(logging.WARNING, logger="clio_agent.config"):
        with patch.dict("os.environ", {"CLIO_LM_CONTEXT_WINDOW": "0"}, clear=False):
            conf.reload()
            cfg.apply_handshake(report)
            conf.reload()
    warning_records = [r for r in caplog.records if "context_window_below_native" in r.message]
    assert warning_records, "Expected context_window_below_native warning to fire"
    # Served and native values appear in the warning for operator visibility.
    assert "16384" in warning_records[0].message
    assert str(facts.model.context_max.value) in warning_records[0].message
    # chosen_context reflects the served window, not the native max.
    assert cfg.chosen_context == 16384
