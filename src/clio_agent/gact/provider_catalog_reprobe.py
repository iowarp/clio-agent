"""Background re-probe of providers served from their last-good model list.

When a provider's live check comes back empty (ALCF at boot: the stored Globus
sign-in cannot mint a token yet, the gateway is briefly unreachable) the
catalog serves its last good list, typed stale. This loop re-checks those
providers until a live answer replaces them:

* **Real re-probes.** The handshake TTL cache also caches FAILURES, so each
  attempt first drops that provider's cached report; otherwise the "re-probe"
  would re-read the same failure for 30 seconds and never touch the network.
* **Bounded backoff.** :data:`REPROBE_BACKOFF_S` (5s, 30s, 2min), then every
  :data:`REPROBE_STEADY_S` (10min) while anything is still stale. The loop
  stops as soon as no provider in the snapshot is stale (a live answer
  arrived, or a refresh replaced the entry) or the snapshot was retired.
* **Never on a reason only sign-in fixes.** A provider stuck on a
  :data:`NO_AUTO_REPROBE_REASONS` reason (ALCF's ``argonne_reauthentication_required``)
  is excluded from every attempt AND from the loop's own stop condition: no
  amount of retrying on a timer produces a different answer, and a completed
  sign-in already invalidates the provider so the very next catalog read
  re-discovers it -- a background timer would just re-fail the same way every
  10 minutes forever.
* **Diagnosable.** Every attempt logs a typed ``provider_catalog_reprobe_attempt``
  line per provider with its outcome and the live failure it saw.
* **Never clobbers newer truth.** A result is merged only if nothing re-wrote
  that provider's entry while the attempt was in flight
  (:func:`~clio_agent.gact.provider_catalog_snapshot.provider_seq`).

The task lives on ``app.state.provider_catalog_reprobe_task`` and is cancelled
at lifespan shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact import provider_catalog_snapshot as snapshot
from clio_agent.providers.handshake import cache as handshake_cache

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

REPROBE_TASK_ATTR = "provider_catalog_reprobe_task"

#: Delays before the first attempts, then :data:`REPROBE_STEADY_S` forever.
REPROBE_BACKOFF_S: tuple[float, ...] = (5.0, 30.0, 120.0)
REPROBE_STEADY_S = 600.0

#: Live-failure reasons a timer retry cannot fix -- only a credential change
#: does (a completed sign-in already invalidates the provider, so the very
#: next catalog read re-discovers it with no reprobe needed). Retrying these
#: on the steady 10-minute cadence forever would just re-fail the same way.
NO_AUTO_REPROBE_REASONS: frozenset[str] = frozenset({"argonne_reauthentication_required"})


#: Consecutive failed attempts per provider logged at INFO; later ones at DEBUG
#: (the steady 10-minute retries of a down provider would otherwise flood INFO).
REPROBE_INFO_FAILURES = 3


def _delay(attempt: int) -> float:
    return REPROBE_BACKOFF_S[attempt] if attempt < len(REPROBE_BACKOFF_S) else REPROBE_STEADY_S


def _live_failure(record: dict[str, Any]) -> str:
    freshness = record.get("freshness") if isinstance(record.get("freshness"), dict) else {}
    staleness = freshness.get("staleness") if isinstance(freshness, dict) else None
    if isinstance(staleness, dict) and staleness.get("live_failure"):
        return str(staleness["live_failure"])
    return str(record.get("failure") or "")


def _needs_reauth(record: dict[str, Any]) -> bool:
    """Whether ``record``'s live failure is a reason only a fresh sign-in fixes."""

    reason = _live_failure(record).split(":", 1)[0].strip()
    return reason in NO_AUTO_REPROBE_REASONS


def _reprobe_target_ids(payload: Any) -> list[str]:
    """Stale provider ids worth an automatic timed re-probe.

    Excludes providers stuck on a :data:`NO_AUTO_REPROBE_REASONS` reason --
    retrying those on a timer cannot help, and a completed sign-in already
    invalidates the provider so the next read re-discovers it immediately.
    """

    if not isinstance(payload, dict):
        return []
    return [
        str(provider.get("id") or "")
        for provider in payload.get("providers") or []
        if isinstance(provider, dict)
        and provider.get("id")
        and snapshot.is_stale(provider)
        and not _needs_reauth(provider)
    ]


