"""Tests for the generic fetch -> disk-cache -> validate -> last-good pipeline.

This is the mechanism ``models_dev``, ``litellm_catalog``, and
``claude_code_catalog`` all now share (see ``docs`` in
:mod:`clio_agent.providers.fetched_catalog`). These tests exercise the class
directly, independent of any one caller: a fresh cache hit, TTL expiry, the
ETag 304 path, a fetch failure keeping last-good with a typed reason, a
validation failure keeping last-good, a cold start using the bundled source,
and the atomic write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.providers.fetched_catalog import (
    CatalogResult,
    FetchedCatalog,
    FetchedCatalogUnavailable,
)


def _response(payload: str, *, status: int = 200, etag: str = "") -> httpx.Response:
    headers = {"etag": etag} if etag else {}
    return httpx.Response(
        status,
        text=payload,
        headers=headers,
        request=httpx.Request("GET", "https://example/catalog.json"),
    )


def _parse_dict(payload: bytes) -> dict[str, Any]:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _catalog(
    tmp_path: Path,
    *,
    ttl_s: float = 3600.0,
    max_bytes: int = 1024,
    bundled: Any = None,
) -> FetchedCatalog[dict[str, Any]]:
    return FetchedCatalog(
        "widgets",
        "https://example/catalog.json",
        parse=_parse_dict,
        ttl_s=ttl_s,
        max_bytes=max_bytes,
        timeout_s=1.0,
        bundled=bundled,
        cache_path=tmp_path / "widgets.json",
    )


# ---- fresh cache hit ----
def test_fresh_cache_hit_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}'),
    )
    first = catalog.get()
    assert first.source == "network"

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise AssertionError("a fresh cache must not re-fetch")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _boom)
    second = catalog.get()
    assert second.source == "disk_cache"
    assert second.stale_reason == ""
    assert second.data == {"a": 1}
    assert second.etag == first.etag
    assert second.version == first.version


# ---- TTL expiry refetch ----
def test_ttl_expiry_triggers_a_refetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, ttl_s=0.0)
    calls = {"n": 0}

    def _get(*_a: object, **_kw: object) -> httpx.Response:
        calls["n"] += 1
        return _response(json.dumps({"n": calls["n"]}))

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _get)
    first = catalog.get()
    second = catalog.get()
    assert first.data == {"n": 1}
    assert second.data == {"n": 2}
    assert calls["n"] == 2


# ---- ETag 304 path ----
def test_etag_304_refreshes_fetched_at_and_keeps_the_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}', etag='"abc"'),
    )
    first = catalog.get(force_refresh=True)
    assert first.etag == '"abc"'
    assert first.version == 'etag:"abc"'

    seen_headers: dict[str, str] = {}

    def _conditional(
        url: str, *, headers: dict[str, str] | None = None, **kwargs: object
    ) -> httpx.Response:
        seen_headers.update(headers or {})
        return _response("", status=304)

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _conditional)
    second = catalog.get(force_refresh=True)
    assert seen_headers.get("If-None-Match") == '"abc"'
    assert second.source == "network"
    assert second.stale_reason == ""
    assert second.data == {"a": 1}
    assert second.fetched_at >= first.fetched_at


def test_304_with_no_prior_cache_is_a_miss(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response("", status=304),
    )
    with pytest.raises(FetchedCatalogUnavailable):
        catalog.get()


# ---- fetch failure keeps last-good with stale_reason ----
def test_transport_failure_keeps_last_good_with_typed_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path, ttl_s=0.0)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}'),
    )
    catalog.get()

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _boom)
    result = catalog.get()
    assert result.source == "disk_cache"
    assert result.data == {"a": 1}
    assert result.stale_reason.startswith("transport_error")


def test_http_error_status_keeps_last_good(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, ttl_s=0.0)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}'),
    )
    catalog.get()

    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response("server error", status=500),
    )
    result = catalog.get()
    assert result.source == "disk_cache"
    assert result.stale_reason == "http_status_500"


def test_oversized_response_keeps_last_good(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path, ttl_s=0.0, max_bytes=8)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}'),
    )
    catalog.get()

    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": "way too much data for the limit"}'),
    )
    result = catalog.get()
    assert result.source == "disk_cache"
    assert result.stale_reason.startswith("too_large")
    assert result.data == {"a": 1}


# ---- validation failure keeps last-good ----
def test_validation_failure_keeps_last_good(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path, ttl_s=0.0)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}'),
    )
    catalog.get()

    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response("[1, 2, 3]"),  # valid JSON, fails our `parse`
    )
    result = catalog.get()
    assert result.source == "disk_cache"
    assert result.stale_reason == "validation_failed"
    assert result.data == {"a": 1}
    # The disk cache itself must NOT have been overwritten with the bad payload.
    on_disk = json.loads(catalog.cache_path.read_text(encoding="utf-8"))
    assert json.loads(on_disk["payload"]) == {"a": 1}


# ---- cold start with no network uses bundled ----
def test_cold_start_with_no_cache_and_no_network_uses_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path, bundled=lambda: {"bundled": True})

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _boom)
    result = catalog.get()
    assert result.source == "bundled"
    assert result.stale_reason == "bundled_cold_start"
    assert result.data == {"bundled": True}
    assert result.version == "bundled"


def test_disabled_fetch_with_no_cache_uses_bundled(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, bundled=lambda: {"bundled": True})
    result = catalog.get(allow_fetch=False)
    assert result.source == "bundled"


def test_cold_start_with_no_bundled_and_no_network_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _boom)
    with pytest.raises(FetchedCatalogUnavailable):
        catalog.get()


def test_bad_bundled_loader_still_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _bad_bundled() -> dict[str, Any]:
        raise RuntimeError("packaged file missing")

    catalog = _catalog(tmp_path, bundled=_bad_bundled)

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _boom)
    with pytest.raises(FetchedCatalogUnavailable):
        catalog.get()


# ---- bundled is never preferred once ANY disk cache exists ----
def test_bundled_is_only_used_when_disk_cache_is_totally_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path, ttl_s=0.0, bundled=lambda: {"bundled": True})
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}'),
    )
    catalog.get()

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _boom)
    result = catalog.get()
    assert result.source == "disk_cache"
    assert result.data == {"a": 1}


# ---- atomic write ----
def test_write_is_atomic_no_tmp_file_left_and_content_is_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}', etag='"e1"'),
    )
    catalog.get()

    tmp_marker = catalog.cache_path.with_suffix(catalog.cache_path.suffix + ".tmp")
    assert not tmp_marker.exists()
    on_disk = json.loads(catalog.cache_path.read_text(encoding="utf-8"))
    assert set(on_disk) == {"fetched_at", "etag", "source_url", "version", "payload"}
    assert json.loads(on_disk["payload"]) == {"a": 1}

    # A second write (refresh) replaces the file wholesale -- still no leftover tmp,
    # still fully valid JSON (never a torn half-write).
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 2}', etag='"e2"'),
    )
    catalog.get(force_refresh=True)
    assert not tmp_marker.exists()
    on_disk_2 = json.loads(catalog.cache_path.read_text(encoding="utf-8"))
    assert json.loads(on_disk_2["payload"]) == {"a": 2}


def test_write_retries_a_transient_windows_sharing_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The disk-cache write goes through ``platform_paths.atomic_replace`` --
    the SAME helper ``model_discovery/overlay.py`` uses -- so it retries the
    exact transient Windows race (WinError 5/32) P4a saw there, rather than a
    second, non-retrying tmp+replace implementation."""
    import sys

    from clio_agent import platform_paths

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_paths.time, "sleep", lambda _seconds: None)
    calls: list[int] = []
    real_replace = platform_paths.os.replace

    def flaky_replace(src: str, dst: str) -> None:
        calls.append(1)
        if len(calls) < 2:
            exc = PermissionError("sharing violation")
            exc.winerror = 32  # type: ignore[attr-defined]
            raise exc
        real_replace(src, dst)

    monkeypatch.setattr(platform_paths.os, "replace", flaky_replace)

    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}', etag='"e1"'),
    )

    result = catalog.get()

    assert result.data == {"a": 1}
    assert len(calls) == 2
    assert json.loads(catalog.cache_path.read_text(encoding="utf-8"))["payload"] == '{"a": 1}'


