"""The in-process snapshot behind ``GET /v1/provider-catalog``.

Discovery is a handshake per provider, so ordinary reads serve one snapshot
instead of re-probing every provider. Three things retire parts of it:

* ``refresh=true`` re-probes every provider, or only ``provider=<id>``.
* :func:`invalidate_provider` — called when a provider's credential state
  changes out of band (sign-in completed, an explicit "Check provider") —
  drops that provider's cached handshake and marks its snapshot entry for
  re-discovery on the very next read, so no client has to know to ask. An
  invalidation that arrives while a read is awaiting discovery is kept for the
  next read, never dropped.
* A provider served from its last-good list (a live probe came back empty) is
  re-probed in the background with bounded backoff until a live answer
  replaces it (:mod:`clio_agent.gact.provider_catalog_reprobe`).

Every write stamps the providers it covers with a monotonically increasing
sequence number (:func:`provider_seq`), so a slower background re-probe never
merges its older answer over a newer refresh.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact.events import Event
from clio_agent.gact.provider_catalog import discover_provider
from clio_agent.providers.catalog import as_lm_presets
from clio_agent.providers.handshake import cache as handshake_cache
from clio_agent.providers.model_discovery import LAST_GOOD_CATALOG_SOURCE

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_INVALIDATED_ATTR = "provider_catalog_invalidated"
_SEQ_ATTR = "provider_catalog_seq"
_SEQUENCE = itertools.count(1)


class UnknownCatalogProviderError(LookupError):
    """``provider=<id>`` named no registered provider preset."""


def invalidate_provider(app: "FastAPI", provider_id: str) -> None:
    """Retire one provider's catalog evidence after its credential state changed.

    Drops every cached handshake report for the provider and marks its snapshot
    entry so the next catalog read re-discovers it (with no ``refresh`` flag
    needed). Other providers' entries are untouched.
    """

    handshake_cache.invalidate_provider(provider_id)
    _pending(app).add(provider_id)


def _pending(app: "FastAPI") -> set[str]:
    pending = getattr(app.state, _INVALIDATED_ATTR, None)
    if not isinstance(pending, set):
        pending = set()
        setattr(app.state, _INVALIDATED_ATTR, pending)
    return pending


def provider_seq(app: "FastAPI", provider_id: str) -> int:
    """The sequence number of the last snapshot write covering ``provider_id`` (0: never)."""

    seqs = getattr(app.state, _SEQ_ATTR, None)
    return int(seqs.get(provider_id, 0)) if isinstance(seqs, dict) else 0


def commit(app: "FastAPI", payload: dict[str, Any], provider_ids: list[str]) -> None:
    """Install ``payload`` as the snapshot and stamp the providers it re-evidenced."""

    seqs = getattr(app.state, _SEQ_ATTR, None)
    if not isinstance(seqs, dict):
        seqs = {}
        setattr(app.state, _SEQ_ATTR, seqs)
    stamp = next(_SEQUENCE)
    for provider_id in provider_ids:
        seqs[provider_id] = stamp
    app.state.provider_catalog = payload


def publish(app: "FastAPI", payload: dict[str, Any]) -> None:
    """Tell every session the catalog changed."""

    for session in app.state.sessions.list():
        app.state.bus.publish(
            Event(type="provider_catalog.refreshed", session_id=session.id, payload=payload)
        )


def _payload(providers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"catalog_id": "active", "providers": providers, "authoritative": "live_handshake"}


def is_stale(record: dict[str, Any]) -> bool:
    """Whether a provider record is a served last-good list (not current evidence)."""

    freshness = record.get("freshness")
    return isinstance(freshness, dict) and freshness.get("source") == LAST_GOOD_CATALOG_SOURCE


def stale_provider_ids(payload: Any) -> list[str]:
    """Ids of the providers ``payload`` serves from a last-good list."""

    if not isinstance(payload, dict):
        return []
    return [
        str(provider.get("id") or "")
        for provider in payload.get("providers") or []
        if isinstance(provider, dict) and provider.get("id") and is_stale(provider)
    ]


def merge(payload: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the matching provider entries in ``payload`` (preset order is kept)."""

    by_id = {str(record.get("id")): record for record in records}
    providers = [
        by_id.pop(str(provider.get("id")), provider)
        for provider in payload.get("providers") or []
        if isinstance(provider, dict)
    ]
    providers.extend(by_id.values())
    return _payload(providers)


