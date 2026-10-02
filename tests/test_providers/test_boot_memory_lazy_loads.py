"""Boot-time memory: work nothing at startup needs is deferred to first use (D16).

Measured on a booted server (no LM calls), develop vs the 2026-09-03 gate commit:
the LiteLLM community cost map (~10 MB in isolation) was parsed and memoised by
the startup catalog refresh, whose job is only to make the disk cache fresh. It
is now deferred without changing what the consumer eventually sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.providers.fetched_catalog import FetchedCatalog

# --------------------------------------------------------------------------- #
# LiteLLM cost map: the startup refresh does not keep the parsed map resident
# --------------------------------------------------------------------------- #


def _catalog(tmp_path: Path) -> FetchedCatalog[dict[str, Any]]:
    return FetchedCatalog(
        "widgets",
        "https://example/catalog.json",
        parse=lambda payload: json.loads(payload.decode("utf-8")),
        ttl_s=3600.0,
        timeout_s=1.0,
        cache_path=tmp_path / "widgets.json",
    )


def _response(payload: str) -> httpx.Response:
    return httpx.Response(
        200, text=payload, request=httpx.Request("GET", "https://example/catalog.json")
    )


def test_refresh_disk_cache_writes_the_cache_but_memoises_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get", lambda *_a, **_kw: _response('{"a": 1}')
    )
    result = catalog.refresh_disk_cache()
    assert result.source == "network"
    assert (tmp_path / "widgets.json").is_file()
    assert catalog._fresh is None  # noqa: SLF001 - the point: nothing resident

    def _no_network(*_a: object, **_kw: object) -> httpx.Response:
        raise AssertionError("the first lookup must read the fresh disk cache")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _no_network)
    first = catalog.get()
    assert (first.source, first.data, first.stale_reason) == ("disk_cache", {"a": 1}, "")
    assert catalog._fresh is not None  # noqa: SLF001 - memoised on first use


def test_refresh_disk_cache_keeps_a_memo_a_lookup_already_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get", lambda *_a, **_kw: _response('{"a": 1}')
    )
    catalog.get()
    catalog.refresh_disk_cache()
    assert catalog._fresh is not None  # noqa: SLF001 - never evicts a real consumer's memo


def test_the_startup_refresh_uses_the_disk_only_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.handshake.sources import db as model_limits_db
    from clio_agent.providers.handshake.sources import litellm_catalog
    from clio_agent.providers.model_discovery import model_overlay_catalog, refresh

    calls: list[str] = []

    class _Catalog:
        def refresh_disk_cache(self) -> None:
            calls.append("refresh_disk_cache")

        def get(self, **_kw: object) -> None:
            calls.append("get")

    monkeypatch.setattr(litellm_catalog, "_catalog", lambda: _Catalog())
    monkeypatch.setattr(model_limits_db, "refresh_model_limits_seed", lambda: "")
    monkeypatch.setattr(model_overlay_catalog, "load_model_overlay_catalog", lambda: None)
    assert refresh.refresh_online_catalogs() == {}
    assert calls == ["refresh_disk_cache"]
