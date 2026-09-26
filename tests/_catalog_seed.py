"""Seed a fetched catalog's disk cache the way an earlier successful fetch leaves it.

CLIO's catalogs are referenced online only (no packaged copies). Tests that
need a catalog's CONTENT without the network write the disk-cache envelope
:class:`clio_agent.providers.fetched_catalog.FetchedCatalog` itself writes,
using the repository's own ``catalogs/`` file -- exactly the bytes raw GitHub
serves -- as the recorded payload.
"""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.providers.fetched_catalog import cache_path_for

REPO_CATALOGS = Path(__file__).resolve().parents[1] / "catalogs"


def seed_catalog_cache(
    name: str,
    payload: bytes | str,
    *,
    source_url: str = "",
    fetched_at: str = "2020-01-01T00:00:00+00:00",
    cache_path: Path | None = None,
) -> Path:
    """Write ``payload`` as catalog ``name``'s disk cache; return the cache path.

    ``fetched_at`` defaults to long ago: the copy is served (typed stale) to
    offline reads, and any read allowed to fetch still re-fetches.
    """

    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    path = cache_path or cache_path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "etag": "",
                "source_url": source_url,
                "version": "sha256:recorded",
                "payload": text,
            }
        ),
        encoding="utf-8",
    )
    return path


def seed_repo_catalog(filename: str, name: str, *, cache_path: Path | None = None) -> Path:
    """Seed catalog ``name`` from the repository's ``catalogs/<filename>``."""

    return seed_catalog_cache(name, (REPO_CATALOGS / filename).read_bytes(), cache_path=cache_path)


#: A recorded slice of LiteLLM's live ``model_prices_and_context_window.json``
#: (captured 2026-09-26 from raw GitHub).
LITELLM_RECORDED_SLICE = (
    Path(__file__).resolve().parent
    / "test_providers"
    / "fixtures"
    / "catalogs"
    / "litellm_recorded_slice.json"
)


def seed_model_overlay() -> Path:
    """Seed the model-overlay catalog cache from ``catalogs/model-overlay.json``."""

    return seed_repo_catalog("model-overlay.json", "model-overlay")


def seed_litellm_cost_map() -> Path:
    """Seed the LiteLLM cost-map cache with the recorded slice."""

    return seed_catalog_cache("litellm-model-cost-map", LITELLM_RECORDED_SLICE.read_bytes())
