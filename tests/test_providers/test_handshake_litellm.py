"""Tests for the LiteLLM community model-cost-map source.

LiteLLM is clio's actual runtime (DSPy calls models through it), and clio now
fetches the SAME community map LiteLLM itself uses, through the generic
:mod:`clio_agent.providers.fetched_catalog` mechanism. Every test here forces
``allow_fetch=False`` (or points the underlying catalog at a fixture) so the
suite never depends on network access -- the bundled wheel snapshot is the
guaranteed-present offline fallback, exactly like before the refactor.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.fetched_catalog import FetchedCatalog
from clio_agent.providers.handshake.sources import litellm_catalog as lc


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FetchedCatalog:
    """Point the module's catalog singleton at a per-test disk cache.

    Keeps the real bundled-fallback wiring (so the offline tests below still
    exercise it) while isolating each test's disk cache from the real one and
    from other tests.
    """
    fresh: FetchedCatalog = FetchedCatalog(
        "litellm-model-cost-map-test",
        lc._cost_map_url(),
        parse=lc._parse_cost_map,
        ttl_s=lc.DEFAULT_TTL_S,
        max_bytes=lc._MAX_BYTES,
        timeout_s=lc._FETCH_TIMEOUT_S,
        bundled=lc._bundled_model_cost_map,
        cache_path=tmp_path / "litellm-model-cost-map.json",
    )
    monkeypatch.setattr(lc, "_catalog", lambda: fresh)
    return fresh


# ---- bundled catalog, offline (no cache, network disabled -> bundled fallback) ----
def test_real_catalog_gpt4o() -> None:
    assert lc.lookup_litellm_context("gpt-4o", allow_fetch=False) == 128000
    assert (lc.lookup_litellm_output("gpt-4o", allow_fetch=False) or 0) > 0


def test_real_catalog_cloud_anthropic() -> None:
    # the whole point: a cloud model whose /models API reports no context
    assert lc.lookup_litellm_context("claude-sonnet-4-5", allow_fetch=False) == 200000


def test_real_catalog_miss_and_empty() -> None:
    assert lc.lookup_litellm("acme/nonexistent-zzz-99", allow_fetch=False) == (None, None)
    assert lc.lookup_litellm("", allow_fetch=False) == (None, None)
    assert lc.lookup_litellm("   ", allow_fetch=False) == (None, None)


def test_offline_lookup_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise AssertionError("allow_fetch=False must never hit the network")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    assert lc.lookup_litellm_context("gpt-4o", allow_fetch=False) == 128000


def test_offline_lookup_reports_bundled_provenance(isolated_catalog: FetchedCatalog) -> None:
    result = isolated_catalog.get(allow_fetch=False)
    assert result.source == "bundled"
    assert result.stale_reason == "bundled_cold_start"
    assert result.version == "bundled"


# ---- id-variant probing + prefix fall-through ----
def test_variants_include_provider_prefixes() -> None:
    variants = lc._id_variants("claude-x")
    assert "claude-x" in variants
    assert "anthropic/claude-x" in variants
    assert "openai/claude-x" in variants


def test_prefix_resolves_when_bare_id_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lc,
        "_cost_map",
        lambda *, allow_fetch=True: {
            "anthropic/foo-model": {"max_input_tokens": 50000, "max_output_tokens": 4096}
        },
    )
    assert lc.lookup_litellm("foo-model", allow_fetch=False) == (50000, 4096)


def test_zero_and_bool_values_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lc,
        "_cost_map",
        lambda *, allow_fetch=True: {"x": {"max_input_tokens": 0, "max_output_tokens": True}},
    )
    assert lc.lookup_litellm("x", allow_fetch=False) == (None, None)


def test_max_tokens_fallback_for_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # some entries only carry max_tokens (no max_input_tokens) -> used as context
    monkeypatch.setattr(
        lc,
        "_cost_map",
        lambda *, allow_fetch=True: {"x": {"max_tokens": 32768, "max_output_tokens": 8192}},
    )
    assert lc.lookup_litellm("x", allow_fetch=False) == (32768, 8192)


# ---- live fetch + cache path (mocked network, no real I/O) ----
def _response(payload: str, *, status: int = 200, etag: str = "") -> httpx.Response:
    headers = {"etag": etag} if etag else {}
    return httpx.Response(
        status, text=payload, headers=headers, request=httpx.Request("GET", "https://x")
    )


def test_live_fetch_populates_cache_and_is_reused(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    calls = {"n": 0}

    def _get(url: str, **kwargs: object) -> httpx.Response:
        calls["n"] += 1
        return _response('{"gpt-9": {"max_input_tokens": 999000, "max_output_tokens": 32000}}')

    monkeypatch.setattr(fetched_catalog.httpx, "get", _get)
    first = lc.lookup_litellm_context("gpt-9")
    second = lc.lookup_litellm_context("gpt-9")

    assert first == 999000
    assert second == 999000
    assert calls["n"] == 1  # second read served from the fresh disk cache


def test_fetch_failure_falls_back_to_last_good_with_stale_reason(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"gpt-9": {"max_input_tokens": 999000, "max_output_tokens": 32000}}'
        ),
    )
    good = isolated_catalog.get(force_refresh=True)
    assert good.source == "network"
    assert good.stale_reason == ""

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    survived = isolated_catalog.get(force_refresh=True)
    assert survived.source == "disk_cache"
    assert survived.stale_reason.startswith("transport_error")
    assert survived.data["gpt-9"]["max_input_tokens"] == 999000


def test_validation_failure_falls_back_to_last_good(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"gpt-9": {"max_input_tokens": 999000, "max_output_tokens": 32000}}'
        ),
    )
    good = isolated_catalog.get(force_refresh=True)
    assert good.source == "network"

    monkeypatch.setattr(
        fetched_catalog.httpx, "get", lambda *_a, **_kw: _response("not json at all")
    )
    survived = isolated_catalog.get(force_refresh=True)
    assert survived.source == "disk_cache"
    assert survived.stale_reason == "validation_failed"
    assert survived.data["gpt-9"]["max_input_tokens"] == 999000
