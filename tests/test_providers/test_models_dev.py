"""Tests for the models.dev source's real fetch/cache pipeline.

``test_handshake_sources.py`` exercises the id-matching/cascade logic against a
stubbed ``_load_models_dev`` (a captured fixture, no I/O at all). This file
exercises the REAL ``_load_models_dev``/``lookup_models_dev`` implementation,
which is now backed by the generic
:mod:`clio_agent.providers.fetched_catalog` mechanism: a disk cache with a TTL,
ETag, and last-good-never-cleared. Every test here mocks the network (or uses
the ``path=`` seam) so nothing depends on real connectivity.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.fetched_catalog import FetchedCatalog
from clio_agent.providers.handshake.sources import models_dev as models_dev_mod

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "handshake"
MODELS_DEV_FIXTURE = FIXTURE_DIR / "models_dev_subset.json"


def _response(payload: str, *, status: int = 200, etag: str = "") -> httpx.Response:
    headers = {"etag": etag} if etag else {}
    return httpx.Response(
        status, text=payload, headers=headers, request=httpx.Request("GET", "https://x")
    )


@pytest.fixture
def isolated_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FetchedCatalog:
    """A models.dev FetchedCatalog pointed at a per-test disk cache (real logic)."""
    fresh: FetchedCatalog = FetchedCatalog(
        "models-dev-test",
        models_dev_mod.MODELS_DEV_URL,
        parse=models_dev_mod._parse_catalog,
        ttl_s=models_dev_mod.DEFAULT_TTL_S,
        max_bytes=models_dev_mod._MAX_BYTES,
        timeout_s=models_dev_mod._FETCH_TIMEOUT_S,
        cache_path=tmp_path / "models-dev.json",
    )

    def _get_fresh(*, ttl_s: float) -> FetchedCatalog:
        fresh.ttl_s = ttl_s  # honor a per-call ttl_s override, same as the real _catalog()
        return fresh

    monkeypatch.setattr(models_dev_mod, "_catalog", _get_fresh)
    return fresh


def test_path_seam_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise AssertionError("path= must never fetch")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    catalog = models_dev_mod._load_models_dev(path=MODELS_DEV_FIXTURE)
    assert "google/gemma-4-31b-it" in catalog


def test_path_seam_missing_file_is_a_clean_miss(tmp_path: Path) -> None:
    assert models_dev_mod._load_models_dev(path=tmp_path / "nope.json") == {}


def test_live_fetch_populates_cache_and_ttl_reuses_it(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    calls = {"n": 0}

    def _get(url: str, **kwargs: object) -> httpx.Response:
        calls["n"] += 1
        return _response(json.dumps({"acme/foo": {"limit": {"context": 4096, "output": 1024}}}))

    monkeypatch.setattr(fetched_catalog.httpx, "get", _get)
    first = models_dev_mod._load_models_dev()
    second = models_dev_mod._load_models_dev()

    assert first["acme/foo"]["limit"]["context"] == 4096
    assert second == first
    assert calls["n"] == 1  # second read served from the fresh disk cache


def test_ttl_expiry_triggers_a_refetch(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    calls = {"n": 0}

    def _get(url: str, **kwargs: object) -> httpx.Response:
        calls["n"] += 1
        return _response(
            json.dumps({"acme/foo": {"limit": {"context": 1000 * calls["n"], "output": 1}}})
        )

    monkeypatch.setattr(fetched_catalog.httpx, "get", _get)
    first = models_dev_mod._load_models_dev(ttl_s=0.0)
    second = models_dev_mod._load_models_dev(ttl_s=0.0)

    assert first["acme/foo"]["limit"]["context"] == 1000
    assert second["acme/foo"]["limit"]["context"] == 2000
    assert calls["n"] == 2


def test_etag_304_refreshes_fetched_at_without_reparsing_new_data(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            json.dumps({"acme/foo": {"limit": {"context": 4096}}}), etag='"v1"'
        ),
    )
    first = isolated_catalog.get(force_refresh=True)
    assert first.etag == '"v1"'

    def _not_modified(
        url: str, *, headers: dict[str, str] | None = None, **kwargs: object
    ) -> httpx.Response:
        assert headers is not None and headers.get("If-None-Match") == '"v1"'
        return _response("", status=304)

    monkeypatch.setattr(fetched_catalog.httpx, "get", _not_modified)
    second = isolated_catalog.get(force_refresh=True)
    assert second.source == "network"
    assert second.stale_reason == ""
    assert second.data["acme/foo"]["limit"]["context"] == 4096
    assert second.fetched_at >= first.fetched_at


def test_fetch_failure_keeps_last_good_with_typed_reason(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(json.dumps({"acme/foo": {"limit": {"context": 4096}}})),
    )
    models_dev_mod._load_models_dev(ttl_s=0.0)

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    result = isolated_catalog.get(force_refresh=True)
    assert result.source == "disk_cache"
    assert result.stale_reason.startswith("transport_error")
    assert result.data["acme/foo"]["limit"]["context"] == 4096
    # The public lookup function must still serve the last-good copy, not {}.
    survived = models_dev_mod._load_models_dev(ttl_s=0.0)
    assert survived["acme/foo"]["limit"]["context"] == 4096


def test_validation_failure_keeps_last_good(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(json.dumps({"acme/foo": {"limit": {"context": 4096}}})),
    )
    models_dev_mod._load_models_dev(ttl_s=0.0)

    monkeypatch.setattr(fetched_catalog.httpx, "get", lambda *_a, **_kw: _response("[1, 2, 3]"))
    survived = models_dev_mod._load_models_dev(ttl_s=0.0)
    assert survived["acme/foo"]["limit"]["context"] == 4096


def test_total_miss_returns_empty_dict_not_a_raise(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    assert models_dev_mod._load_models_dev() == {}


def test_disabled_fetch_serves_disk_cache_only(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(json.dumps({"acme/foo": {"limit": {"context": 4096}}})),
    )
    models_dev_mod._load_models_dev()

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise AssertionError("allow_fetch=False must never hit the network")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    result = models_dev_mod._load_models_dev(allow_fetch=False)
    assert result["acme/foo"]["limit"]["context"] == 4096
