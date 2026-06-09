"""Tests for handshake -> config feedback (resolve_effective_max_tokens + apply_handshake)."""

from __future__ import annotations

from clio_agent.config import LMProviderConfig, resolve_effective_max_tokens
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)


def _report(model_id: str, **kw: object) -> HandshakeReport:
    return HandshakeReport(
        provider_id="p",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models=(ModelProfile(id=model_id, **kw),),  # type: ignore[arg-type]
    )


# ---- resolve_effective_max_tokens precedence ----
def test_user_override_always_wins() -> None:
    assert (
        resolve_effective_max_tokens(
            user_max_tokens=5000, provider_default=4096, context_window=262144
        )
        == 5000
    )


def test_context_aware_big_window_uses_sane_cap() -> None:
    assert (
        resolve_effective_max_tokens(
            user_max_tokens=0, provider_default=4096, context_window=262144
        )
        == 32000
    )


def test_context_aware_small_window_reserves_prompt_budget() -> None:
    # 16384 - 8000 prompt budget = 8384, under the 32000 cap
    assert (
        resolve_effective_max_tokens(user_max_tokens=0, provider_default=4096, context_window=16384)
        == 8384
    )


def test_context_aware_tiny_window_floored() -> None:
    # 8192 - 8000 = 192 -> floored to 2048
    assert (
        resolve_effective_max_tokens(user_max_tokens=0, provider_default=4096, context_window=8192)
        == 2048
    )


def test_no_window_falls_back_to_static_default() -> None:
    assert (
        resolve_effective_max_tokens(user_max_tokens=0, provider_default=4096, context_window=None)
        == 4096
    )


# ---- apply_handshake ----
def test_apply_handshake_replaces_alcf_4096_with_context_aware() -> None:
    cfg = LMProviderConfig(provider="argonne", model="openai/gpt-oss-120b", api_key="x")
    assert cfg.max_tokens == 4096  # the ALCF static default (the bug)
    cfg.apply_handshake(
        _report("openai/gpt-oss-120b", context_window=65536, native_tool_calling=True),
        user_set_max_tokens=False,
    )
    assert cfg.context_window == 65536
    assert cfg.chosen_context == 65536
    assert cfg.native_tool_calling is True
    assert cfg.max_tokens == 32000  # was 4096


def test_apply_handshake_user_max_tokens_preserved() -> None:
    cfg = LMProviderConfig(
        provider="argonne", model="openai/gpt-oss-120b", api_key="x", max_tokens=12345
    )
    cfg.apply_handshake(
        _report("openai/gpt-oss-120b", context_window=65536), user_set_max_tokens=True
    )
    assert cfg.max_tokens == 12345  # user choice untouched
    assert cfg.chosen_context == 65536  # context still recorded


def test_apply_handshake_records_reasoning() -> None:
    cfg = LMProviderConfig(provider="argonne", model="nvidia/nemotron-3-super-120b", api_key="x")
    cfg.apply_handshake(
        _report(
            "nvidia/nemotron-3-super-120b",
            context_window=262144,
            is_reasoning=True,
            reasoning_param="super_v3",
        ),
        user_set_max_tokens=False,
    )
    assert cfg.is_reasoning is True
    assert cfg.reasoning_param == "super_v3"


def test_apply_handshake_uses_loaded_window_when_present() -> None:
    cfg = LMProviderConfig(provider="lm_studio", model="qwopus3.5-9b-v3", api_key="x")
    cfg.apply_handshake(
        _report(
            "qwopus3.5-9b-v3",
            context_window=262144,
            loaded_context_window=8192,
        ),
        user_set_max_tokens=False,
    )
    # effective window is the LOADED one (8192), so max_tokens is floored
    assert cfg.chosen_context == 8192


def test_apply_handshake_noop_when_no_profile_match() -> None:
    cfg = LMProviderConfig(provider="argonne", model="openai/gpt-oss-120b", api_key="x")
    before = cfg.max_tokens
    rep = HandshakeReport(
        provider_id="p",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models=(
            ModelProfile(id="other/a", context_window=1000),
            ModelProfile(id="other/b", context_window=2000),
        ),
    )
    cfg.apply_handshake(rep, user_set_max_tokens=False)
    assert cfg.max_tokens == before
    assert cfg.context_window is None
