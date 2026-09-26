"""The one accessor: effective capabilities for ``(provider_id, api_base, model_id)``.

Every consumer that used to read a flat ``ModelProfile`` flag now calls
:func:`get_effective_capabilities` instead. It looks up the three records from
:mod:`clio_agent.providers.capabilities.invalidation` (whatever a handshake
adapter -- or the last-good persistence -- most recently wrote), combines them
with :func:`~clio_agent.providers.capabilities.combine.combine_capabilities`,
and caches the result keyed by the identity PLUS every contributing record's
own fingerprint, so a change to any one of the three records invalidates
exactly the effective views that depended on it and nothing else -- callers
never have to remember to bust anything themselves.
"""

from __future__ import annotations

from collections import OrderedDict

from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.combine import EffectiveCapabilities, combine_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    ModelCapabilities,
)
from clio_agent.providers.identity import deployment_key, endpoint_key

#: Bounds the memoization cache so a long-running server with many transient
#: (provider, api_base, model) combinations (per-expert overrides, ad-hoc
#: probes) cannot grow this dict without limit. Well above realistic
#: concurrent identity counts for one process.
_MAX_CACHE_ENTRIES = 2048

#: cache key -> effective view. An ``OrderedDict`` gives cheap "move to end on
#: hit" + "pop oldest on overflow" without a third-party LRU dependency.
_cache: OrderedDict[tuple, EffectiveCapabilities] = OrderedDict()


def _record_fingerprint(
    record: ModelCapabilities | EndpointCapabilities | DeploymentCapabilities | None,
) -> str:
    """A structural cache-busting token for one record.

    Endpoint/deployment records carry their own brief-5.6 ``fingerprint``
    (the server/loaded-model identity a probe re-derives); when that field is
    populated it IS the token. Otherwise (a model record, which has no single
    fingerprint field yet -- P4a has no overlay/HF layer to version it by), or
    when a fingerprint has never been set, the record's own ``repr`` stands in:
    every field is part of a frozen dataclass, so two structurally identical
    records repr identically and two different ones never collide in a way
    that would wrongly serve a stale cached combination.
    """

    if record is None:
        return ""
    fingerprint = getattr(record, "fingerprint", "") or ""
    if fingerprint:
        return fingerprint
    return repr(record)


def _remember(key: tuple, value: EffectiveCapabilities) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > _MAX_CACHE_ENTRIES:
        _cache.popitem(last=False)


def get_effective_capabilities(
    provider_id: str, api_base: str, model_id: str
) -> EffectiveCapabilities:
    """Return the combined, cached effective capabilities for one deployment.

    Args:
        provider_id: The configured provider's own id (never its wire kind --
            brief Part 3).
        api_base: The endpoint's configured base URL (normalized internally).
        model_id: The wire model id as the server itself names it.

    Returns:
        An :class:`~clio_agent.providers.capabilities.combine.EffectiveCapabilities`.
        Every field degrades to "unknown"/no-restriction gracefully when a
        contributing record has not been recorded yet (e.g. before the first
        handshake for this endpoint has completed) -- this never raises for a
        cold identity.
    """

    dep_key = deployment_key(provider_id, api_base, model_id)
    end_key = endpoint_key(provider_id, api_base)
    deployment = invalidation.get_deployment_capabilities(dep_key)
    endpoint = invalidation.get_endpoint_capabilities(end_key)
    model_key = (
        deployment.model_key.value
        if deployment is not None and deployment.model_key.known
        else None
    )
    model = invalidation.get_model_capabilities(model_key)

    cache_key = (
        dep_key,
        _record_fingerprint(model),
        _record_fingerprint(endpoint),
        _record_fingerprint(deployment),
    )
    cached = _cache.get(cache_key)
    if cached is not None:
        _cache.move_to_end(cache_key)
        return cached

    result = combine_capabilities(model, endpoint, deployment)
    _remember(cache_key, result)
    return result


def clear_cache() -> None:
    """Drop every memoized effective view. Test-only."""
    _cache.clear()


__all__ = ["clear_cache", "get_effective_capabilities"]
