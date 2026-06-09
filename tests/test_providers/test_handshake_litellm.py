"""Tests for the LiteLLM model-info source.

LiteLLM is clio's actual runtime (DSPy calls models through it), and it ships a
bundled catalog with no network access — so the real-catalog tests assert on stable
facts for well-known models. The variant/miss logic is exercised with a mocked
``litellm.get_model_info``.
"""

from __future__ import annotations

import litellm
import pytest

from clio_agent.providers.handshake.sources import litellm_catalog as lc


# ---- real bundled catalog (offline-safe, ships with the litellm package) ----
def test_real_catalog_gpt4o() -> None:
    assert lc.lookup_litellm_context("gpt-4o") == 128000
    assert (lc.lookup_litellm_output("gpt-4o") or 0) > 0


def test_real_catalog_cloud_anthropic() -> None:
    # the whole point: a cloud model whose /models API reports no context
    assert lc.lookup_litellm_context("claude-sonnet-4-5") == 200000


def test_real_catalog_miss_and_empty() -> None:
    assert lc.lookup_litellm("acme/nonexistent-zzz-99") == (None, None)
    assert lc.lookup_litellm("") == (None, None)
    assert lc.lookup_litellm("   ") == (None, None)


# ---- id-variant probing + prefix fall-through (mocked) ----
def test_variants_include_provider_prefixes() -> None:
    variants = lc._id_variants("claude-x")
    assert "claude-x" in variants
    assert "anthropic/claude-x" in variants
    assert "openai/claude-x" in variants


def test_prefix_resolves_when_bare_id_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(candidate: str) -> dict[str, int]:
        if candidate == "anthropic/foo-model":
            return {"max_input_tokens": 50000, "max_output_tokens": 4096}
        raise KeyError(candidate)  # unmapped -> clean miss

    monkeypatch.setattr(litellm, "get_model_info", fake_get)
    assert lc.lookup_litellm("foo-model") == (50000, 4096)


def test_get_model_info_raises_is_clean_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(candidate: str) -> dict[str, int]:
        raise KeyError(candidate)

    monkeypatch.setattr(litellm, "get_model_info", boom)
    assert lc.lookup_litellm("anything") == (None, None)


def test_zero_and_bool_values_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda candidate: {"max_input_tokens": 0, "max_output_tokens": True},
    )
    assert lc.lookup_litellm("x") == (None, None)


def test_max_tokens_fallback_for_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # some entries only carry max_tokens (no max_input_tokens) -> used as context
    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda candidate: {"max_tokens": 32768, "max_output_tokens": 8192},
    )
    assert lc.lookup_litellm("x") == (32768, 8192)
