"""The GitHub-hosted model overlay is fetched, cached, and validated. There is
no packaged copy (coordinator decision D18): a cold start with no network is
the typed ``catalog_unavailable_offline`` state. These tests mirror
``test_claude_code_catalog.py``'s fetched_catalog-backed caching contract.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.fetched_catalog import FetchedCatalog
from clio_agent.providers.model_discovery import model_overlay_catalog

REPO_COMPILED_OVERLAY = Path(__file__).resolve().parents[2] / "catalogs" / "model-overlay.json"


def _response(payload: str, *, status: int = 200, etag: str = "") -> httpx.Response:
    headers = {"etag": etag} if etag else {}
    return httpx.Response(
        status,
        text=payload,
        headers=headers,
        request=httpx.Request("GET", model_overlay_catalog.MODEL_OVERLAY_CATALOG_URL),
    )


@pytest.fixture
def isolated_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FetchedCatalog:
    """Point the module's catalog singleton at a per-test disk cache (no bundled fallback)."""
    fresh: FetchedCatalog = FetchedCatalog(
        "model-overlay-test",
        model_overlay_catalog.MODEL_OVERLAY_CATALOG_URL,
        parse=model_overlay_catalog._parse_catalog,
        ttl_s=model_overlay_catalog.DEFAULT_TTL_S,
        max_bytes=model_overlay_catalog._MAX_BYTES,
        timeout_s=model_overlay_catalog._FETCH_TIMEOUT_S,
        cache_path=tmp_path / "model-overlay.json",
    )
    monkeypatch.setattr(model_overlay_catalog, "_CATALOG", fresh)
    return fresh


def test_committed_catalog_file_parses_via_real_validator() -> None:
    """The repo-shipped catalogs/model-overlay.json must itself be valid."""
    catalog = model_overlay_catalog._parse_catalog(REPO_COMPILED_OVERLAY.read_bytes())
    families = {row["family"] for row in catalog.entries}
    assert "qwen3.6-27b" in families
    assert {"gemma-3", "gemma-4-e4b", "meta-llama-3.2-vision", "mistral-large-3"} <= families
    assert len(catalog.entries) == 20


def test_cold_start_with_no_disk_cache_and_no_network_is_the_typed_offline_state(
    isolated_catalog: FetchedCatalog,
) -> None:
    """No packaged copy: a first offline run has no overlay, and says so."""
    entries, error = model_overlay_catalog.cached_model_overlay_entries()
    assert entries is None
    assert error.startswith("catalog_unavailable_offline")
    with pytest.raises(fetched_catalog.FetchedCatalogUnavailable) as error_info:
        isolated_catalog.get(allow_fetch=False)
    assert error_info.value.reason == "catalog_unavailable_offline"


def test_overlay_url_is_the_raw_github_main_catalog() -> None:
    assert model_overlay_catalog.MODEL_OVERLAY_CATALOG_URL == (
        "https://raw.githubusercontent.com/iowarp/clio-agent/main/catalogs/model-overlay.json"
    )


def test_first_read_fetches_then_ttl_serves_disk_cache(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    calls: list[str] = []

    def _get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(
            '{"schema_version":1,"entries":['
            '{"family":"f","matchPatterns":["f"],"capabilities":{"chat":true}}]}'
        )

    monkeypatch.setattr(fetched_catalog.httpx, "get", _get)
    first = isolated_catalog.get()
    second = isolated_catalog.get()

    assert first.data.entries[0]["family"] == "f"
    assert second.source == "disk_cache"
    assert calls == [model_overlay_catalog.MODEL_OVERLAY_CATALOG_URL]


def test_a_failed_fetch_never_clears_the_last_good_disk_cache(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    good_payload = (
        '{"schema_version":1,"entries":['
        '{"family":"good","matchPatterns":["good"],"capabilities":{"chat":true}}]}'
    )
    monkeypatch.setattr(fetched_catalog.httpx, "get", lambda *a, **k: _response(good_payload))
    first = isolated_catalog.get()
    assert first.data.entries[0]["family"] == "good"

    def _boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("network is down")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    second = isolated_catalog.get(force_refresh=True)
    assert second.data.entries[0]["family"] == "good"
    assert second.stale_reason.startswith("transport_error")


def test_an_empty_entries_list_is_a_valid_if_useless_document() -> None:
    model_overlay_catalog._parse_catalog(b'{"schema_version":1,"entries":[]}')


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":2,"entries":[{"family":"f","matchPatterns":["f"],"capabilities":{}}]}',
        '{"schema_version":1,"entries":[{"family":"f","capabilities":{}}]}',  # missing matchPatterns
        '{"schema_version":1,"entries":[{"family":"f","matchPatterns":[],"capabilities":{}}]}',
        '{"schema_version":1,"entries":"not-a-list"}',
        "not json at all",
    ],
)
def test_malformed_payloads_are_rejected(payload: str) -> None:
    with pytest.raises(model_overlay_catalog.ModelOverlayCatalogError):
        model_overlay_catalog._parse_catalog(payload.encode())


def test_cached_entries_reads_disk_cache_without_touching_network(
    monkeypatch: pytest.MonkeyPatch, isolated_catalog: FetchedCatalog
) -> None:
    payload = (
        '{"schema_version":1,"entries":['
        '{"family":"cached","matchPatterns":["cached"],"capabilities":{}}]}'
    )
    monkeypatch.setattr(fetched_catalog.httpx, "get", lambda *a, **k: _response(payload))
    isolated_catalog.get()  # warm the disk cache

    def _boom(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("cached_model_overlay_entries must never touch the network")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    entries, error = model_overlay_catalog.cached_model_overlay_entries()
    assert error == ""
    assert entries is not None
    assert entries[0]["family"] == "cached"
