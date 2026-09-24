"""The in-process snapshot behind ``GET /v1/provider-catalog``.

Discovery is a handshake per provider, so ordinary reads serve one snapshot
instead of re-probing every provider. Three things retire parts of it:

* ``refresh=true`` re-probes every provider, or only ``provider=<id>``.
* :func:`invalidate_provider` — called when a provider's credential state
  changes out of band (sign-in completed, an explicit "Check provider") —
  drops that provider's cached handshake and marks its snapshot entry for
  re-discovery on the very next read, so no client has to know to ask.
* A provider served from its last-good list (a live probe came back empty) is
  re-probed in the background on later reads until a live answer replaces it;
  the replacement is published as ``provider_catalog.refreshed``. The handshake
  TTL cache bounds how often that re-probe really touches the network.
"""

from __future__ import annotations

import asyncio
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
_REPROBE_TASK_ATTR = "provider_catalog_reprobe_task"


class UnknownCatalogProviderError(LookupError):
    """``provider=<id>`` named no registered provider preset."""


def invalidate_provider(app: "FastAPI", provider_id: str) -> None:
    """Retire one provider's catalog evidence after its credential state changed.

    Drops every cached handshake report for the provider and marks its snapshot
    entry so the next catalog read re-discovers it (with no ``refresh`` flag
    needed). Other providers' entries are untouched.
    """

    handshake_cache.invalidate_provider(provider_id)
    pending = getattr(app.state, _INVALIDATED_ATTR, None)
    if not isinstance(pending, set):
        pending = set()
        setattr(app.state, _INVALIDATED_ATTR, pending)
    pending.add(provider_id)


def _pending(app: "FastAPI") -> set[str]:
    pending = getattr(app.state, _INVALIDATED_ATTR, None)
    return pending if isinstance(pending, set) else set()


def _publish(app: "FastAPI", payload: dict[str, Any]) -> None:
    for session in app.state.sessions.list():
        app.state.bus.publish(
            Event(type="provider_catalog.refreshed", session_id=session.id, payload=payload)
        )


def _payload(providers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"catalog_id": "active", "providers": providers, "authoritative": "live_handshake"}


def _stale_provider_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for provider in payload.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        freshness = provider.get("freshness")
        if isinstance(freshness, dict) and freshness.get("source") == LAST_GOOD_CATALOG_SOURCE:
            ids.append(str(provider.get("id") or ""))
    return [provider_id for provider_id in ids if provider_id]


def _merge(payload: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the matching provider entries in ``payload`` (preset order is kept)."""

    by_id = {str(record.get("id")): record for record in records}
    providers = [
        by_id.pop(str(provider.get("id")), provider)
        for provider in payload.get("providers") or []
        if isinstance(provider, dict)
    ]
    providers.extend(by_id.values())
    return _payload(providers)


async def _discover(provider_ids: list[str], *, refresh: bool) -> list[dict[str, Any]]:
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


async def _reprobe_stale(app: "FastAPI", provider_ids: list[str]) -> None:
    """Re-probe last-good providers; publish when a live answer replaced one."""

    try:
        records = await _discover(provider_ids, refresh=False)
    except Exception as exc:  # noqa: BLE001 - a background re-probe must never crash the loop
        logger.warning(
            "provider catalog re-probe failed: reason=provider_catalog_reprobe_failed "
            "providers=%s error=%r",
            provider_ids,
            exc,
        )
        return
    replaced = [
        record
        for record in records
        if (record.get("freshness") or {}).get("source") != LAST_GOOD_CATALOG_SOURCE
    ]
    if not replaced:
        return
    cached = getattr(app.state, "provider_catalog", None)
    if not isinstance(cached, dict):
        return
    payload = _merge(cached, replaced)
    app.state.provider_catalog = payload
    _publish(app, payload)


def schedule_stale_reprobe(app: "FastAPI", payload: dict[str, Any]) -> asyncio.Task | None:
    """Start one background re-probe of last-good providers, unless one is running."""

    stale = _stale_provider_ids(payload)
    if not stale:
        return None
    running = getattr(app.state, _REPROBE_TASK_ATTR, None)
    if isinstance(running, asyncio.Task) and not running.done():
        return None
    task = asyncio.create_task(_reprobe_stale(app, stale))
    setattr(app.state, _REPROBE_TASK_ATTR, task)
    return task


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
    if not isinstance(cached, dict) or (refresh and not provider_id):
        payload = _payload(await _discover(preset_ids, refresh=refresh))
        pending.clear()
        app.state.provider_catalog = payload
        if refresh:
            _publish(app, payload)
        schedule_stale_reprobe(app, payload)
        return payload

    targets = sorted(pending & set(preset_ids))
    if provider_id and (refresh or provider_id in pending) and provider_id not in targets:
        targets.append(provider_id)
    if not targets:
        schedule_stale_reprobe(app, cached)
        return cached
    # An invalidated provider's cached handshake is already gone, so a plain
    # read re-probes it; an explicit refresh forces through the TTL cache.
    records = await _discover(targets, refresh=refresh)
    pending.difference_update(targets)
    payload = _merge(cached, records)
    app.state.provider_catalog = payload
    _publish(app, payload)
    schedule_stale_reprobe(app, payload)
    return payload


__all__ = [
    "UnknownCatalogProviderError",
    "invalidate_provider",
    "read_catalog",
    "schedule_stale_reprobe",
]
