"""Fetch the compiled model overlay from the CLIO GitHub document (brief Part 8).

The model overlay (``catalogs/models/*.yaml``, compiled by
``scripts/compile_model_overlay.py`` into ``catalogs/model-overlay.json``) is
served the same way ``catalogs/claude-code-models.json`` is
(:mod:`clio_agent.providers.model_discovery.claude_code_catalog`): fetched from
raw GitHub (:func:`~clio_agent.providers.fetched_catalog.clio_catalog_url`),
cached on disk with a TTL, and never cleared by a transient failure -- the last
good copy rides through instead. It reuses
:mod:`clio_agent.providers.fetched_catalog` rather than a second fetcher.

There is NO packaged copy (coordinator decision D18: everything is referenced
online). A first run with no network and no earlier successful fetch has no
overlay at all -- the typed ``catalog_unavailable_offline`` state -- rather
than a stale list shipped inside the wheel. The server fetches it once at
startup (:func:`clio_agent.providers.model_discovery.refresh.
refresh_subscription_catalogs_at_startup`); lookups read the disk cache only.

This module only fetches + validates the compiled document's outer shape
(``schema_version``, ``entries`` is a list of dict-shaped rows with a
``family``/``matchPatterns``/``capabilities``). Mapping an entry's fields onto
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities`, the
longest-match-wins lookup, and the multi-root (fetched/user/project)
precedence all live in :mod:`clio_agent.providers.capabilities.model_overlay`,
which is this module's only intended caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from clio_agent.providers.fetched_catalog import (
    FetchedCatalog,
    FetchedCatalogUnavailable,
    clio_catalog_url,
)

MODEL_OVERLAY_CATALOG_URL = clio_catalog_url("model-overlay.json")

#: The overlay is a small, hand-curated document (unlike a live per-model
#: probe result); an hour keeps clients current without hammering GitHub.
DEFAULT_TTL_S = 60 * 60.0

_FETCH_TIMEOUT_S = 8.0
_MAX_BYTES = 2 * 1024 * 1024
_SCHEMA_VERSION = 1


class ModelOverlayCatalogError(RuntimeError):
    """The compiled model overlay could not be read safely."""


@dataclass(frozen=True)
class ModelOverlayCatalog:
    """One fetched-and-validated snapshot of the compiled model overlay.

    Attributes:
        entries: Raw overlay rows, each shaped
            ``{"family", "matchPatterns", "capabilities", "quirks"?}`` exactly
            as ``catalogs/models/overlay.schema.json`` describes -- unpacked
            into :class:`~clio_agent.providers.capabilities.model_overlay.OverlayEntry`
            by that module, not here.
    """

    entries: list[dict[str, Any]]


def _parse_catalog(payload: bytes) -> ModelOverlayCatalog:
    """Validate one compiled overlay document's bytes into a :class:`ModelOverlayCatalog`.

    Only checks the OUTER shape the compile script guarantees
    (``schema_version``, ``entries`` is a list of ``family``/``matchPatterns``/
    ``capabilities``-bearing dicts) -- full per-entry schema validation already
    happened in CI when this document was compiled and committed. Raises
    :class:`ModelOverlayCatalogError` on any shape violation, which
    ``FetchedCatalog`` treats as a validation failure (the last good copy
    rides through instead of being replaced by a corrupt fetch).
    """

    try:
        payload_obj: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ModelOverlayCatalogError(f"model overlay is not valid JSON: {exc}") from exc

    if not isinstance(payload_obj, dict) or payload_obj.get("schema_version") != _SCHEMA_VERSION:
        raise ModelOverlayCatalogError("model overlay has an unsupported schema_version")
    entries = payload_obj.get("entries")
    if not isinstance(entries, list):
        raise ModelOverlayCatalogError("model overlay 'entries' must be a list")

    validated: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("family"), str)
            or not entry["family"].strip()
            or not isinstance(entry.get("matchPatterns"), list)
            or not entry["matchPatterns"]
            or not all(isinstance(p, str) and p for p in entry["matchPatterns"])
            or not isinstance(entry.get("capabilities"), dict)
        ):
            raise ModelOverlayCatalogError(f"model overlay entry {index} is malformed")
        validated.append(entry)
    return ModelOverlayCatalog(entries=validated)


_CATALOG: FetchedCatalog[ModelOverlayCatalog] = FetchedCatalog(
    "model-overlay",
    MODEL_OVERLAY_CATALOG_URL,
    parse=_parse_catalog,
    ttl_s=DEFAULT_TTL_S,
    max_bytes=_MAX_BYTES,
    timeout_s=_FETCH_TIMEOUT_S,
)


def _raise_unavailable(exc: FetchedCatalogUnavailable) -> ModelOverlayCatalogError:
    return ModelOverlayCatalogError(f"could not load the model overlay: {exc}")


def load_model_overlay_catalog() -> ModelOverlayCatalog:
    """Return the current compiled overlay, per the disk-cache/network policy.

    Raises:
        ModelOverlayCatalogError: ``catalog_unavailable_offline`` -- no live
            fetch answered and no earlier fetch left a disk cache.
    """

    try:
        return _CATALOG.get().data
    except FetchedCatalogUnavailable as exc:
        raise _raise_unavailable(exc) from exc


def refresh_model_overlay_catalog() -> ModelOverlayCatalog:
    """Force a live re-fetch (bypassing TTL freshness), keeping last-good on failure."""

    try:
        return _CATALOG.get(force_refresh=True).data
    except FetchedCatalogUnavailable as exc:
        raise _raise_unavailable(exc) from exc


def cached_model_overlay_entries() -> tuple[list[dict[str, Any]] | None, str]:
    """Return the last known-good overlay entries without touching the network.

    Disk cache only (no HTTP) -- for callers on a cold, offline path
    (:meth:`~clio_agent.providers.capabilities.model_overlay.CatalogOverlaySource`)
    that must never block on network I/O just to answer one model's facts.
    """

    try:
        return _CATALOG.get(allow_fetch=False).data.entries, ""
    except FetchedCatalogUnavailable as exc:
        return None, str(exc)


__all__ = [
    "MODEL_OVERLAY_CATALOG_URL",
    "ModelOverlayCatalog",
    "ModelOverlayCatalogError",
    "cached_model_overlay_entries",
    "load_model_overlay_catalog",
    "refresh_model_overlay_catalog",
]
