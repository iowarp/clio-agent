"""Process-global TTL cache for :class:`HandshakeReport` objects.

Generalizes the existing ad-hoc 30s ``_live_models_cache`` in ``gact/app.py`` so
repeated picker reads / health checks don't hammer a provider (and a down
provider's failure report is cached too, matching today's behaviour). Keyed by
``(provider_id, api_base)`` so an ``api_base`` override busts the entry.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from clio_agent.providers.handshake.model import HandshakeReport

DEFAULT_TTL_S = 30.0

# key -> (stored_monotonic, report)
_cache: dict[tuple[str, str], tuple[float, HandshakeReport]] = {}


def cache_key(provider_id: str, api_base: str) -> tuple[str, str]:
    return (provider_id, api_base or "")


def get_cached(key: tuple[str, str], *, ttl_s: float = DEFAULT_TTL_S) -> HandshakeReport | None:
    """Return a fresh-enough cached report, or None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    stored, report = entry
    if (time.monotonic() - stored) > ttl_s:
        return None
    return report


def put_cached(key: tuple[str, str], report: HandshakeReport) -> None:
    _cache[key] = (time.monotonic(), report)


def invalidate(key: tuple[str, str] | None = None) -> None:
    """Drop one entry, or the whole cache when ``key`` is None (e.g. on rebind)."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


async def cached_or_run(
    key: tuple[str, str],
    runner: Callable[[], Awaitable[HandshakeReport]],
    *,
    ttl_s: float = DEFAULT_TTL_S,
    force: bool = False,
) -> HandshakeReport:
    """Return a cached report or await ``runner`` and cache its result."""
    if not force:
        hit = get_cached(key, ttl_s=ttl_s)
        if hit is not None:
            return hit
    report = await runner()
    put_cached(key, report)
    return report
