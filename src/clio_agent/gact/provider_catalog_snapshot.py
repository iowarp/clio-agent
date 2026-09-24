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
from clio_agent.providers.api_base import normalize as _normalize_api_base
from clio_agent.providers.catalog import as_lm_presets
from clio_agent.providers.handshake import cache as handshake_cache
from clio_agent.providers.identity import EndpointKey, endpoint_key
from clio_agent.providers.model_discovery import LAST_GOOD_CATALOG_SOURCE

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import LMProviderPreset

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


def provider_seq(app: "FastAPI", provider_id: str, api_base: str) -> int:
    """The sequence number of the last snapshot write for this ``(provider_id,
    api_base)`` endpoint identity (0: never) -- see :mod:`clio_agent.providers.identity`."""

    seqs = getattr(app.state, _SEQ_ATTR, None)
    if not isinstance(seqs, dict):
        return 0
    return int(seqs.get(endpoint_key(provider_id, api_base), 0))


def commit(app: "FastAPI", payload: dict[str, Any], provider_ids: list[str]) -> None:
    """Install ``payload`` as the snapshot and stamp the providers it re-evidenced.

    Each id is stamped under its endpoint identity -- ``(provider_id,
    normalized api_base)``, the api_base its OWN record in ``payload`` was
    actually discovered against -- so a provider whose configured endpoint
    changes stamps a different key rather than overwriting the freshness
    ledger for its old one (model-capabilities brief Part 3).
    """

    seqs = getattr(app.state, _SEQ_ATTR, None)
    if not isinstance(seqs, dict):
        seqs = {}
        setattr(app.state, _SEQ_ATTR, seqs)
    stamp = next(_SEQUENCE)
    by_id = {
        str(record.get("id")): record
        for record in payload.get("providers") or []
        if isinstance(record, dict)
    }
    for provider_id in provider_ids:
        api_base = str(by_id.get(provider_id, {}).get("endpoint") or "")
        seqs[endpoint_key(provider_id, api_base)] = stamp
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


def _configured_preset_override(app: "FastAPI") -> tuple[str, Any] | None:
    """The one preset row whose ``api_base`` the active bind has overridden.

    ``app.state.lm_config`` (set by ``PUT /v1/providers/lm``) carries the
    endpoint a person actually configured; the static catalog preset only
    ever carries its compiled-in default. Discovery for that ONE bound
    provider must probe the CONFIGURED endpoint -- otherwise a llama.cpp/vLLM
    server pointed at a non-default port is never actually asked, its real
    model id is never discovered, and the catalog keeps serving whatever the
    default port would have answered (#1418 cause C).

    Returns ``(provider_id, overridden_preset)``, or ``None`` when nothing is
    bound or the bound endpoint matches the catalog default already (compared
    normalized -- Part 3 -- so a cosmetic difference like a trailing slash
    never reads as an override). This app runs one active LM at a time, so
    there is still only ever one relevant api_base per provider_id; what Part
    3 adds is that the freshness ledger (:func:`provider_seq` / :func:`commit`)
    is keyed on that api_base too, not just the id, so a rebind's discovery
    can never be mistaken for stale evidence of the id's PREVIOUS endpoint.
    A rebind also still invalidates the whole snapshot wholesale
    (``app.state.provider_catalog = None`` in the ``PUT`` handler); this only
    has to pick the right base for the rebuild that follows.
    """
    cfg = getattr(app.state, "lm_config", None) or {}
    provider_id = str(cfg.get("provider_id") or "")
    api_base = str(cfg.get("api_base") or "")
    if not provider_id or not api_base:
        return None
    preset = next((p for p in as_lm_presets() if p.id == provider_id), None)
    if preset is None or _normalize_api_base(preset.api_base) == _normalize_api_base(api_base):
        return None
    return provider_id, preset.model_copy(update={"api_base": api_base})


def _resolve_presets(app: "FastAPI") -> dict[str, "LMProviderPreset"]:
    """Every catalog preset, with the actively bound provider's api_base override applied.

    The single source both :func:`discover` and the freshness-ledger seq
    lookups use for "what api_base is this provider_id's identity right now",
    so the two stay consistent (:func:`_configured_preset_override`).
    """

    presets = {preset.id: preset for preset in as_lm_presets()}
    override = _configured_preset_override(app)
    if override is not None:
        presets[override[0]] = override[1]
    return presets


async def discover(
    app: "FastAPI", provider_ids: list[str], *, refresh: bool
) -> list[dict[str, Any]]:
    """Discover the named providers concurrently (unknown ids are skipped).

    The provider currently bound as the active global LM is probed at its
    CONFIGURED ``api_base`` rather than the catalog preset's default; see
    :func:`_configured_preset_override`.
    """

    presets = _resolve_presets(app)
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
    app: "FastAPI", records: list[dict[str, Any]], seqs: dict[EndpointKey, int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Swap in the CURRENT entry for any provider re-written while ``records`` was awaited.

    Returns the records to install and the ids they newly evidence: a provider
    whose sequence moved (a background re-probe answered meanwhile, or its
    configured endpoint changed underneath this read) keeps that newer entry
    and is not re-stamped. ``seqs`` is keyed by endpoint identity
    (:func:`provider_seq`), captured before discovery ran.
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
        api_base = str(record.get("endpoint") or "")
        key = endpoint_key(provider_id, api_base)
        if provider_seq(app, provider_id, api_base) != seqs.get(key, 0) and provider_id in newer:
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
    presets = _resolve_presets(app)
    seqs = {
        endpoint_key(pid, presets[pid].api_base): provider_seq(app, pid, presets[pid].api_base)
        for pid in preset_ids
        if pid in presets
    }
    if not isinstance(cached, dict) and provider_id:
        # No snapshot yet (boot, or retired by a model refresh): build it from
        # cached handshakes, forcing only the provider that was asked about.
        others, targeted = await asyncio.gather(
            discover(app, [pid for pid in preset_ids if pid != provider_id], refresh=False),
            discover(app, [provider_id], refresh=refresh),
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
        records, stamped = _keep_newer(app, await discover(app, preset_ids, refresh=refresh), seqs)
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
    records, stamped = _keep_newer(app, await discover(app, targets, refresh=refresh), seqs)
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
