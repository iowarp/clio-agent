"""The handshake TTL cache is keyed by endpoint identity, not provider_kind.

``providers/handshake/cache.py`` caches a :class:`HandshakeReport` per
configured server. Two presets that share a catalog kind (openrouter and
llama.cpp both share "openai") must never share a cache entry, and a changed
``api_base`` for the same provider_id must miss the old entry rather than
serve stale evidence for a different server (model-capabilities brief Part 3).
"""

from __future__ import annotations

from clio_agent.providers.handshake import cache as handshake_cache
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
)


def _report(provider_id: str) -> HandshakeReport:
    return HandshakeReport(
        provider_id=provider_id,
        provider_kind="openai",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models_source="live",
    )


def test_two_same_kind_presets_never_share_a_cache_entry() -> None:
    """openrouter and llama_cpp share kind "openai" but must cache separately."""
    handshake_cache.invalidate()
    try:
        openrouter_key = handshake_cache.cache_key("openrouter", "https://openrouter.ai/api/v1")
        llama_cpp_key = handshake_cache.cache_key("llama_cpp", "http://127.0.0.1:8088/v1")
        handshake_cache.put_cached(openrouter_key, _report("openrouter"))
        handshake_cache.put_cached(llama_cpp_key, _report("llama_cpp"))

        assert handshake_cache.get_cached(openrouter_key).provider_id == "openrouter"  # type: ignore[union-attr]
        assert handshake_cache.get_cached(llama_cpp_key).provider_id == "llama_cpp"  # type: ignore[union-attr]
        assert openrouter_key != llama_cpp_key
    finally:
        handshake_cache.invalidate()


def test_a_changed_api_base_misses_the_old_cache_entry() -> None:
    """Moving a provider's endpoint must never serve the previous endpoint's report."""
    handshake_cache.invalidate()
    try:
        old_key = handshake_cache.cache_key("llama_cpp", "http://127.0.0.1:8088/v1")
        new_key = handshake_cache.cache_key("llama_cpp", "http://127.0.0.1:9000/v1")
        handshake_cache.put_cached(old_key, _report("llama_cpp"))

        assert handshake_cache.get_cached(old_key) is not None
        assert handshake_cache.get_cached(new_key) is None
    finally:
        handshake_cache.invalidate()


def test_cache_key_normalizes_equivalent_api_base_spellings() -> None:
    """A trailing slash / spelled-out default port must not create a false miss."""
    handshake_cache.invalidate()
    try:
        key_a = handshake_cache.cache_key("ollama", "http://127.0.0.1:11434/")
        key_b = handshake_cache.cache_key("ollama", "http://127.0.0.1:11434")
        handshake_cache.put_cached(key_a, _report("ollama"))

        assert key_a == key_b
        assert handshake_cache.get_cached(key_b) is not None
    finally:
        handshake_cache.invalidate()