async def discover(provider_ids: list[str], *, refresh: bool) -> list[dict[str, Any]]:
    """Discover the named providers concurrently (unknown ids are skipped)."""

    presets = {preset.id: preset for preset in as_lm_presets()}
    return list(
        await asyncio.gather(
            *(
                discover_provider(presets[provider_id], refresh=refresh)
                for provider_id in provider_ids
                if provider_id in presets
            )
        )
    )


def _keep_newer(
    app: "FastAPI", records: list[dict[str, Any]], seqs: dict[str, int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Swap in the CURRENT entry for any provider re-written while ``records`` was awaited.

    Returns the records to install and the ids they newly evidence: a provider
    whose sequence moved (a background re-probe answered meanwhile) keeps that
    newer entry and is not re-stamped.
    """
    current = getattr(app.state, "provider_catalog", None)
    newer = {
        str(row.get("id")): row
        for row in ((current or {}).get("providers") or [])
        if isinstance(row, dict)
    }
    kept: list[dict[str, Any]] = []
    stamped: list[str] = []
    for record in records:
        provider_id = str(record.get("id") or "")
        if provider_seq(app, provider_id) != seqs.get(provider_id, 0) and provider_id in newer:
            kept.append(newer[provider_id])
        else:
            kept.append(record)
            stamped.append(provider_id)
    return kept, stamped


def _schedule_reprobe(app: "FastAPI", payload: dict[str, Any]) -> None:
    from clio_agent.gact.provider_catalog_reprobe import (  # noqa: PLC0415 - cycle
        schedule_stale_reprobe,
    )

    schedule_stale_reprobe(app, payload)


async def read_catalog(
    app: "FastAPI", *, refresh: bool = False, provider_id: str = ""
) -> dict[str, Any]:
    """Serve the catalog snapshot, re-discovering exactly what must be re-discovered.

    Args:
        app: The GACT application (owns the snapshot on ``app.state``).
        refresh: Force a live re-probe (bypassing the handshake TTL cache).
        provider_id: Limit a refresh to one provider; its entry is merged into the
            snapshot. Empty means every provider.

    Raises:
        UnknownCatalogProviderError: ``provider_id`` names no registered preset.
    """

    preset_ids = [preset.id for preset in as_lm_presets()]
    if provider_id and provider_id not in preset_ids:
        raise UnknownCatalogProviderError(provider_id)
    cached = getattr(app.state, "provider_catalog", None)
    pending = _pending(app)
    # Only the invalidations seen NOW are answered by this read; one that
    # arrives while discovery is awaited stays pending for the next read.
    taken = set(pending)
    seqs = {pid: provider_seq(app, pid) for pid in preset_ids}
    if not isinstance(cached, dict) and provider_id:
        # No snapshot yet (boot, or retired by a model refresh): build it from
        # cached handshakes, forcing only the provider that was asked about.
        others, targeted = await asyncio.gather(
            discover([pid for pid in preset_ids if pid != provider_id], refresh=False),
            discover([provider_id], refresh=refresh),
        )
        records, stamped = _keep_newer(app, [*others, *targeted], seqs)
        by_id = {str(record.get("id")): record for record in records}
        payload = _payload([by_id[pid] for pid in preset_ids if pid in by_id])
        pending.difference_update(taken)
        commit(app, payload, stamped)
        publish(app, payload)
        _schedule_reprobe(app, payload)
        return payload
    if not isinstance(cached, dict) or (refresh and not provider_id):
        records, stamped = _keep_newer(app, await discover(preset_ids, refresh=refresh), seqs)
        payload = _payload(records)
        pending.difference_update(taken)
        commit(app, payload, stamped)
        if refresh:
            publish(app, payload)
        _schedule_reprobe(app, payload)
        return payload

    targets = sorted(taken & set(preset_ids))
    if provider_id and (refresh or provider_id in taken) and provider_id not in targets:
        targets.append(provider_id)
    if not targets:
        _schedule_reprobe(app, cached)
        return cached
    # An invalidated provider's cached handshake is already gone, so a plain
    # read re-probes it; an explicit refresh forces through the TTL cache.
    records, stamped = _keep_newer(app, await discover(targets, refresh=refresh), seqs)
    pending.difference_update(targets)
    current = getattr(app.state, "provider_catalog", None)
    payload = merge(current if isinstance(current, dict) else cached, records)
    commit(app, payload, stamped)
    publish(app, payload)
    _schedule_reprobe(app, payload)
    return payload


__all__ = [
    "UnknownCatalogProviderError",
    "commit",
    "discover",
    "invalidate_provider",
    "is_stale",
    "merge",
    "provider_seq",
    "publish",
    "read_catalog",
    "stale_provider_ids",
]
