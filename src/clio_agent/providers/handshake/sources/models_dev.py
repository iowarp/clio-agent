"""models.dev context-window source — the highest-priority context source.

`models.dev <https://models.dev>`_ publishes a single ``models.json`` describing
every model it tracks, including each model's ``limit.context``. This is the most
authoritative, broadly-covering source clio has, so the factory consults it first
(models.dev -> marketplace -> static).

Fetch / cache / offline policy is owned by the generic
:mod:`clio_agent.providers.fetched_catalog` mechanism (disk cache with a TTL and
ETag under ``paths.user_cache_dir()/catalogs/models-dev.json``, atomic writes,
last-good-never-cleared on a failed fetch or a failed validation). This module
supplies only the models.dev-specific parts: the URL, the TTL, and how to parse
and index one ``models.json`` document.

**Offline-safe:** any fetch failure or invalid document falls back to the last
cached copy if one exists, else a miss — a handshake must never fail because
models.dev is down. Nothing is fetched at import time; the network is only
touched lazily on first lookup when the cache is stale/absent.

The id matching mirrors the published key shape ``"<vendor>/<id>"`` and tolerates
provider ids that drop or echo the vendor — see
:func:`...sources._normalize.iter_id_candidates`.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from clio_agent.providers.fetched_catalog import FetchedCatalog, FetchedCatalogUnavailable
from clio_agent.providers.handshake.sources._normalize import (
    iter_id_candidates,
    normalize_id,
)

#: Canonical catalog URL.
MODELS_DEV_URL = "https://models.dev/models.json"

#: Default cache lifetime in seconds (24h).
DEFAULT_TTL_S = 24 * 60 * 60.0

#: HTTP fetch timeout in seconds — short; a slow models.dev must not stall a handshake.
_FETCH_TIMEOUT_S = 6.0

#: Cap a models.dev document at 16 MiB — generous for a catalog of this shape,
#: but still a hard ceiling against a misbehaving/compromised upstream.
_MAX_BYTES = 16 * 1024 * 1024


def default_cache_path() -> Path:
    """Return the default models.dev cache file path (fetched_catalog's disk cache)."""
    return _catalog(ttl_s=DEFAULT_TTL_S).cache_path


def _parse_catalog(payload: bytes) -> dict[str, Any]:
    """Parse+validate a models.dev JSON document into its top-level mapping.

    Raises:
        ValueError: The payload is not valid JSON, or not a JSON object.
    """
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("models.dev catalog is not a JSON object")
    return data


@lru_cache(maxsize=8)
def _catalog(*, ttl_s: float) -> FetchedCatalog[dict[str, Any]]:
    """Return the (process-singleton, per-``ttl_s``) :class:`FetchedCatalog`.

    Memoised so every caller shares one instance -- and therefore one refresh
    lock and one view of the disk cache -- rather than each call building a
    throwaway ``FetchedCatalog`` whose lock protects nothing.

    No bundled fallback: models.dev ships nothing with the package, so a total
    cold start with no network is a plain miss (see :func:`_load_models_dev`).
    """
    return FetchedCatalog(
        "models-dev",
        MODELS_DEV_URL,
        parse=_parse_catalog,
        ttl_s=ttl_s,
        max_bytes=_MAX_BYTES,
        timeout_s=_FETCH_TIMEOUT_S,
    )


def _load_models_dev(
    path: str | os.PathLike[str] | None = None,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    allow_fetch: bool = True,
) -> dict[str, Any]:
    """Return the models.dev catalog mapping, using cache/network per policy.

    This is the test seam: pass ``path`` to load a catalog directly from a file
    (e.g. a captured fixture) with no network access and no cache involvement at
    all — the pure offline test path.

    Otherwise resolution is delegated to :class:`~clio_agent.providers.fetched_catalog.FetchedCatalog`:
    a fresh disk cache short-circuits the network; a stale/absent cache tries to
    fetch (when ``allow_fetch``); a failed fetch or a failed validation falls
    back to the last good disk copy; a total miss (no cache, no bundled source,
    fetch failed/disabled) returns ``{}`` rather than raising — models.dev being
    unreachable must never fail a handshake.

    Args:
        path: Optional explicit catalog file to load (test seam / override).
        ttl_s: Cache freshness window in seconds.
        allow_fetch: When False, never touch the network (cache-only).

    Returns:
        The catalog mapping (``{"<vendor>/<id>": {...}}``), possibly empty.
    """
    if path is not None:
        try:
            return _parse_catalog(Path(path).read_bytes())
        except (OSError, ValueError):
            return {}

    try:
        result = _catalog(ttl_s=ttl_s).get(allow_fetch=allow_fetch)
    except FetchedCatalogUnavailable:
        return {}
    return result.data


def _extract_context(entry: object) -> int | None:
    """Pull ``limit.context`` out of a models.dev entry, or None if absent/invalid."""
    if not isinstance(entry, dict):
        return None
    limit = entry.get("limit")
    if not isinstance(limit, dict):
        return None
    context = limit.get("context")
    if isinstance(context, bool):  # reject bools masquerading as ints
        return None
    if isinstance(context, int) and context > 0:
        return context
    return None


def _build_index(catalog: dict[str, Any]) -> dict[str, int]:
    """Build a normalized ``{candidate_key: context}`` index from a raw catalog.

    For each ``"<vendor>/<id>"`` key we index both the full normalized key and its
    post-slash basename, so a provider id lacking the vendor prefix still matches.
    On a basename collision the first-seen value wins (callers prefer the full-key
    match anyway because :func:`iter_id_candidates` probes it first).
    """
    index: dict[str, int] = {}
    for key, entry in catalog.items():
        if not isinstance(key, str):
            continue
        context = _extract_context(entry)
        if context is None:
            continue
        norm_key = normalize_id(key)
        if norm_key and norm_key not in index:
            index[norm_key] = context
        if "/" in norm_key:
            basename = norm_key.rsplit("/", 1)[1]
            if basename and basename not in index:
                index[basename] = context
    return index


def lookup_models_dev(
    model_id: str,
    *,
    path: str | os.PathLike[str] | None = None,
    ttl_s: float = DEFAULT_TTL_S,
    allow_fetch: bool = True,
) -> int | None:
    """Return the models.dev context window for ``model_id``, or None.

    Args:
        model_id: The raw model identifier (with or without a ``vendor/`` prefix).
        path: Optional explicit catalog file (test seam — forces offline load).
        ttl_s: Cache freshness window in seconds.
        allow_fetch: When False, never touch the network (cache-only).

    Returns:
        The context window in tokens, or ``None`` on a miss.
    """
    catalog = _load_models_dev(path, ttl_s=ttl_s, allow_fetch=allow_fetch)
    if not catalog:
        return None
    index = _build_index(catalog)
    for candidate in iter_id_candidates(model_id):
        window = index.get(candidate)
        if window is not None:
            return window
    return None


def _extract_output(entry: object) -> int | None:
    """Pull ``limit.output`` out of a models.dev entry, or None if absent/invalid."""
    if not isinstance(entry, dict):
        return None
    limit = entry.get("limit")
    if not isinstance(limit, dict):
        return None
    output = limit.get("output")
    if isinstance(output, bool):
        return None
    if isinstance(output, int) and output > 0:
        return output
    return None


def _build_output_index(catalog: dict[str, Any]) -> dict[str, int]:
    """Build a normalized ``{candidate_key: max_output_tokens}`` index (``limit.output``)."""
    index: dict[str, int] = {}
    for key, entry in catalog.items():
        if not isinstance(key, str):
            continue
        output = _extract_output(entry)
        if output is None:
            continue
        norm_key = normalize_id(key)
        if norm_key and norm_key not in index:
            index[norm_key] = output
        if "/" in norm_key:
            basename = norm_key.rsplit("/", 1)[1]
            if basename and basename not in index:
                index[basename] = output
    return index


def lookup_models_dev_output(
    model_id: str,
    *,
    path: str | os.PathLike[str] | None = None,
    ttl_s: float = DEFAULT_TTL_S,
    allow_fetch: bool = True,
) -> int | None:
    """Return the models.dev max output tokens (``limit.output``) for ``model_id``, or None."""
    catalog = _load_models_dev(path, ttl_s=ttl_s, allow_fetch=allow_fetch)
    if not catalog:
        return None
    index = _build_output_index(catalog)
    for candidate in iter_id_candidates(model_id):
        output = index.get(candidate)
        if output is not None:
            return output
    return None


#: The modality spellings models.dev uses in ``modalities.input``/``.output`` that
#: CLIO has a modality for. models.dev's vocabulary already IS CLIO's
#: (``text``/``image``/``audio``/``video``/``pdf``); anything else is dropped
#: rather than guessed at.
_MODELS_DEV_MODALITIES: frozenset[str] = frozenset({"text", "image", "audio", "video", "pdf"})


def _build_entry_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a normalized ``{candidate_key: entry}`` index (same key rules as :func:`_build_index`)."""
    index: dict[str, dict[str, Any]] = {}
    for key, entry in catalog.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        norm_key = normalize_id(key)
        if norm_key and norm_key not in index:
            index[norm_key] = entry
        if "/" in norm_key:
            basename = norm_key.rsplit("/", 1)[1]
            if basename and basename not in index:
                index[basename] = entry
    return index


def lookup_models_dev_entry(
    model_id: str,
    *,
    path: str | os.PathLike[str] | None = None,
    ttl_s: float = DEFAULT_TTL_S,
    allow_fetch: bool = True,
) -> dict[str, Any] | None:
    """Return the whole models.dev entry for ``model_id`` (one catalog load), or None.

    Uses the same id candidates as the limit lookups, so a caller that needs
    several fields of one model reads the catalog once instead of once per field.
    """
    catalog = _load_models_dev(path, ttl_s=ttl_s, allow_fetch=allow_fetch)
    if not catalog:
        return None
    index = _build_entry_index(catalog)
    for candidate in iter_id_candidates(model_id):
        entry = index.get(candidate)
        if entry is not None:
            return entry
    return None


def _modality_set(raw: object) -> frozenset[str] | None:
    if not isinstance(raw, list):
        return None
    values = {str(value).strip().lower() for value in raw if isinstance(value, str)}
    return frozenset(values & _MODELS_DEV_MODALITIES)


def modalities_from_entry(
    entry: object,
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    """``(input, output)`` modalities from a models.dev entry's ``modalities`` block.

    Each side is ``None`` when the entry does not state it -- an absent list is
    no evidence, never "text only".
    """
    if not isinstance(entry, dict):
        return None, None
    block = entry.get("modalities")
    if not isinstance(block, dict):
        return None, None
    return _modality_set(block.get("input")), _modality_set(block.get("output"))


def task_from_output(output: frozenset[str] | None) -> str | None:
    """The task a models.dev OUTPUT list proves, when it proves one.

    models.dev has no task field and lists embedding models with a ``text``
    output, so a ``text`` output decides nothing. Only an output that lacks text
    names a task: image output is ``text-to-image``, audio output ``text-to-speech``.
    """
    if not output or "text" in output:
        return None
    if "image" in output:
        return "text-to-image"
    if "audio" in output:
        return "text-to-speech"
    return None
