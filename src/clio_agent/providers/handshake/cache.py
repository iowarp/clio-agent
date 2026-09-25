"""Process-global TTL cache for :class:`HandshakeReport` objects.

Generalizes the existing ad-hoc 30s ``_live_models_cache`` in ``gact/app.py`` so
repeated picker reads / health checks don't hammer a provider (and a down
provider's failure report is cached too, matching today's behaviour). Keyed by
the endpoint identity ``(provider_id, normalized api_base)``
(:mod:`clio_agent.providers.identity`) so an ``api_base`` override busts the
entry, and a cosmetically different spelling of the SAME endpoint (a trailing
slash, a spelled-out default port) never misses when it should hit.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from clio_agent.providers.handshake.model import HandshakeReport
from clio_agent.providers.identity import EndpointKey, endpoint_key

DEFAULT_TTL_S = 30.0

# key -> (stored_monotonic, report)
_cache: dict[EndpointKey, tuple[float, HandshakeReport]] = {}


def cache_key(provider_id: str, api_base: str) -> EndpointKey:
    return endpoint_key(provider_id, api_base)


def get_cached(key: EndpointKey, *, ttl_s: float = DEFAULT_TTL_S) -> HandshakeReport | None:
    """Return a fresh-enough cached report, or None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    stored, report = entry
    if (time.monotonic() - stored) > ttl_s:
        return None
    return report


def put_cached(key: EndpointKey, report: HandshakeReport) -> None:
    _cache[key] = (time.monotonic(), report)


def invalidate(key: EndpointKey | None = None) -> None:
    """Drop one entry, or the whole cache when ``key`` is None (e.g. on rebind)."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def invalidate_provider(provider_id: str) -> int:
    """Drop every cached report for ``provider_id`` (all ``api_base`` variants).

    Used when the provider's credential state changed out of band — a completed
    sign-in or an explicit "Check provider" — so the next read re-probes instead
    of serving a report produced under the old credential. Returns how many
    entries were dropped.
    """
    stale = [key for key in _cache if key[0] == provider_id]
    for key in stale:
        _cache.pop(key, None)
    return len(stale)


async def cached_or_run(
    key: EndpointKey,
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