def test_corrupt_disk_cache_is_treated_as_absent(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, bundled=lambda: {"bundled": True})
    catalog.cache_path.parent.mkdir(parents=True, exist_ok=True)
    catalog.cache_path.write_text("not json{{{", encoding="utf-8")
    result = catalog.get(allow_fetch=False)
    assert result.source == "bundled"


def test_provenance_round_trips_through_catalogresult() -> None:
    result = CatalogResult(
        data={"a": 1}, source="network", source_url="u", etag="e", version="v", fetched_at="t"
    )
    assert result.stale_reason == ""
    assert result.data == {"a": 1}


def test_empty_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FetchedCatalog(
            "", "https://x", parse=_parse_dict, ttl_s=1.0, cache_path=tmp_path / "x.json"
        )


# ---- repeated reads are served from memory, never re-parsed ----
def test_repeated_fresh_reads_parse_the_document_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider refresh looks up hundreds of model ids; each lookup used to
    re-read and re-parse the whole catalog document (thousands of full parses
    for one OpenRouter refresh). A fresh read is now parsed once and re-served
    from memory -- with disk-cache provenance -- until its TTL expires."""
    parses = {"n": 0}

    def _counting_parse(payload: bytes) -> dict[str, Any]:
        parses["n"] += 1
        return _parse_dict(payload)

    catalog = FetchedCatalog(
        "widgets",
        "https://example/catalog.json",
        parse=_counting_parse,
        ttl_s=3600.0,
        max_bytes=1024,
        timeout_s=1.0,
        cache_path=tmp_path / "widgets.json",
    )
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get",
        lambda *_a, **_kw: _response('{"a": 1}', etag='"v1"'),
    )

    first = catalog.get()
    results = [catalog.get() for _ in range(50)]

    assert parses["n"] == 1
    assert first.source == "network"
    assert {result.source for result in results} == {"disk_cache"}
    assert {result.stale_reason for result in results} == {""}
    assert all(result.data == {"a": 1} for result in results)


def test_force_refresh_bypasses_the_in_memory_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    calls = {"n": 0}

    def _get(*_a: object, **_kw: object) -> httpx.Response:
        calls["n"] += 1
        return _response(json.dumps({"n": calls["n"]}))

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _get)
    catalog.get()
    assert catalog.get().data == {"n": 1}
    assert catalog.get(force_refresh=True).data == {"n": 2}
    assert catalog.get().data == {"n": 2}


def test_bundled_snapshot_is_loaded_once_and_every_read_keeps_its_typed_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    loads = {"n": 0}

    def _bundled() -> dict[str, Any]:
        loads["n"] += 1
        return {"bundled": True}

    catalog = _catalog(tmp_path, bundled=_bundled)
    with caplog.at_level("WARNING", logger="clio_agent.providers.fetched_catalog"):
        results = [catalog.get(allow_fetch=False) for _ in range(20)]

    assert loads["n"] == 1
    assert {result.stale_reason for result in results} == {"bundled_cold_start"}
    assert sum("bundled_cold_start" in record.getMessage() for record in caplog.records) == 1
