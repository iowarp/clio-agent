"""CLIO's catalogs are referenced online only (coordinator decision D18).

* CLIO's own catalogs (``catalogs/claude-code-models.json``,
  ``catalogs/model-overlay.json``, ``catalogs/model-limits.json``) are fetched
  from raw GitHub ``main`` through :mod:`clio_agent.providers.fetched_catalog`.
* A first run with no network is the typed ``catalog_unavailable_offline``
  state per catalog; a later outage serves the last good disk copy.
* No runtime read path of a packaged catalog copy is left in ``src/``.

The fetch tests run a REAL local HTTP server serving the repository's own
``catalogs/`` files (the bytes raw GitHub serves) and point the catalog base
URL at it.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.fetched_catalog import (
    CATALOG_UNAVAILABLE_OFFLINE,
    FetchedCatalog,
    FetchedCatalogUnavailable,
    clio_catalog_url,
)
from clio_agent.providers.handshake.sources import db as model_limits_db
from clio_agent.providers.model_discovery import (
    claude_code_catalog,
    model_overlay_catalog,
    refresh,
)
from tests._catalog_seed import REPO_CATALOGS

_SRC = Path(__file__).resolve().parents[2] / "src" / "clio_agent"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def catalog_server() -> Iterator[dict[str, Any]]:
    """Serve ``catalogs/`` on 127.0.0.1; ``state["up"] = False`` answers 503."""

    state: dict[str, Any] = {"up": True, "hits": []}

    class Handler(_Quiet):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            state["hits"].append(self.path)
            if not state["up"]:
                self.send_error(503)
                return
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(REPO_CATALOGS)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _served(catalog: FetchedCatalog[Any], base: str, filename: str, tmp_path: Path) -> Any:
    """The same catalog wiring, pointed at the fixture server and a tmp cache."""
    return FetchedCatalog(
        catalog.name,
        f"{base}/{filename}",
        parse=catalog._parse,
        ttl_s=0.0,  # every read re-fetches: exercises the outage path directly
        max_bytes=catalog.max_bytes,
        timeout_s=5.0,
        cache_path=tmp_path / f"{catalog.name}.json",
    )


def test_clio_catalogs_are_referenced_on_raw_github_main() -> None:
    base = "https://raw.githubusercontent.com/iowarp/clio-agent/main/catalogs"
    assert fetched_catalog.CLIO_CATALOG_BASE_URL == base
    assert claude_code_catalog.CLAUDE_CODE_CATALOG_URL == f"{base}/claude-code-models.json"
    assert model_overlay_catalog.MODEL_OVERLAY_CATALOG_URL == f"{base}/model-overlay.json"
    assert model_limits_db.MODEL_LIMITS_SEED_URL == f"{base}/model-limits.json"
    assert clio_catalog_url("x.json") == f"{base}/x.json"


@pytest.mark.parametrize(
    ("catalog", "filename"),
    [
        (model_overlay_catalog._CATALOG, "model-overlay.json"),
        (model_limits_db._SEED, "model-limits.json"),
        (claude_code_catalog._CATALOG, "claude-code-models.json"),
    ],
    ids=["model-overlay", "model-limits", "claude-code-models"],
)
def test_fetch_then_last_good_through_an_outage(
    catalog_server: dict[str, Any], tmp_path: Path, catalog: Any, filename: str
) -> None:
    served = _served(catalog, catalog_server["base"], filename, tmp_path)

    first = served.get()
    assert first.source == "network"
    assert first.stale_reason == ""
    assert catalog_server["hits"] == [f"/{filename}"]

    catalog_server["up"] = False
    second = served.get()
    assert second.source == "disk_cache"
    assert second.stale_reason == "http_status_503"
    assert second.data == first.data


@pytest.mark.parametrize(
    ("catalog", "filename"),
    [
        (model_overlay_catalog._CATALOG, "model-overlay.json"),
        (model_limits_db._SEED, "model-limits.json"),
        (claude_code_catalog._CATALOG, "claude-code-models.json"),
    ],
    ids=["model-overlay", "model-limits", "claude-code-models"],
)
def test_first_run_offline_is_typed_per_catalog(
    catalog_server: dict[str, Any], tmp_path: Path, catalog: Any, filename: str
) -> None:
    catalog_server["up"] = False
    served = _served(catalog, catalog_server["base"], filename, tmp_path)

    with pytest.raises(FetchedCatalogUnavailable) as error:
        served.get()
    assert error.value.reason == CATALOG_UNAVAILABLE_OFFLINE
    assert error.value.name == catalog.name
    assert error.value.fetch_failure == "http_status_503"


def test_startup_refresh_reports_each_unavailable_catalog_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _offline(*_a: object, **_kw: object) -> Any:
        import httpx

        raise httpx.ConnectError("offline")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _offline)

    failures = refresh.refresh_online_catalogs()

    assert set(failures) == {"model-overlay", "model-limits", "litellm-model-cost-map"}
    assert all(CATALOG_UNAVAILABLE_OFFLINE in failure for failure in failures.values())
    # Lookups in the offline state are typed misses, never a packaged answer.
    assert model_limits_db.lookup_context("openai/gpt-4o") is None
    entries, error = model_overlay_catalog.cached_model_overlay_entries()
    assert entries is None
    assert error.startswith(CATALOG_UNAVAILABLE_OFFLINE)


def test_no_packaged_catalog_read_path_is_left() -> None:
    """Grep guard: no runtime read of a catalog copy shipped inside the wheel."""

    offenders: list[str] = []
    patterns = [
        re.compile(r"bundled=[A-Za-z_]"),  # FetchedCatalog's deleted packaged-fallback kwarg
        re.compile(r"model_prices_and_context_window_backup"),  # litellm's in-wheel map
        re.compile(r"[\"']data[\"']\s*/\s*[\"'](model[-_]limits|model-overlay)"),
        re.compile(r"resources\.files\(\s*[\"']litellm[\"']"),
    ]
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(_SRC)}: {pattern.pattern}")
    assert offenders == []
    packaged = [
        p.relative_to(_SRC)
        for p in _SRC.rglob("*.json")
        if p.name in {"model_limits.json", "model-limits.json", "model-overlay.json"}
        or p.name.endswith("-models.json")
    ]
    assert packaged == []