async def _attempt(
    app: "FastAPI", attempt: int, stale: list[str], failures: dict[str, int] | None = None
) -> None:
    """One re-probe of ``stale``: real handshakes, merged only over unchanged entries.

    ``failures`` counts each provider's consecutive failed attempts (reset by a
    live answer): the first :data:`REPROBE_INFO_FAILURES` log at INFO, later ones
    at DEBUG, and a live answer always logs at INFO.
    """
    failures = failures if failures is not None else {}

    # The api_base each stale id's CURRENT entry carries -- its identity for the
    # seq check below, so a rebind mid-attempt (whole snapshot swapped, endpoint
    # possibly changed) reads as "superseded" rather than comparing across two
    # different endpoints under the same id (model-capabilities brief Part 3).
    current_records = {
        str(record.get("id")): record
        for record in (getattr(app.state, "provider_catalog", None) or {}).get("providers") or []
        if isinstance(record, dict)
    }
    seqs = {
        provider_id: snapshot.provider_seq(
            app, provider_id, str(current_records.get(provider_id, {}).get("endpoint") or "")
        )
        for provider_id in stale
    }
    for provider_id in stale:
        handshake_cache.invalidate_provider(provider_id)
    snapshot.mark_checking(app, stale)
    try:
        records = await snapshot.discover(app, stale, refresh=False)
    finally:
        snapshot.clear_checking(app, stale)
    fresh: list[dict[str, Any]] = []
    for record in records:
        provider_id = str(record.get("id") or "")
        api_base = str(record.get("endpoint") or "")
        superseded = snapshot.provider_seq(app, provider_id, api_base) != seqs.get(provider_id)
        outcome = (
            "superseded" if superseded else "still_stale" if snapshot.is_stale(record) else "live"
        )
        if outcome == "still_stale":
            failures[provider_id] = failures.get(provider_id, 0) + 1
        elif outcome == "live":
            failures.pop(provider_id, None)
        quiet = failures.get(provider_id, 0) > REPROBE_INFO_FAILURES
        logger.log(
            logging.DEBUG if quiet else logging.INFO,
            "provider catalog re-probe: reason=provider_catalog_reprobe_attempt attempt=%d "
            "provider=%s outcome=%s live_failure=%r",
            attempt,
            provider_id,
            outcome,
            _live_failure(record) if outcome != "live" else "",
        )
        if not superseded:
            fresh.append(record)
    current = getattr(app.state, "provider_catalog", None)
    if not fresh or not isinstance(current, dict):
        return
    payload = snapshot.merge(current, fresh)
    snapshot.commit(app, payload, [str(record.get("id")) for record in fresh])
    if any(not snapshot.is_stale(record) for record in fresh):
        snapshot.publish(app, payload)


async def reprobe_until_live(app: "FastAPI") -> None:
    """Re-probe stale providers with bounded backoff until none is stale."""

    attempt = 0
    failures: dict[str, int] = {}
    while _reprobe_target_ids(getattr(app.state, "provider_catalog", None)):
        await asyncio.sleep(_delay(attempt))
        attempt += 1
        stale = _reprobe_target_ids(getattr(app.state, "provider_catalog", None))
        if not stale:
            return
        try:
            await _attempt(app, attempt, stale, failures)
        except Exception as exc:  # noqa: BLE001 - one failed attempt must not end the loop
            logger.warning(
                "provider catalog re-probe: reason=provider_catalog_reprobe_failed "
                "attempt=%d providers=%s error=%r",
                attempt,
                stale,
                exc,
            )


def schedule_stale_reprobe(app: "FastAPI", payload: dict[str, Any]) -> asyncio.Task | None:
    """Start the re-probe loop when ``payload`` serves a stale provider and none runs."""

    if not _reprobe_target_ids(payload):
        return None
    running = getattr(app.state, REPROBE_TASK_ATTR, None)
    if isinstance(running, asyncio.Task) and not running.done():
        return None
    task = asyncio.create_task(reprobe_until_live(app))
    setattr(app.state, REPROBE_TASK_ATTR, task)
    return task


__all__ = [
    "NO_AUTO_REPROBE_REASONS",
    "REPROBE_BACKOFF_S",
    "REPROBE_STEADY_S",
    "REPROBE_TASK_ATTR",
    "reprobe_until_live",
    "schedule_stale_reprobe",
]
