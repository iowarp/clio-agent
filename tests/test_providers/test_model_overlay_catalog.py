"""The GitHub-hosted model overlay is fetched, cached, validated, and has a
REAL bundled cold-start fallback (brief Part 8.1) -- unlike the Claude Code
catalog, which deliberately has none. These tests mirror
``test_claude_code_catalog.py``'s fetched_catalog-backed caching contract and
add the cold-start case that catalog doesn't need.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.fetched_catalog import FetchedCatalog
from clio_agent.providers.model_discovery import model_overlay_catalog

REPO_COMPILED_OVERLAY = Path(__file__).resolve().parents[2] / "catalogs" / "model-overlay.json"
BUNDLED_OVERLAY = model_overlay_catalog._BUNDLED_OVERLAY


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


@pytest.fixture
def isolated_catalog_with_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> FetchedCatalog:
    """Same as above, but WITH the real bundled fallback wired (the cold-start case)."""
    fresh: FetchedCatalog = FetchedCatalog(
        "model-overlay-test-bundled",
        model_overlay_catalog.MODEL_OVERLAY_CATALOG_URL,
        parse=model_overlay_catalog._parse_catalog,
        ttl_s=model_overlay_catalog.DEFAULT_TTL_S,
        max_bytes=model_overlay_catalog._MAX_BYTES,
        timeout_s=model_overlay_catalog._FETCH_TIMEOUT_S,
        bundled=model_overlay_catalog._load_bundled,
        cache_path=tmp_path / "model-overlay.json",
    )
    monkeypatch.setattr(model_overlay_catalog, "_CATALOG", fresh)
    return fresh


def test_committed_catalog_file_parses_via_real_validator() -> None:
    """The repo-shipped catalogs/model-overlay.json must itself be valid."""
    catalog = model_overlay_catalog._parse_catalog(REPO_COMPILED_OVERLAY.read_bytes())
    families = {row["family"] for row in catalog.entries}
    assert "qwen3.6-27b" in families
    assert len(catalog.entries) == 14


def test_bundled_copy_is_byte_identical_to_the_committed_catalog() -> None:
    """The compile script keeps both copies in sync (see scripts/compile_model_overlay.py)."""
    assert BUNDLED_OVERLAY.read_text(encoding="utf-8") == REPO_COMPILED_OVERLAY.read_text(
        encoding="utf-8"
    )


def test_cold_start_with_no_disk_cache_and_no_network_serves_the_bundled_copy(
    isolated_catalog_with_bundled: FetchedCatalog,
) -> None:
    """brief Part 8.1: 'the committed file as the cold-start bundled source'."""
    result = isolated_catalog_with_bundled.get(allow_fetch=False)
    assert result.source == "bundled"
    assert result.stale_reason == "bundled_cold_start"
    families = {row["family"] for row in result.data.entries}
    assert "qwen3.6-27b" in families


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
