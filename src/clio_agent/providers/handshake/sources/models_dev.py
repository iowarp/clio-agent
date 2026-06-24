"""models.dev context-window source — the highest-priority context source.

`models.dev <https://models.dev>`_ publishes a single ``models.json`` describing
every model it tracks, including each model's ``limit.context``. This is the most
authoritative, broadly-covering source clio has, so the factory consults it first
(models.dev -> marketplace -> static).

Fetch / cache / offline policy:

* The catalog is fetched from :data:`MODELS_DEV_URL` and cached to a local JSON
  file under the clio data dir with a TTL (default 24h).
* A fresh-enough cache short-circuits the network.
* **Offline-safe:** any fetch failure falls back to the last cached copy if one
  exists, else a miss — a handshake must never fail because models.dev is down.
* Nothing is fetched at import time; the network is only touched lazily on first
  lookup when the cache is stale/absent.

The id matching mirrors the published key shape ``"<vendor>/<id>"`` and tolerates
provider ids that drop or echo the vendor — see
:func:`...sources._normalize.iter_id_candidates`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

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


def _data_dir() -> Path:
    """Per-user OS-correct cache dir for the (regenerable) models.dev catalog.

    The catalog is a global, regenerable cache shared across workspaces, so it lives in
    the OS cache dir (Linux ~/.cache, macOS ~/Library/Caches, Windows %LOCALAPPDATA%),
    not a workspace ``.clio/agent``."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return paths.user_cache_dir()


def default_cache_path() -> Path:
    """Return the default models.dev cache file path under the data dir."""
    return _data_dir() / "models_dev.json"


def _parse_catalog(text: str) -> dict[str, Any] | None:
    """Parse a models.dev JSON document into its top-level mapping, or None."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _load_cache(cache_path: Path) -> dict[str, Any] | None:
    """Return the parsed cached catalog, or None if absent/unreadable."""
    try:
        text = cache_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_catalog(text)


def _cache_is_fresh(cache_path: Path, ttl_s: float) -> bool:
    """Return True if ``cache_path`` exists and is younger than ``ttl_s``."""
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= ttl_s


def _write_cache(cache_path: Path, text: str) -> None:
    """Best-effort atomic write of the catalog to the cache; never raises."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(cache_path)
    except OSError:
        pass


def _fetch_catalog() -> str | None:
    """Fetch the raw models.dev document over HTTP, or None on any failure.

    Synchronous and dependency-light by design: this source is consulted from a
    sync factory and must degrade silently when offline.
    """
    try:
        import httpx  # noqa: PLC0415

        response = httpx.get(MODELS_DEV_URL, timeout=_FETCH_TIMEOUT_S)
        response.raise_for_status()
        return response.text
    except Exception:
        # Any error (offline, DNS, timeout, HTTP status, import) -> caller falls
        # back to the last cached copy.
        return None


def _load_models_dev(
    path: str | os.PathLike[str] | None = None,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    allow_fetch: bool = True,
) -> dict[str, Any]:
    """Return the models.dev catalog mapping, using cache/network per policy.

    This is the test seam: pass ``path`` to load a catalog directly from a file
    (e.g. a captured fixture) with no network access at all.

    Resolution order:

    1. If ``path`` is given, load and return that file's catalog (no fetch, no
       cache TTL) — the offline test path.
    2. Otherwise, if the default cache is fresh, return it.
    3. Otherwise try to fetch; on success, refresh the cache and return it.
    4. On fetch failure, fall back to the last cached copy if present.
    5. Total miss -> ``{}``.

    Args:
        path: Optional explicit catalog file to load (test seam / override).
        ttl_s: Cache freshness window in seconds.
        allow_fetch: When False, never touch the network (cache-only).

    Returns:
        The catalog mapping (``{"<vendor>/<id>": {...}}``), possibly empty.
    """
    if path is not None:
        catalog = _load_cache(Path(path))
        return catalog or {}

    cache_path = default_cache_path()

    if _cache_is_fresh(cache_path, ttl_s):
        cached = _load_cache(cache_path)
        if cached is not None:
            return cached

    if allow_fetch:
        text = _fetch_catalog()
        if text is not None:
            parsed = _parse_catalog(text)
            if parsed is not None:
                _write_cache(cache_path, text)
                return parsed

    # Fetch failed or disallowed — fall back to any cached copy, even if stale.
    stale = _load_cache(cache_path)
    if stale is not None:
        return stale
    return {}


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
